#!/usr/bin/env python3
"""
compile_ui_to_blob: prototype compiler for the WCH 8040 panel "theme blob".

Turns a decrypted .ui XML (+ theme images dir) into the binary theme blob the
panel consumes.

STATUS OF EACH PIECE (see SPEC.md for the full derivation):
  * descriptor / header .................. CONFIRMED (A,C exact; B consistent)
  * widget-table framing (64B entries) ... CONFIRMED (A,C exact)
  * .ui-type -> blob-type map ............ CONFIRMED (A,C exact)
  * coordinate reslice transform ......... CONFIRMED (A,C exact, byte-perfect)
  * ProgressBar(0x8b) color layout ....... CONFIRMED
  * StaticText(0x93) color+ptr layout .... CONFIRMED (ptr into 8bpp mask area)
  * Number(0x92) color + hAlign flag ..... CONFIRMED; digit-metric table INFERRED
  * DateTime(0x8e) color + format string . CONFIRMED
  * Image(0x84) ptr/framecount/delay ..... INFERRED (from B, whose blob predates
                                           its own .ui's fan, so unverified)
  * pre-rendered text/glyph resource area  CONFIRMED FORMAT (8bpp coverage mask),
                                           but SYNTHESIS is NOT done here -> we
                                           REUSE a known-good base blob's records
                                           + resource area verbatim, and only
                                           rebuild the descriptor + widget table.

Because StaticText/Number glyph bitmaps require pixel-accurate font rendering
that matches the vendor, the honest prototype path is:
  compile geometry/type/colors  ->  new widget table + descriptor
  keep records + resource area  ->  copied from base_blob_for_resources
This yields a blob whose widgets are re-placed/re-typed correctly and whose
text/number pointers still resolve (as long as the same StaticTexts exist in
the base). For a from-scratch renderer, replace _copy_resource_area().
"""
import struct
import xml.etree.ElementTree as ET

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


def ui_to_blob_xy(ux, uy, portrait):
    """Portrait 480x800 -> landscape framebuffer via 256-tall band reslice.
    bl_x = (ux mod 256) + 256*(uy//256) ; bl_y = uy mod 256.   Landscape = identity."""
    if not portrait:
        return ux, uy
    band = uy // 256
    return (ux % 256) + 256 * band, uy % 256


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
        w["fontColor"] = f.findtext("fontColor", "ffffffff") if f is not None else "ffffffff"
        w["hAlign"] = int(el.findtext("hAlign", "0"))
        st = el.find("style")
        if st is not None:
            w["fgColor"] = st.findtext("fgColor", "ff000000")
            w["bgColor"] = st.findtext("bgColor", "ff000000")
            w["frameColor"] = st.findtext("frameColor", "ff000000")
        w["imageDelay"] = int(el.findtext("imageDelay", "0"))
        w["backgroundType"] = int(el.findtext("backgroundType", "0")) if el.tag == "widgetParent" else 0
        w["backgroundImagePath"] = el.findtext("backgroundImagePath", "") or ""
        out.append(w)
    return out


# ---------------------------------------------------------------------------
# base-blob helpers (for reusing records + resource area)
# ---------------------------------------------------------------------------
def _base_widget_table(base):
    """Return list of (type,field,64B-entry) from a base blob, keyed for reuse of
    the type-specific tail (colors/ptrs/metrics) when synthesizing is too hard."""
    ents = []
    off = WIDGET_TABLE_START
    while off + ENTRY <= len(base):
        e = base[off:off + ENTRY]
        if not (0x80 <= e[0] <= 0x9f):
            break
        ents.append(bytearray(e))
        off += ENTRY
    return ents, off


# ---------------------------------------------------------------------------
# entry builders
# ---------------------------------------------------------------------------
def build_entry(w, wid, portrait, template=None):
    """Build one 64-byte widget entry.
    template: optional full 64-byte base entry of the same type. Used to lend
    glyph tails AND the rendered-bitmap w/h for StaticText/Number/DateTime, whose
    blob w/h are the pre-rendered image size (NOT the .ui bounding box)."""
    template_tail = template[12:64] if template else None
    bt = UI2BLOB[w["type"]]
    bx, by = ui_to_blob_xy(w["x"], w["y"], portrait)
    e = bytearray(64)
    e[0] = bt
    e[1] = wid & 0xFF
    # field id [2]: for data widgets == fastSensor; DateTime carries a fixed 0x15.
    e[2] = (0x15 if bt == 0x8e else w.get("fastSensor", 0)) & 0xFF
    e[3] = (w["x"] // 256) & 0xFF        # CONFIRMED: portrait x-band index
    struct.pack_into("<H", e, 4, bx & 0xFFFF)
    struct.pack_into("<H", e, 6, by & 0xFFFF)
    # For text-ish widgets the blob w/h are the *rendered bitmap* size, taken from
    # the resource, not the .ui bounding box. For ProgressBar/Image they equal the
    # .ui geometry. We fill geometry here; text widgets overwrite w/h from template.
    struct.pack_into("<H", e, 8, w["width"] & 0xFFFF)
    e[10] = w["height"] & 0xFF

    if bt == 0x8b:   # ProgressBar: [11]=1, colors bg/fg/frame RGB565 BE
        e[11] = 1
        struct.pack_into(">H", e, 12, rgb565(w.get("bgColor", "ff000000")))
        struct.pack_into(">H", e, 14, rgb565(w.get("fgColor", "ff000000")))
        struct.pack_into(">H", e, 16, rgb565(w.get("frameColor", "ff000000")))
        # [18:20] and [26:28] = value-range field (INFERRED); leave as template if given
        if template_tail:
            e[18:64] = template_tail[6:52]
    elif bt == 0x93:  # StaticText: [11]=0, ptr[12:15], fontColor565[15:17], [17]=ff
        e[11] = 0
        if template:           # keep rendered-bitmap w/h + ptr + color from base
            e[8:12] = template[8:12]
            e[12:64] = template_tail
        else:
            struct.pack_into(">H", e, 15, rgb565(w.get("fontColor")))
            e[17] = 0xFF
    elif bt == 0x92:  # Number: [11]=hAlign, fontColor565[12:14], [14:16]=00ff, glyph metrics
        e[11] = w.get("hAlign", 0) & 0xFF
        struct.pack_into(">H", e, 12, rgb565(w.get("fontColor")))
        e[14] = 0x00; e[15] = 0xFF
        if template:           # glyph-metric table (font-size dependent) + w/h from base
            e[8:12] = template[8:12]
            e[16:64] = template_tail[4:52]
    elif bt == 0x8e:  # DateTime: [11]=1, fontColor565[12:14], [14]=ff, metrics + fmt str
        e[11] = 1
        struct.pack_into(">H", e, 12, rgb565(w.get("fontColor")))
        e[14] = 0xFF
        if template:
            e[8:12] = template[8:12]
            e[15:64] = template_tail[3:52]
    elif bt == 0x84:  # Image (animation): ptr[12:15], framecount[16], delay[18:20]
        e[11] = 0
        # ptr filled by caller once frame records are laid out
        struct.pack_into(">H", e, 18, w.get("imageDelay", 0) & 0xFFFF)
    return e


# ---------------------------------------------------------------------------
# main compiler
# ---------------------------------------------------------------------------
def compile_ui_to_blob(ui_xml, images_dir, base_blob_for_resources):
    """Compile a decrypted .ui (bytes) into a theme blob (bytes).

    images_dir: theme image folder (for background JPEG). May be None to reuse the
                base blob's background.
    base_blob_for_resources: bytes of a known-good blob of the SAME resolution/
                orientation, used verbatim for the records + resource (glyph/text)
                area and as the source of per-type glyph tails.
    """
    import os
    widgets = parse_ui(ui_xml)
    parent = next((w for w in widgets if w["tag"] == "widgetParent"), None)
    uiw = [w for w in widgets if w["tag"] == "widget"]

    W = parent["width"] if parent else 480
    H = parent["height"] if parent else 800
    portrait = H > W

    base = base_blob_for_resources
    base_ents, base_table_end = _base_widget_table(base)
    # per-type pool of base tails to lend to text/number/datetime widgets in order
    from collections import defaultdict, deque
    pool = defaultdict(deque)
    for be in base_ents:
        pool[be[0]].append(bytes(be))          # full 64-byte base entry

    # ---- widget table ----
    table = bytearray()
    for i, w in enumerate(uiw, start=1):
        bt = UI2BLOB.get(w["type"])
        if bt is None:
            continue
        tmpl = pool[bt].popleft() if pool[bt] else None
        table += build_entry(w, i, portrait, tmpl)

    # ---- assemble ----
    out = bytearray(len(base))          # start from base so records/resources persist
    out[:] = base
    # rewrite header magic + descriptor
    out[0:4] = b"\x96\x02\x00\x00"
    out[0x40] = 0x81
    struct.pack_into(">H", out, 0x47, W)
    struct.pack_into(">H", out, 0x49, H)
    struct.pack_into(">H", out, 0x4c, 0xF79E)          # observed constant
    # background/record flags copied from base (0x50..0x58) already present via copy

    # overwrite widget table region with ours (pad to base_table_end with zeros)
    region_len = base_table_end - WIDGET_TABLE_START
    tbl = bytearray(region_len)
    tbl[:len(table)] = table[:region_len]
    out[WIDGET_TABLE_START:base_table_end] = tbl

    # optionally replace background JPEG record from images_dir
    if images_dir and parent and parent.get("backgroundType") == 1 and parent.get("backgroundImagePath"):
        bgpath = os.path.join(images_dir, os.path.basename(parent["backgroundImagePath"]))
        if os.path.exists(bgpath):
            jpg = open(bgpath, "rb").read()
            # NOTE: this only works cleanly if the new JPEG is <= the base's record
            # span; otherwise the whole resource area shifts and all 0x93 pointers
            # would need re-basing (not attempted in this prototype).
            base_len = struct.unpack_from(">I", base, RECORD_OFFSET)[0]
            if len(jpg) <= base_len:
                struct.pack_into(">I", out, RECORD_OFFSET, len(jpg))
                out[RECORD_OFFSET + 4: RECORD_OFFSET + 4 + len(jpg)] = jpg
                # zero the tail of the old (larger) record
                for k in range(RECORD_OFFSET + 4 + len(jpg), RECORD_OFFSET + 4 + base_len):
                    out[k] = 0

    # content length (BE32 @ 0x58) = used length; keep base's (records unchanged)
    return bytes(out)


if __name__ == "__main__":
    import sys
    from csm_panel.theme.ui_codec import decode_file
    ui = decode_file(sys.argv[1])
    base = open(sys.argv[2], "rb").read()
    blob = compile_ui_to_blob(ui, sys.argv[3] if len(sys.argv) > 3 else None, base)
    out = sys.argv[4] if len(sys.argv) > 4 else "compiled.bin"
    open(out, "wb").write(blob)
    print(f"wrote {out} ({len(blob)} bytes)")
