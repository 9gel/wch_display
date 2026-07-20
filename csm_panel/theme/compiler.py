#!/usr/bin/env python3
"""
compile_ui_to_blob: compiler for the WCH 8040 panel "theme blob".

Turns a decrypted .ui XML (+ theme images dir) into the binary theme blob the
panel consumes.  Two authoring modes:

  render_text=False  (legacy reuse path, CONFIRMED byte-exact)
      Rebuilds the descriptor + 64-byte widget table from the .ui, and REUSES a
      base blob's records + resource area (background JPEG + StaticText glyph
      masks + Number/DateTime glyph-metric tails) verbatim, matched by type+order.
      Reproduces homelab_blob_v2 / base_theme BYTE-FOR-BYTE.

  render_text=True   (from-scratch authoring, NEW)
      Renders each StaticText (type 2) as an 8-bpp coverage mask with PIL, lays
      out a fresh resource area, and points every StaticText [12:15] at its mask.
      Backgrounds are honoured from <widgetParent> (solid color -> descriptor
      [0x4c]; image -> JPEG record at 0x1000). Number/DateTime glyph-metric tails
      still come from a base blob's metric LIBRARY keyed by (type, fontSize),
      because the firmware font cannot be synthesised offline.

STATUS OF EACH PIECE (see SPEC.md for the full derivation):
  * descriptor / header .................. CONFIRMED (A,C exact; B consistent)
  * widget-table framing (64B entries) ... CONFIRMED (A,C exact)
  * .ui-type -> blob-type map ............ CONFIRMED (A,C exact)
  * coordinate reslice transform ......... CONFIRMED (A,C exact, byte-perfect)
  * ProgressBar(0x8b) color layout ....... CONFIRMED
  * StaticText(0x93) color+ptr layout .... CONFIRMED (ptr into 8bpp mask area)
  * Number(0x92) color + hAlign flag ..... CONFIRMED; digit-metric table INFERRED
  * DateTime(0x8e) color + format string . CONFIRMED
  * Image(0x84) ptr/framecount/delay ..... INFERRED
  * StaticText mask RENDERING (render_text) structurally CONFIRMED (8bpp, w*h,
                                           row-major); glyph shapes differ from
                                           the vendor font. EXPERIMENTAL: a
                                           render_text blob has bricked the panel
                                           ("MDT Error") once — the from-scratch
                                           descriptor/resource layout is not yet
                                           HW-verified. VALIDATE (open the .ui in
                                           the vendor editor / capture its
                                           download) before flashing; keep the
                                           firmware recovery frames handy.
  * background color [0x4c] .............. CONFIRMED (homelab bg fff1f1f1->0xf79e)
  * Number/DateTime metric tail by size .. INFERRED (copied from base library)
"""
import glob
import os
import struct
import xml.etree.ElementTree as ET
from collections import defaultdict, deque

WIDGET_TABLE_START = 0x80
ENTRY = 64
RECORD_OFFSET = 0x1000   # fixed offset of the first (background) JPEG record

# .ui widget "type" -> blob type code            (CONFIRMED A,C; Image from B)
UI2BLOB = {2: 0x93, 3: 0x8b, 5: 0x92, 6: 0x8e, 4: 0x84}


def rgb565(argb_hex):
    """'ffRRGGBB' (or 'RRGGBB') ARGB hex -> RGB565 int."""
    if not argb_hex:
        return 0
    h = argb_hex.strip()
    if len(h) == 8:
        h = h[2:]
    r = int(h[0:2], 16); g = int(h[2:4], 16); b = int(h[4:6], 16)
    return ((r >> 3) << 11) | ((g >> 2) << 5) | (b >> 3)


def argb_to_rgb(argb_hex):
    """'ffRRGGBB' -> (r,g,b) tuple of 8-bit ints."""
    h = (argb_hex or "ff000000").strip()
    if len(h) == 8:
        h = h[2:]
    return int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)


def ui_to_blob_xy(ux, uy, portrait):
    """Portrait 480x800 -> landscape framebuffer via 256-tall band reslice.
    bl_x = (ux mod 256) + 256*(uy//256) ; bl_y = uy mod 256.   Landscape = identity."""
    if not portrait:
        return ux, uy
    band = uy // 256
    return (ux % 256) + 256 * band, uy % 256


# ---------------------------------------------------------------------------
# font discovery + StaticText mask rendering
# ---------------------------------------------------------------------------
_FONT_CACHE = {}


def _find_font(bold):
    name = "DejaVuSans-Bold.ttf" if bold else "DejaVuSans.ttf"
    hits = sorted(glob.glob(f"/nix/store/*/share/fonts/truetype/{name}"))
    if not hits:
        # fall back to any DejaVu on the system
        hits = sorted(glob.glob(f"/**/{name}", recursive=False)) or \
               sorted(glob.glob(f"/usr/share/fonts/**/{name}", recursive=True))
    if not hits:
        raise FileNotFoundError(f"could not locate {name} under /nix/store")
    return hits[0]


def render_text_mask(text, font_size, bold):
    """Render `text` to an 8-bpp coverage mask.

    Returns (mask_bytes, w, h): mask_bytes is w*h bytes, row-major, one alpha
    byte per pixel (0=transparent .. 255=opaque), tightly cropped to the inked
    bounding box. The device colorises this with the entry's fontColor565.
    """
    from PIL import Image, ImageDraw, ImageFont
    key = (_find_font(bool(bold)), int(font_size))
    font = _FONT_CACHE.get(key)
    if font is None:
        font = ImageFont.truetype(key[0], key[1])
        _FONT_CACHE[key] = font
    if not text:
        return b"", 0, 0
    # oversize canvas, draw white text on black, then crop to ink bbox
    tmp = Image.new("L", (max(4, len(text) * font_size * 2 + 8), font_size * 3 + 8), 0)
    d = ImageDraw.Draw(tmp)
    d.text((4, 4), text, fill=255, font=font)
    bbox = tmp.getbbox()
    if bbox is None:
        return b"", 0, 0
    crop = tmp.crop(bbox)
    w, h = crop.size
    return crop.tobytes(), w, h


# ---------------------------------------------------------------------------
# .ui parsing (minimal, order-preserving)
# ---------------------------------------------------------------------------
def parse_ui(xml_bytes):
    root = ET.fromstring(xml_bytes.decode("utf-8", "replace"))
    out = []
    for el in root:
        w = {"tag": el.tag, "type": int(el.get("type", "0"))}
        g = el.find("geometry")
        for k in ("x", "y", "width", "height"):
            w[k] = int(g.findtext(k, "0")) if g is not None else 0
        s = el.find("sensor")
        w["fastSensor"] = int(s.findtext("fastSensor", "0")) if s is not None else 0
        f = el.find("font")
        if f is not None:
            w["fontColor"] = f.findtext("fontColor", "ffffffff")
            w["text"] = f.findtext("text", "") or ""
            w["fontSize"] = int(f.findtext("fontSize", "0") or "0")
            w["bold"] = int(f.findtext("bold", "0") or "0")
        else:
            w["fontColor"] = "ffffffff"; w["text"] = ""; w["fontSize"] = 0; w["bold"] = 0
        w["hAlign"] = int(el.findtext("hAlign", "0"))
        st = el.find("style")
        if st is not None:
            w["fgColor"] = st.findtext("fgColor", "ff000000")
            w["bgColor"] = st.findtext("bgColor", "ff000000")
            w["frameColor"] = st.findtext("frameColor", "ff000000")
        w["imageDelay"] = int(el.findtext("imageDelay", "0"))
        if el.tag == "widgetParent":
            w["backgroundType"] = int(el.findtext("backgroundType", "0"))
            w["backgroundColor"] = el.findtext("backgroundColor", "ff000000") or "ff000000"
            w["backgroundImagePath"] = el.findtext("backgroundImagePath", "") or ""
        else:
            w["backgroundType"] = 0
        out.append(w)
    return out


# ---------------------------------------------------------------------------
# base-blob helpers (records + resource area + metric library)
# ---------------------------------------------------------------------------
def _base_widget_table(base):
    """Return (list-of-64B-entries, offset-just-past-table)."""
    ents = []
    off = WIDGET_TABLE_START
    while off + ENTRY <= len(base):
        e = base[off:off + ENTRY]
        if not (0x80 <= e[0] <= 0x9f):
            break
        ents.append(bytearray(e))
        off += ENTRY
    return ents, off


def build_metric_library(base_blobs_and_uis):
    """Survey base blobs -> {(blob_type, fontSize): tail12_64_bytes} for
    Number(0x92)/DateTime(0x8e). Each tail is the full 52-byte [12:64] span from
    a representative entry; the caller re-injects the correct fontColor but keeps
    the size-dependent advance-width metrics + format template.

    base_blobs_and_uis: iterable of (blob_bytes, ui_xml_bytes).
    """
    lib = {}
    for blob, ui_xml in base_blobs_and_uis:
        ents, _ = _base_widget_table(blob)
        widgets = [w for w in parse_ui(ui_xml) if w["tag"] == "widget"]
        bi = 0
        for w in widgets:
            bt = UI2BLOB.get(w["type"])
            if bt is None:
                continue
            if bi >= len(ents):
                break
            e = ents[bi]; bi += 1
            if e[0] != bt:
                break  # alignment broke; stop trusting this pair
            if bt in (0x92, 0x8e):
                lib.setdefault((bt, w["fontSize"]), bytes(e[12:64]))
    return lib


def _nearest_size(lib, bt, size, warnings):
    if (bt, size) in lib:
        return lib[(bt, size)]
    cands = [s for (t, s) in lib if t == bt]
    if not cands:
        return None
    best = min(cands, key=lambda s: abs(s - size))
    warnings.append(f"no {('Number' if bt==0x92 else 'DateTime')} metrics for "
                    f"size {size}; using nearest available size {best}")
    return lib[(bt, best)]


# ---------------------------------------------------------------------------
# entry builders
# ---------------------------------------------------------------------------
def build_entry(w, wid, portrait, template=None, metric_tail=None,
                mask_w=None, mask_h=None, mask_ptr=None, field_override=None):
    """Build one 64-byte widget entry.

    template   : legacy reuse path — full 64-byte base entry of same type; lends
                 glyph tails AND the rendered-bitmap w/h for text/number/datetime.
    metric_tail: from-scratch path — 52-byte [12:64] span from the metric library
                 (Number/DateTime), whose advance metrics we keep and colour we
                 overwrite.
    mask_w/h/ptr: from-scratch path — StaticText rendered-mask size + resource ptr.
    """
    template_tail = template[12:64] if template else None
    bt = UI2BLOB[w["type"]]
    bx, by = ui_to_blob_xy(w["x"], w["y"], portrait)
    e = bytearray(64)
    e[0] = bt
    e[1] = wid & 0xFF
    # field id [2]: DateTime is fixed 0x15; otherwise the data-field id. The
    # exact convention is theme-dependent (Simplicity stores == fastSensor;
    # homelab_v2 stores fastSensor+1 so the 0x66 driver addresses fields 2..21).
    # In the reuse path we copy the aligned base entry's [2] so the output is
    # byte-exact regardless of convention; from-scratch defaults to == fastSensor.
    if field_override is not None:
        e[2] = field_override & 0xFF
    else:
        e[2] = (0x15 if bt == 0x8e else w.get("fastSensor", 0)) & 0xFF
    e[3] = (w["x"] // 256) & 0xFF        # CONFIRMED: portrait x-band index
    struct.pack_into("<H", e, 4, bx & 0xFFFF)
    struct.pack_into("<H", e, 6, by & 0xFFFF)
    struct.pack_into("<H", e, 8, w["width"] & 0xFFFF)
    e[10] = w["height"] & 0xFF

    if bt == 0x8b:   # ProgressBar
        e[11] = 1
        struct.pack_into(">H", e, 12, rgb565(w.get("bgColor", "ff000000")))
        struct.pack_into(">H", e, 14, rgb565(w.get("fgColor", "ff000000")))
        struct.pack_into(">H", e, 16, rgb565(w.get("frameColor", "ff000000")))
        if template_tail:
            e[18:64] = template_tail[6:52]
    elif bt == 0x93:  # StaticText
        e[11] = 0
        if template:                       # legacy: reuse mask + w/h + color
            e[8:12] = template[8:12]
            e[12:64] = template_tail
        elif mask_ptr is not None:         # from-scratch: our rendered mask
            struct.pack_into("<H", e, 8, mask_w & 0xFFFF)
            e[10] = mask_h & 0xFF
            e[12] = (mask_ptr >> 16) & 0xFF
            e[13] = (mask_ptr >> 8) & 0xFF
            e[14] = mask_ptr & 0xFF
            struct.pack_into(">H", e, 15, rgb565(w.get("fontColor")))
            e[17] = 0xFF
        else:
            struct.pack_into(">H", e, 15, rgb565(w.get("fontColor")))
            e[17] = 0xFF
    elif bt == 0x92:  # Number
        e[11] = w.get("hAlign", 0) & 0xFF
        tail = template[12:64] if template else metric_tail
        if tail:
            e[8:12] = (template[8:12] if template else e[8:12])
            e[12:64] = tail
            # overwrite fontColor with this widget's color; keep metrics/marker
            struct.pack_into(">H", e, 12, rgb565(w.get("fontColor")))
            e[14] = 0x00; e[15] = 0xFF
        else:
            struct.pack_into(">H", e, 12, rgb565(w.get("fontColor")))
            e[14] = 0x00; e[15] = 0xFF
    elif bt == 0x8e:  # DateTime
        e[11] = 1
        tail = template[12:64] if template else metric_tail
        if tail:
            e[8:12] = (template[8:12] if template else e[8:12])
            e[12:64] = tail
            struct.pack_into(">H", e, 12, rgb565(w.get("fontColor")))
            e[14] = 0xFF
        else:
            struct.pack_into(">H", e, 12, rgb565(w.get("fontColor")))
            e[14] = 0xFF
    elif bt == 0x84:  # Image (animation)
        e[11] = 0
        struct.pack_into(">H", e, 18, w.get("imageDelay", 0) & 0xFFFF)
    return e


# ---------------------------------------------------------------------------
# main compiler
# ---------------------------------------------------------------------------
def compile_ui_to_blob(ui_xml, images_dir=None, base_blob_for_resources=None,
                       render_text=True, extra_metric_bases=None):
    """Compile a decrypted .ui (bytes) into a theme blob (bytes).

    render_text=False : legacy reuse path (byte-exact self-compile). Requires
                        base_blob_for_resources; reuses its records + resources.
    render_text=True  : from-scratch authoring. StaticText masks are rendered
                        with PIL; background honours <widgetParent>; Number/
                        DateTime metric tails are taken from the metric library
                        built from base_blob_for_resources (+ extra_metric_bases).

    images_dir : theme image folder (for background image JPEG).
    base_blob_for_resources : a known-good same-resolution blob. In legacy mode
                        it supplies the whole resource area; in from-scratch mode
                        it (plus extra_metric_bases) supplies Number/DateTime
                        metric tails only.
    extra_metric_bases : optional list of (blob_bytes, ui_xml_bytes) to widen the
                        metric library (more font sizes).
    """
    widgets = parse_ui(ui_xml)
    parent = next((w for w in widgets if w["tag"] == "widgetParent"), None)
    uiw = [w for w in widgets if w["tag"] == "widget"]
    W = parent["width"] if parent else 480
    H = parent["height"] if parent else 800
    portrait = H > W

    if not render_text:
        return _compile_reuse(uiw, parent, images_dir, base_blob_for_resources,
                              W, H, portrait)
    return _compile_from_scratch(uiw, parent, images_dir, base_blob_for_resources,
                                 extra_metric_bases, W, H, portrait)


def _compile_reuse(uiw, parent, images_dir, base, W, H, portrait):
    """Legacy path: rebuild descriptor+table, reuse base records+resources.
    Byte-exact against homelab_blob_v2 / base_theme."""
    assert base is not None, "render_text=False requires base_blob_for_resources"
    base_ents, base_table_end = _base_widget_table(base)
    pool = defaultdict(deque)
    for be in base_ents:
        pool[be[0]].append(bytes(be))

    table = bytearray()
    for i, w in enumerate(uiw, start=1):
        bt = UI2BLOB.get(w["type"])
        if bt is None:
            continue
        tmpl = pool[bt].popleft() if pool[bt] else None
        fo = tmpl[2] if tmpl else None      # copy base field-id convention
        table += build_entry(w, i, portrait, template=tmpl, field_override=fo)

    out = bytearray(base)
    out[0:4] = b"\x96\x02\x00\x00"
    out[0x40] = 0x81
    struct.pack_into(">H", out, 0x47, W)
    struct.pack_into(">H", out, 0x49, H)
    struct.pack_into(">H", out, 0x4c, 0xF79E)

    region_len = base_table_end - WIDGET_TABLE_START
    tbl = bytearray(region_len)
    tbl[:len(table)] = table[:region_len]
    out[WIDGET_TABLE_START:base_table_end] = tbl

    if images_dir and parent and parent.get("backgroundType") == 1 and parent.get("backgroundImagePath"):
        bgpath = os.path.join(images_dir, os.path.basename(parent["backgroundImagePath"]))
        if os.path.exists(bgpath):
            jpg = open(bgpath, "rb").read()
            base_len = struct.unpack_from(">I", base, RECORD_OFFSET)[0]
            if len(jpg) <= base_len:
                struct.pack_into(">I", out, RECORD_OFFSET, len(jpg))
                out[RECORD_OFFSET + 4: RECORD_OFFSET + 4 + len(jpg)] = jpg
                for k in range(RECORD_OFFSET + 4 + len(jpg), RECORD_OFFSET + 4 + base_len):
                    out[k] = 0
    return bytes(out)


def _compile_from_scratch(uiw, parent, images_dir, base, extra_metric_bases,
                          W, H, portrait):
    """From-scratch: render StaticText masks, honour background, look up metrics."""
    warnings = []
    # --- metric library (Number/DateTime tails by size) ---
    metric_sources = []
    if base is not None and extra_metric_bases is None:
        # caller gave only a blob; we can't derive its .ui here, so treat
        # extra_metric_bases as the authoritative (blob,ui) list when present.
        pass
    if extra_metric_bases:
        metric_sources.extend(extra_metric_bases)
    lib = build_metric_library(metric_sources) if metric_sources else {}

    has_num_dt = any(UI2BLOB.get(w["type"]) in (0x92, 0x8e) for w in uiw)
    if has_num_dt and not lib:
        warnings.append("Number/DateTime widgets present but no metric library "
                        "provided (pass extra_metric_bases=[(blob,ui),...]); "
                        "their glyph metrics will be zero.")

    # --- background ---
    bg_type = parent.get("backgroundType", 0) if parent else 0
    bg_record = b""
    bg_flag = 0x00
    framecount = 0
    bg_color565 = rgb565(parent.get("backgroundColor", "ff000000")) if parent else 0
    if bg_type == 1 and parent and parent.get("backgroundImagePath") and images_dir:
        from PIL import Image
        bgpath = os.path.join(images_dir, os.path.basename(parent["backgroundImagePath"]))
        if os.path.exists(bgpath):
            import io
            im = Image.open(bgpath).convert("RGB")
            if im.size != (W, H):
                im = im.resize((W, H))
            buf = io.BytesIO(); im.save(buf, format="JPEG")
            jpg = buf.getvalue()
            bg_record = struct.pack(">I", len(jpg)) + jpg
            bg_flag = 0x10
            framecount = 1
        else:
            warnings.append(f"backgroundImagePath not found: {bgpath}")

    # --- render StaticText masks (dedup by (text,size,bold)) ---
    # resource area begins right after the (optional) background record at 0x1000.
    res_base = RECORD_OFFSET + len(bg_record)
    mask_blobs = []          # list of (bytes) in emission order
    mask_index = {}          # (text,size,bold) -> (ptr, w, h)
    st_info = {}             # id(widget) -> (ptr, w, h)
    cursor = res_base
    for w in uiw:
        if UI2BLOB.get(w["type"]) != 0x93:
            continue
        key = (w["text"], w["fontSize"], bool(w["bold"]))
        if key not in mask_index:
            mb, mw, mh = render_text_mask(w["text"], w["fontSize"], w["bold"])
            ptr = cursor
            mask_index[key] = (ptr, mw, mh)
            mask_blobs.append(mb)
            cursor += len(mb)
        st_info[id(w)] = mask_index[key]

    # --- widget table ---
    table = bytearray()
    for i, w in enumerate(uiw, start=1):
        bt = UI2BLOB.get(w["type"])
        if bt is None:
            continue
        if bt == 0x93:
            ptr, mw, mh = st_info[id(w)]
            table += build_entry(w, i, portrait, mask_w=mw, mask_h=mh, mask_ptr=ptr)
        elif bt in (0x92, 0x8e):
            tail = _nearest_size(lib, bt, w["fontSize"], warnings)
            table += build_entry(w, i, portrait, metric_tail=tail)
        else:
            table += build_entry(w, i, portrait)

    # --- assemble ---
    # header + widget table region padded to 0x1000, then bg record, then masks.
    out = bytearray(RECORD_OFFSET)          # 0x0000 .. 0x1000, zeroed
    out[0:4] = b"\x96\x02\x00\x00"
    out[0x40] = 0x81
    out[0x4b] = 0x01 if portrait else 0x00
    struct.pack_into(">H", out, 0x47, W)
    struct.pack_into(">H", out, 0x49, H)
    struct.pack_into(">H", out, 0x4c, bg_color565)      # bg color (solid) / const
    out[0x50] = bg_flag
    struct.pack_into(">I", out, 0x54, framecount)
    out[WIDGET_TABLE_START:WIDGET_TABLE_START + len(table)] = table

    out += bg_record
    for mb in mask_blobs:
        out += mb

    content_len = len(out)
    struct.pack_into(">I", out, 0x58, content_len)
    _last_warnings.clear(); _last_warnings.extend(warnings)
    return bytes(out)


# from-scratch compile warnings are also exposed here for callers/tests
_last_warnings = []


def last_warnings():
    return list(_last_warnings)


if __name__ == "__main__":
    import sys
    from csm_panel.theme.ui_codec import decode_file
    ui = decode_file(sys.argv[1])
    base = open(sys.argv[2], "rb").read() if len(sys.argv) > 2 else None
    blob = compile_ui_to_blob(ui, sys.argv[3] if len(sys.argv) > 3 else None, base,
                              render_text=False)
    out = sys.argv[4] if len(sys.argv) > 4 else "compiled.bin"
    open(out, "wb").write(blob)
    print(f"wrote {out} ({len(blob)} bytes)")
