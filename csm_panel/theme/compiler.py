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

  render_text=True   (from-scratch authoring)
      Renders each StaticText (type 2) as an 8-bpp coverage mask (Liberation Sans,
      pixel size round(fontSize*4/3), height = ascent+descent); each Number
      (type 5) as a glyph-major 8-bpp digit strip ('0..9.-', per-glyph advance x
      strip_h) + metric tail; each Image (type 4) as raw resource pixels — opaque
      source -> RGB565 (w*h*2/frame), transparent PNG -> PLANAR alpha-plane (w*h)
      then RGB565 colour-plane (w*h*2), N frames consecutive for a folder
      animation; each DateTime (type 6) with a SAFE skeleton format as a glyph-
      major atlas ('0..9' + '.-: /' + 6 px-cells) + metric tail (BE32 ptr) + inline
      format skeleton; and each image-fill ProgressBar (type 3, showType=1 with
      bg/fg images) as two opaque RGB565 refs with the fill flag [11]=0. Multi-
      frame BACKGROUNDS embed one JPEG record per folder frame + a zero terminator,
      with the descriptor's frame count [0x52] + per-frame delay [0x56] set from
      <imageDelay>. These new resources are APPENDED after any reused base resource
      area, and each widget's resource pointer targets them. Geometry [3:11] is
      computed purely by ui_to_blob_xy for EVERY widget type (verified byte-exact
      vs the editor; the old "per-band offset" copy was a false alarm). A DateTime
      with an UNSUPPORTED/freeform format (which would crash the panel) is the only
      remaining case that REUSES the base blob's aligned entry + resource area (a
      base blob is then required). Background is honoured from <widgetParent>.

      render_text=True is HW-VALIDATED 2026-07-22: a from-scratch StaticText +
      Number + ProgressBar + image-background theme flashes and renders correctly
      (background, glyph text, digit numbers, bars). Getting there required three
      fixes found by flashing + diffing against the editor: descriptor [0x4b] must
      be 0x00 (not 0x01) and [0x53] defaults to 1 (a bogus descriptor MDT-BRICKS
      the panel into boot mode); the background JPEG must be embedded byte-for-byte
      (not re-encoded); and a StaticText's stored y/width derive from the MASK
      width only (the old code double-applied the wide-band wrap via the box
      width, rendering wide-box labels as misaligned noise). CAVEATS: DateTime is
      still reuse-only (freeform formats black-screen the panel); glyph AA is ~1px
      off the vendor rasteriser (cosmetic). Still VALIDATE a new from-scratch theme
      and keep firmware-recovery frames handy (a bad blob boot-bricks; recover via
      csm_panel.firmware.flash_raw_frames — see PROTOCOL_NOTES "recovery").

STATUS OF EACH PIECE (see SPEC.md for the full derivation):
  * descriptor / header .................. CONFIRMED (A,C exact; B consistent)
  * widget-table framing (64B entries) ... CONFIRMED (A,C exact)
  * .ui-type -> blob-type map ............ CONFIRMED (A,C exact)
  * coordinate reslice transform ......... CONFIRMED byte-perfect incl. multi-band
                                           wrap (k = uw//256 bands): uw==256 (k=1),
                                           uw=512 (k=2), uw=853 (k=3) all verified
                                           on GeometryEdges
  * ProgressBar(0x8b) color layout ....... CONFIRMED
  * StaticText(0x93) color+ptr layout .... CONFIRMED (ptr into 8bpp mask area)
  * Number(0x92) glyph strip + tail ...... CONFIRMED byte-exact (NumberMatrix):
                                           [11]=hAlign, [12:14]color, [17:20]BE24
                                           ptr to glyph-major digit strip, [20:46]
                                           strip_h+12 advances BE u16; from-scratch
                                           emission implemented (render_number_strip)
  * DateTime(0x8e) color + format ........ CONFIRMED byte-exact (DateTimeMatrix):
                                           [15:19] BE32 strip ptr, [19:21] strip_h,
                                           [21:45] 12 advances (6 glyphs + 6 px),
                                           [45:64] inline format skeleton. Glyph
                                           atlas '0..9'+'.-: /'+6 px-cells. Emitted
                                           from-scratch for the SAFE skeletons only;
                                           freeform overflows -> panel crash -> reuse
  * Image(0x84) ptr[12:15]+framecount[16] . CONFIRMED (opaque raw RGB565 w*h*2/frame;
                                           alpha PNG = PLANAR w*h alpha then w*h*2
                                           RGB565 colour; frames consecutive)
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
  * Number digit strip + tail by size .... CONFIRMED byte-exact (from-scratch)
  * DateTime metric tail / glyph strip ... PARTIAL (reuse base; see above)
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


def ui_to_blob_xy(ux, uy, uw, portrait):
    """Portrait 480x800 -> landscape framebuffer via 256-tall band reslice.

    Returns (bl_x, bl_y, bl_w). Verified byte-exact against the Neon Grid,
    BandGeomFlat/Image and GeometryEdges editor blobs:
      bl_x = (ux mod 256) + 256*(uy//256)                  # always
      k    = uw // 256                                      # bands the width spans
      bl_y = (uy mod 256) + 256*k
      bl_w = uw - 256*k
    A widget wider than one band wraps down k bands (by += 256*k) and its stored
    width drops by k bands. GeometryEdges confirmed every branch byte-exact:
    uw==256 -> k=1 (wide branch, bl_w=0); uw=512 -> k=2; uw=853 -> k=3; uw<256
    -> k=0 (identity). Landscape themes are identity."""
    if not portrait:
        return ux, uy, uw
    bl_x = (ux % 256) + 256 * (uy // 256)
    k = uw // 256
    if k:
        return bl_x, (uy % 256) + 256 * k, uw - 256 * k
    return bl_x, uy % 256, uw


# ---------------------------------------------------------------------------
# font discovery + StaticText mask rendering
# ---------------------------------------------------------------------------
_FONT_CACHE = {}

# The editor's "Arial" is Liberation Sans (metric-compatible). Verified against
# fontmatrix_editor.bin: mask height == FreeType ascent+descent at a pixel size
# of round(fontSize * 4/3) (i.e. points at 96 DPI), matching within 1-2px across
# sizes 8..48. (See docs/THEME_UNKNOWNS.md.)
_LIB_SANS = {
    (0, 0): "LiberationSans-Regular.ttf",
    (1, 0): "LiberationSans-Bold.ttf",
    (0, 1): "LiberationSans-Italic.ttf",
    (1, 1): "LiberationSans-BoldItalic.ttf",
}


def _find_font(bold, italic=0):
    name = _LIB_SANS[(1 if bold else 0, 1 if italic else 0)]
    hits = sorted(glob.glob(f"/nix/store/*/share/fonts/truetype/{name}"))
    if not hits:
        hits = sorted(glob.glob(f"/usr/share/fonts/**/{name}", recursive=True))
    if not hits:
        raise FileNotFoundError(f"could not locate {name} under /nix/store")
    return hits[0]


def _pixel_size(font_size):
    """Editor pixel size = fontSize points at 96 DPI = round(fontSize * 4/3)."""
    return max(1, round(int(font_size) * 4 / 3))


def render_text_mask(text, font_size, bold, italic=0):
    """Render `text` to an 8-bpp coverage mask (Liberation Sans / editor "Arial").

    Returns (mask_bytes, w, h): mask_bytes is w*h bytes, row-major, one coverage
    byte per pixel (0=transparent .. 255=opaque). The device colorises this with
    the entry's fontColor565.

    Sizing (verified against fontmatrix_editor.bin, within 1-2px):
      pixel size = round(fontSize * 4/3);  mask height = ascent + descent at that
      pixel size (FreeType getmetrics()); mask width = inked/advance width.
    """
    from PIL import Image, ImageDraw, ImageFont
    px = _pixel_size(font_size)
    key = (_find_font(bool(bold), bool(italic)), px)
    font = _FONT_CACHE.get(key)
    if font is None:
        font = ImageFont.truetype(key[0], px)
        _FONT_CACHE[key] = font
    if not text:
        return b"", 0, 0
    asc, desc = font.getmetrics()
    h = asc + desc                              # font-driven mask height
    # advance width of the string (inked width can be narrower, but the editor's
    # box tracks the pen advance); draw white text on black at the ascent line.
    try:
        w = int(round(font.getlength(text)))
    except AttributeError:                       # very old PIL
        w = font.getbbox(text)[2]
    if w <= 0:
        return b"", 0, 0
    tmp = Image.new("L", (w, h), 0)
    d = ImageDraw.Draw(tmp)
    d.text((0, 0), text, fill=255, font=font)
    return tmp.tobytes(), w, h


# ---------------------------------------------------------------------------
# Number(0x92) digit-glyph resource + metric tail
# ---------------------------------------------------------------------------
# The Number glyph resource ("digit strip") is a GLYPH-MAJOR 8-bpp coverage
# atlas, decoded byte-exact from NumberMatrix.bin: 12 glyphs in fixed order
#   '0','1',...,'9','.','-'
# stored consecutively, each glyph a (advance_width x strip_height) row-major
# 8-bpp block. The widget entry's [17:20] BE24 pointer targets this strip; its
# per-glyph advance widths + strip height live in the tail [20:...] as BE u16:
#   [20:22]  strip_height
#   [22:42]  advance width of each digit 0..9   (all equal, = FreeType adv('0'))
#   [42:44]  advance width of '.'  (FreeType adv('.'))
#   [44:46]  advance width of '-'  (FreeType adv('-'))
# Advances match FreeType getlength() EXACTLY at pixel size round(fontSize*4/3);
# strip_height == FreeType ascent+descent at that size (editor within +-1px).
# Entry [11] = hAlign (0 left / 1 center / 2 right); [12:14] fontColor RGB565 BE;
# [14]=0x00 [15]=0xff [16]=0x00. Entry [10] = the .ui box height (NOT strip_h).
NUMBER_GLYPHS = "0123456789.-"


def render_number_strip(font_size, bold=0, italic=0):
    """Render the Number digit strip for `font_size`.

    Returns (strip_bytes, strip_h, advances) where advances is the 12-entry list
    of per-glyph widths [d0..d9, dp, minus]. Each glyph is stored as an
    advance_width x strip_h 8-bpp coverage block, laid out glyph-major in the
    order NUMBER_GLYPHS. Matches the editor's digit strip within ~1px.
    """
    from PIL import Image, ImageDraw, ImageFont
    px = _pixel_size(font_size)
    fp = _find_font(bool(bold), bool(italic))
    font = ImageFont.truetype(fp, px)
    asc, desc = font.getmetrics()
    strip_h = asc + desc
    advances = [max(1, int(round(font.getlength(ch)))) for ch in NUMBER_GLYPHS]
    out = bytearray()
    for ch, adv in zip(NUMBER_GLYPHS, advances):
        cell = Image.new("L", (adv, strip_h), 0)
        ImageDraw.Draw(cell).text((0, 0), ch, fill=255, font=font)
        out += cell.tobytes()
    return bytes(out), strip_h, advances


def number_metric_tail(strip_h, advances):
    """Build the 52-byte Number metric tail [12:64] (minus color/ptr, which the
    entry builder fills). Returns bytes for [20:64]: strip_h + 12 advances as
    BE u16, zero-padded. (Color [12:17] + BE24 ptr [17:20] are added by the
    caller.)"""
    tail = bytearray(44)                       # [20:64] = 44 bytes
    struct.pack_into(">H", tail, 0, strip_h & 0xFFFF)
    for i, adv in enumerate(advances):         # d0..d9, dp, minus
        struct.pack_into(">H", tail, 2 + 2 * i, adv & 0xFFFF)
    return bytes(tail)


# ---------------------------------------------------------------------------
# DateTime(0x8e) glyph strip + metric tail + inline format skeleton
# ---------------------------------------------------------------------------
# DECODED byte-exact from DateTimeMatrix.bin (sizes 16/24/40, all safe formats).
# The DateTime tail differs from Number:
#   [12:14] fontColor RGB565 BE; [14]=0xff
#   [15:19] BE32 pointer to the glyph strip (NB 32-bit, not the 24-bit Number ptr)
#   [19:21] strip_height (BE16) == FreeType ascent+descent
#   [21:45] TWELVE advances (BE16): the 6 real glyphs '0' '.' '-' ':' ' ' '/'
#           followed by 6 px-wide field-slot cells (px = round(fontSize*4/3))
#   [45:64] format skeleton, NUL-terminated (freeform overruns -> panel crash)
# The glyph strip is a glyph-major 8-bpp atlas of the 10 digits '0'..'9', then
# '.', '-', ':', ' ', '/', then 6 px-wide cells — each an advance x strip_h block.
# Verified: strip width (sum of advances) == 277/419/686 for sizes 16/24/40.
DATETIME_GLYPHS = "0123456789.-: /"          # 15 glyphs, then 6 px-wide cells


def render_datetime_strip(font_size, bold=0, italic=0):
    """Render the DateTime glyph strip for `font_size`.

    Returns (strip_bytes, strip_h, advances) where advances is the 12-entry
    metric-tail list [d, dot, minus, colon, space, slash, px, px, px, px, px, px]
    (px = round(fontSize*4/3)). The strip itself is a glyph-major atlas of the 10
    digits '0'..'9' + '.' '-' ':' ' ' '/' + 6 px-wide (blank) field-slot cells,
    each glyph an advance x strip_h 8-bpp block. Matches the editor's strip width
    byte-exactly and its metric tail exactly (advances) / within ~1px (strip_h).
    """
    from PIL import Image, ImageDraw, ImageFont
    px = _pixel_size(font_size)
    fp = _find_font(bool(bold), bool(italic))
    font = ImageFont.truetype(fp, px)
    asc, desc = font.getmetrics()
    strip_h = asc + desc
    gadv = {ch: max(1, int(round(font.getlength(ch)))) for ch in DATETIME_GLYPHS}
    out = bytearray()
    # atlas: 10 digits then . - : space /  then 6 px-wide cells
    for ch in DATETIME_GLYPHS:
        adv = gadv[ch]
        cell = Image.new("L", (adv, strip_h), 0)
        ImageDraw.Draw(cell).text((0, 0), ch, fill=255, font=font)
        out += cell.tobytes()
    out += bytes(px * strip_h) * 6           # 6 blank px-wide field cells
    # metric-tail advances: digit, '.', '-', ':', ' ', '/', then 6x px
    advances = [gadv["0"], gadv["."], gadv["-"], gadv[":"], gadv[" "],
                gadv["/"]] + [px] * 6
    return bytes(out), strip_h, advances


# Safe DateTime format skeletons (a freeform format black-screens the panel).
# Map each supported <dateTimeFormat> to its inline skeleton (year=1 month=2
# day=3 hour=4 minute=5 second=6; literals verbatim). Only these are emitted.
_DATETIME_SKELETONS = {
    "yyyy-mm-dd hh:nn:ss": "1-2-3 4:5:6",
    "hh:nn:ss": "4:5:6",
    "yyyy/mm/dd": "1/2/3",
}


def datetime_format_skeleton(fmt):
    """Return the inline skeleton for a supported <dateTimeFormat>, else None.

    Only the safe built-in skeletons observed in the editor are supported; any
    other (freeform) format returns None so the caller falls back to base reuse
    rather than emit an entry that would overrun [45:64] and crash the panel."""
    return _DATETIME_SKELETONS.get((fmt or "").strip())


# ---------------------------------------------------------------------------
# Image widget resource encoding (raw pixels into the resource area)
# ---------------------------------------------------------------------------
def _image_has_alpha(im):
    """True if the source carries per-pixel transparency (-> w*h*3 alpha format)."""
    if im.mode in ("RGBA", "LA"):
        # any non-opaque pixel?
        alpha = im.getchannel("A")
        return alpha.getextrema()[0] < 255
    return im.mode == "P" and "transparency" in im.info


def _rgb565_bytes(im_rgb):
    """RGB PIL image -> LITTLE-endian RGB565, w*h*2 bytes, row-major.

    Image-resource pixels are stored little-endian (verified against the editor's
    BandGeomImage blob). NB the widget-ENTRY colors are big-endian (`>H`) — a
    separate code path; only raw image bitmaps use this."""
    raw = im_rgb.tobytes()                       # RGBRGB... 3 bytes/pixel
    out = bytearray(len(raw) // 3 * 2)
    for i in range(0, len(raw), 3):
        r, g, b = raw[i], raw[i + 1], raw[i + 2]
        v = ((r >> 3) << 11) | ((g >> 2) << 5) | (b >> 3)
        j = i // 3 * 2
        out[j] = v & 0xFF
        out[j + 1] = (v >> 8) & 0xFF
    return bytes(out)


def render_image_resource(paths, w, h):
    """Render one or more source images to the raw resource bytes for an Image
    widget scaled to (w, h).

    Returns (data, frame_count, is_static, has_alpha). Each frame is:
      opaque -> RGB565, w*h*2 bytes;
      alpha  -> PLANAR: an 8-bit ALPHA plane (w*h bytes, row-major) followed by
                an RGB565 little-endian COLOR plane (w*h*2 bytes, row-major).
    Both planes are full-width row-major (no band-slicing). Frames are stored
    consecutively; a frame is w*h*3 bytes either way. `paths` is a sorted list of
    source files (1 = static, N = animation).

    The PLANAR alpha layout is CONFIRMED byte-exact against AlphaImages.bin: the
    editor stores the whole alpha plane first, then the whole colour plane (an
    earlier per-pixel [colorLo,colorHi,alpha] interleave rendered logos invisible
    / panels mis-tinted). Opaque images are unchanged (byte-exact vs the editor).
    """
    from PIL import Image
    data = bytearray()
    has_alpha = False
    for p in paths:
        with Image.open(p) as im:
            if _image_has_alpha(im):
                has_alpha = True
                break
    for p in paths:
        with Image.open(p) as src:
            im = src.resize((w, h)) if src.size != (w, h) else src.copy()
        if has_alpha:
            im = im.convert("RGBA")
            data += im.getchannel("A").tobytes()          # alpha plane (w*h)
            data += _rgb565_bytes(im.convert("RGB"))       # colour plane (w*h*2)
        else:
            data += _rgb565_bytes(im.convert("RGB"))
    fc = len(paths)
    return bytes(data), fc, fc <= 1, has_alpha


def render_bar_image(path, w, h):
    """Render one image-fill ProgressBar image (bg or fg) to raw resource bytes.

    Bar images are OPAQUE (JPEG sources): RGB565 little-endian, w*h*2 bytes,
    row-major — the same encoding as an opaque Image widget. Returns the bytes.
    """
    from PIL import Image
    with Image.open(path) as src:
        im = src.convert("RGB")
        if im.size != (w, h):
            im = im.resize((w, h))
        return _rgb565_bytes(im)


def _resolve_image(images_dir, image_path):
    """Resolve a ./images/... path (honouring subfolders, falling back to the
    bare basename) to an absolute file, or None."""
    if not image_path or not images_dir:
        return None
    rel = image_path.replace("\\", "/")
    parts = [p for p in rel.split("/") if p not in ("", ".", "images")]
    full = os.path.join(images_dir, *parts)
    if os.path.exists(full):
        return full
    alt = os.path.join(images_dir, os.path.basename(image_path))
    return alt if os.path.exists(alt) else None


def _image_frame_paths(images_dir, image_path):
    """Resolve an <imagePath> to a sorted frame list. A path like
    ./images/WIDE_PATH/wide_path_0.jpg with siblings wide_path_1..N.jpg in the
    same folder is treated as an animation; a plain file is a single static frame."""
    if not image_path:
        return []
    rel = image_path.replace("\\", "/")
    parts = [p for p in rel.split("/") if p not in ("", ".", "images")]
    full = os.path.join(images_dir, *parts) if images_dir else image_path
    if not os.path.exists(full):
        # also try just the basename under images_dir
        alt = os.path.join(images_dir, os.path.basename(image_path)) if images_dir else None
        if alt and os.path.exists(alt):
            full = alt
        else:
            return []
    folder = os.path.dirname(full)
    base = os.path.basename(full)
    stem, ext = os.path.splitext(base)
    # animation: <prefix>_<n><ext> siblings
    import re
    m = re.match(r"^(.*?)(\d+)$", stem)
    if m:
        prefix = m.group(1)
        sibs = []
        for f in os.listdir(folder):
            fs, fe = os.path.splitext(f)
            mm = re.match(r"^(.*?)(\d+)$", fs)
            if fe.lower() == ext.lower() and mm and mm.group(1) == prefix:
                sibs.append((int(mm.group(2)), os.path.join(folder, f)))
        if len(sibs) > 1:
            sibs.sort()
            return [p for _, p in sibs]
    return [full]


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
            w["italic"] = int(f.findtext("italic", "0") or "0")
        else:
            w["fontColor"] = "ffffffff"; w["text"] = ""; w["fontSize"] = 0
            w["bold"] = 0; w["italic"] = 0
        w["imagePath"] = el.findtext("imagePath", "") or ""
        w["hAlign"] = int(el.findtext("hAlign", "0"))
        st = el.find("style")
        if st is not None:
            w["fgColor"] = st.findtext("fgColor", "ff000000")
            w["bgColor"] = st.findtext("bgColor", "ff000000")
            w["frameColor"] = st.findtext("frameColor", "ff000000")
            w["showType"] = int(st.findtext("showType", "0") or "0")
            w["bgImagePath"] = st.findtext("bgImagePath", "") or ""
            w["fgImagePath"] = st.findtext("fgImagePath", "") or ""
        else:
            w["showType"] = 0
            w["bgImagePath"] = ""
            w["fgImagePath"] = ""
        w["dateTimeFormat"] = el.findtext("dateTimeFormat", "") or ""
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
                mask_w=None, mask_h=None, mask_ptr=None, field_override=None,
                img_ptr=None, img_frames=None, img_static=None,
                num_ptr=None, num_tail=None, bar_images=None,
                dt_ptr=None, dt_tail=None):
    """Build one 64-byte widget entry.

    template   : legacy reuse path — full 64-byte base entry of same type; lends
                 glyph tails AND the rendered-bitmap w/h for text/number/datetime.
    metric_tail: from-scratch path — 52-byte [12:64] span from the metric library
                 (Number/DateTime), whose advance metrics we keep and colour we
                 overwrite.
    mask_w/h/ptr: from-scratch path — StaticText rendered-mask size + resource ptr.
    img_ptr/frames/static: from-scratch path — Image resource pointer, frame count
                 and static/anim flag.
    num_ptr/num_tail: from-scratch path — Number digit-strip resource pointer +
                 the [20:64] metric tail from number_metric_tail().
    """
    template_tail = template[12:64] if template else None
    bt = UI2BLOB[w["type"]]
    bx, by, bw = ui_to_blob_xy(w["x"], w["y"], w["width"], portrait)
    e = bytearray(64)
    e[0] = bt
    # id [1]: reuse path copies the base entry's id (the editor's own scheme,
    # which isn't always the sequential document index); from-scratch uses wid.
    e[1] = (template[1] if template is not None else wid) & 0xFF
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
    struct.pack_into("<H", e, 8, bw & 0xFFFF)
    e[10] = w["height"] & 0xFF
    # Reuse path: the aligned base entry is the authoritative geometry. For the
    # common (narrow / band-2) case ui_to_blob_xy already matches it, but for
    # widgets whose band-relative Y the editor derives from glyph/box metrics
    # (Number/DateTime, and ProgressBars/StaticText in bands above the last where
    # a per-band vertical offset appears — see docs/THEME_UNKNOWNS.md), the pure
    # transform diverges. Copying [3:11] verbatim keeps the reuse path byte-exact
    # regardless. (StaticText/Image geometry in from-scratch is (re)derived below.)
    if template is not None:
        e[3:11] = template[3:11]

    if bt == 0x8b:   # ProgressBar
        # [11] = 1 for a colour (value-driven) bar; 0 for an IMAGE-FILL bar. The
        # editor sets [11]=0 iff showType=1 AND bg/fg images are present — that is
        # the flag that distinguishes an image-fill bar from a colour bar
        # (verified: ImageBar showType=0 -> [11]=1 didn't scroll on-panel;
        # ImageBarVal showType=1 -> [11]=0 works). See docs/THEME_UNKNOWNS.md.
        struct.pack_into(">H", e, 12, rgb565(w.get("bgColor", "ff000000")))
        struct.pack_into(">H", e, 14, rgb565(w.get("fgColor", "ff000000")))
        struct.pack_into(">H", e, 16, rgb565(w.get("frameColor", "ff000000")))
        if bar_images is not None:      # from-scratch image-fill bar
            e[11] = 0
            (bgw, bgh, bgptr), (fgw, fgh, fgptr) = bar_images
            struct.pack_into(">H", e, 18, bgw & 0xFFFF)
            struct.pack_into(">H", e, 20, bgh & 0xFFFF)
            struct.pack_into(">I", e, 22, bgptr & 0xFFFFFFFF)
            struct.pack_into(">H", e, 26, fgw & 0xFFFF)
            struct.pack_into(">H", e, 28, fgh & 0xFFFF)
            struct.pack_into(">I", e, 30, fgptr & 0xFFFFFFFF)
        elif template_tail:            # reuse: keep recomputed colours [12:18],
            e[11] = template[11]       # copy the rest incl. [11] (0=image / 1=colour)
            e[18:64] = template_tail[6:52]
        else:
            e[11] = 1
    elif bt == 0x93:  # StaticText
        e[11] = 0
        if template:                       # legacy: reuse mask + w/h + color
            e[8:12] = template[8:12]
            e[12:64] = template_tail
        elif mask_ptr is not None:         # from-scratch: our rendered mask
            # The stored w/h are the MASK dimensions (not the .ui box), and the
            # portrait wide-transform applies to the mask width too: a mask wider
            # than one band stores w-256*k and its band-relative y gets +256*k
            # (verified on GeometryEdges' 853-px WIDE_STATIC_TEXT: k = w//256).
            # y + width come from the MASK width, NOT the .ui box width. Compute
            # from the raw uy so we don't double-apply the wide wrap that the
            # box-width `by` (line ~484) already carries (that double-count pushed
            # every wide-box StaticText a band down -> the panel read the mask
            # misaligned as noise). k is the mask's band span.
            k = (mask_w // 256) if portrait else 0
            sy = (w["y"] % 256) + 256 * k
            sw = mask_w - 256 * k
            struct.pack_into("<H", e, 6, sy & 0xFFFF)
            struct.pack_into("<H", e, 8, sw & 0xFFFF)
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
        if num_ptr is not None:            # from-scratch: rendered digit strip
            # geometry [3:11] already set from ui_to_blob_xy above.
            struct.pack_into(">H", e, 12, rgb565(w.get("fontColor")))
            e[14] = 0x00; e[15] = 0xFF; e[16] = 0x00
            e[17] = (num_ptr >> 16) & 0xFF
            e[18] = (num_ptr >> 8) & 0xFF
            e[19] = num_ptr & 0xFF
            e[20:64] = num_tail            # strip_h + 12 advances (BE u16)
        else:
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
        # [11] = hAlign (0 left / 2 right — DateTime's right-align renders on-panel).
        e[11] = w.get("hAlign", 0) & 0xFF
        if dt_ptr is not None:              # from-scratch: rendered glyph strip
            # geometry [3:11] already set from ui_to_blob_xy above.
            strip_h, advances, skel = dt_tail
            struct.pack_into(">H", e, 12, rgb565(w.get("fontColor")))
            e[14] = 0xFF
            struct.pack_into(">I", e, 15, dt_ptr & 0xFFFFFFFF)   # BE32 strip ptr
            struct.pack_into(">H", e, 19, strip_h & 0xFFFF)
            for k, adv in enumerate(advances):                   # 12 BE16 advances
                struct.pack_into(">H", e, 21 + 2 * k, adv & 0xFFFF)
            fb = skel.encode("latin1")[:18]                      # NUL-terminated in [45:64]
            e[45:45 + len(fb)] = fb
        else:
            e[11] = 1 if template is None else e[11]
            tail = template[12:64] if template else metric_tail
            if tail:
                e[8:12] = (template[8:12] if template else e[8:12])
                e[12:64] = tail
                struct.pack_into(">H", e, 12, rgb565(w.get("fontColor")))
                e[14] = 0xFF
            else:
                struct.pack_into(">H", e, 12, rgb565(w.get("fontColor")))
                e[14] = 0xFF
    elif bt == 0x84:  # Image (static or animation)
        e[11] = 0
        if template:                        # reuse: copy resource pointer [12:15],
            e[12:64] = template_tail         # frame count [16] and static/anim flag [17]
        elif img_ptr is not None:           # from-scratch: our raw pixel resource
            # For Image the stored w/h are the DISPLAY size (untransformed source
            # dims); the wide-transform still applies to [8:10] like other widgets
            # (it wraps the bl_x band) but the resource is w*h at the full width.
            e[12] = (img_ptr >> 16) & 0xFF
            e[13] = (img_ptr >> 8) & 0xFF
            e[14] = img_ptr & 0xFF
            e[15] = 0x00
            e[16] = (img_frames or 1) & 0xFF     # frame count (1 = static)
            e[17] = 0x01 if img_static else 0x00  # static/anim flag
    return e


# ---------------------------------------------------------------------------
# main compiler
# ---------------------------------------------------------------------------
def compile_ui_to_blob(ui_xml, images_dir=None, base_blob_for_resources=None,
                       render_text=True, extra_metric_bases=None):
    """Compile a decrypted .ui (bytes) into a theme blob (bytes).

    render_text=False : legacy reuse path (byte-exact self-compile). Requires
                        base_blob_for_resources; reuses its records + resources.
    render_text=True  : from-scratch authoring. StaticText -> rendered 8-bpp
                        coverage masks; Image -> raw RGB565 / RGB565+alpha pixels;
                        both laid out in a fresh resource area APPENDED after the
                        base's. Number/DateTime digit glyphs (undecoded format)
                        and their geometry/metric tails are REUSED from the base
                        blob's aligned entries + resource area (kept intact at
                        their original offsets). Background is honoured from
                        <widgetParent>, or retained from the base when it carries
                        the Number resource area.

    images_dir : theme image folder (StaticText none; Image + background pixels).
    base_blob_for_resources : a known-good same-resolution blob. Legacy mode: the
                        whole resource area. From-scratch mode: supplies the
                        Number/DateTime digit-glyph resource area + their aligned
                        entry geometry/tails (required when the .ui has any
                        Number/DateTime widget).
    extra_metric_bases : accepted for backwards compatibility; no longer used
                        (from-scratch aligns Number/DateTime entries against the
                        base blob directly).
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
    """From-scratch authoring.

    StaticText -> rendered 8-bpp coverage masks; Number -> rendered digit-strip
    (glyph-major 8-bpp atlas '0..9.-') + metric tail; Image -> raw RGB565 (opaque)
    or RGB565+alpha (transparent) pixels; all laid out in a fresh resource area.
    Geometry [3:11] is computed with the pure ui_to_blob_xy transform for EVERY
    widget type (verified byte-exact vs the editor on BandGeom* / GeometryEdges;
    the old "per-band offset" workaround was a false alarm from a stale fixture).

    DateTime (0x8e) glyphs+format skeleton are only PARTIALLY decoded (the format
    template is packed inline in the tail [45:64] and freeform formats overflow /
    crash the panel), so DateTime still REUSES the base blob's aligned entry +
    resource area verbatim; a base blob is required only when DateTime is present.
    New StaticText/Number/Image resources are APPENDED after the (possibly reused)
    base resource area so base DateTime pointers stay valid.
    """
    warnings = []
    types = {UI2BLOB.get(w["type"]) for w in uiw}
    has_datetime = 0x8e in types

    # Pool of base entries by type, popped in document order to align each
    # DateTime widget with its editor entry (same order the reuse path uses).
    base_pool = defaultdict(deque)
    base_res = b""            # base records + resource area (0x1000..content_len)
    if base is not None:
        _bents, _ = _base_widget_table(base)
        for be in _bents:
            base_pool[be[0]].append(bytes(be))
        base_content_len = struct.unpack_from(">I", base, 0x58)[0]
        base_res = base[RECORD_OFFSET:base_content_len]
    # DateTime with a SAFE skeleton format is emitted from scratch (glyph strip +
    # metric tail + inline format). A DateTime with an unsupported/freeform format
    # can only fall back to a reused base entry; without a base it renders nothing.
    if has_datetime and base is None:
        for w in uiw:
            if UI2BLOB.get(w["type"]) == 0x8e \
                    and datetime_format_skeleton(w.get("dateTimeFormat", "")) is None:
                warnings.append(
                    f"DateTime format {w.get('dateTimeFormat','')!r} is not a "
                    f"supported safe skeleton and no base blob was given; that "
                    f"widget cannot be emitted (a freeform format crashes the panel).")

    # --- background ---
    # When we reuse the base resource area (DateTime present) the base already
    # carries the background JPEG record at 0x1000; keep it (and only swap it for a
    # solid color if the .ui has no background image). Otherwise build a
    # single-frame background record from the .ui.
    bg_type = parent.get("backgroundType", 0) if parent else 0
    bg_color565 = rgb565(parent.get("backgroundColor", "ff000000")) if parent else 0
    # Only fall back to reusing the base resource area (its bg record + glyph
    # tables) when a DateTime widget uses an UNSUPPORTED format that we can't emit
    # from scratch. A DateTime with a supported safe skeleton is fully synthesised,
    # so the base is not needed and the .ui background is honoured normally.
    needs_base_datetime = base is not None and any(
        UI2BLOB.get(w["type"]) == 0x8e
        and datetime_format_skeleton(w.get("dateTimeFormat", "")) is None
        for w in uiw)
    reuse_base_res = needs_base_datetime

    own_bg_record = b""
    own_bg_flag = 0x00
    own_framecount = 0
    own_bg_delay = 0
    if not reuse_base_res:
        if bg_type == 1 and parent and parent.get("backgroundImagePath") and images_dir:
            from PIL import Image
            import io
            # Resolve the bg to a sorted frame list. A folder animation
            # (./images/BGA/anim_0.jpg with numeric siblings) yields N frames — the
            # editor stores each as its own JPEG record at 0x1000, in order,
            # terminated by a zero-size record (verified: AnimatedBg.bin = 4 JPEG
            # records + terminator). A single file yields one static record.
            bg_paths = _image_frame_paths(images_dir, parent["backgroundImagePath"])
            if not bg_paths:
                warnings.append(
                    f"backgroundImagePath not found: {parent['backgroundImagePath']}")
            else:
                recs = bytearray()
                for bgpath in bg_paths:
                    with open(bgpath, "rb") as _bf:
                        raw = _bf.read()
                    with Image.open(bgpath) as probe:
                        fmt, size = probe.format, probe.size
                    if fmt == "JPEG" and size == (W, H) and raw[:2] == b"\xff\xd8":
                        jpg = raw              # embed source JPEG byte-for-byte
                    else:                      # re-encode only if wrong format/size
                        im = Image.open(bgpath).convert("RGB")
                        if im.size != (W, H):
                            im = im.resize((W, H))
                        buf = io.BytesIO()
                        im.save(buf, format="JPEG", subsampling=2, quality=90,
                                progressive=False)
                        jpg = buf.getvalue()
                    recs += struct.pack(">I", len(jpg)) + jpg
                own_framecount = len(bg_paths)
                if own_framecount > 1:
                    recs += b"\x00\x00\x00\x00"     # zero-size terminator
                    own_bg_delay = parent.get("imageDelay", 0) & 0xFF
                own_bg_record = bytes(recs)
                own_bg_flag = 0x10

    # The resource area we start from (base's records+resources, or our own bg).
    start_res = base_res if reuse_base_res else own_bg_record
    res_base = RECORD_OFFSET + len(start_res)   # where NEW resources begin

    # --- render StaticText masks (dedup by identical (text,size,bold,italic)) ---
    appended = bytearray()
    cursor = res_base
    mask_index = {}          # (text,size,bold,italic) -> (ptr, w, h)
    st_info = {}             # id(widget) -> (ptr, w, h)
    for w in uiw:
        if UI2BLOB.get(w["type"]) != 0x93:
            continue
        key = (w["text"], w["fontSize"], bool(w["bold"]), bool(w.get("italic", 0)))
        if key not in mask_index:
            mb, mw, mh = render_text_mask(w["text"], w["fontSize"], w["bold"],
                                          w.get("italic", 0))
            mask_index[key] = (cursor, mw, mh)
            appended += mb
            cursor += len(mb)
        st_info[id(w)] = mask_index[key]

    # --- render Image resources (dedup by (imagePath, w, h)) ---
    img_index = {}           # (imagePath, w, h) -> (ptr, frames, static)
    im_info = {}             # id(widget) -> (ptr, frames, static)
    for w in uiw:
        if UI2BLOB.get(w["type"]) != 0x84:
            continue
        key = (w.get("imagePath", ""), w["width"], w["height"])
        if key not in img_index:
            paths = _image_frame_paths(images_dir, w.get("imagePath", ""))
            if not paths:
                warnings.append(f"image not found: {w.get('imagePath','')}")
                img_index[key] = (0, 1, True)
            else:
                data, fc, static, has_a = render_image_resource(
                    paths, w["width"], w["height"])
                img_index[key] = (cursor, fc, static)
                appended += data
                cursor += len(data)
        im_info[id(w)] = img_index[key]

    # --- render Number digit strips (dedup by (fontSize, bold, italic)) ---
    num_index = {}           # (fontSize,bold,italic) -> (ptr, tail_bytes)
    num_info = {}            # id(widget) -> (ptr, tail_bytes)
    for w in uiw:
        if UI2BLOB.get(w["type"]) != 0x92:
            continue
        key = (w["fontSize"], bool(w["bold"]), bool(w.get("italic", 0)))
        if key not in num_index:
            strip, strip_h, advances = render_number_strip(
                w["fontSize"], w["bold"], w.get("italic", 0))
            tail = number_metric_tail(strip_h, advances)
            num_index[key] = (cursor, tail)
            appended += strip
            cursor += len(strip)
        num_info[id(w)] = num_index[key]

    # --- render image-fill ProgressBar images (bg + fg raw RGB565) ---
    # An image-fill bar is a 0x8b whose .ui <style> has showType=1 and both
    # bgImagePath + fgImagePath. Its resource holds two opaque RGB565 blocks (bg
    # then fg); the entry stores each as a w(BE16) h(BE16) ptr(BE32) triple. The
    # fg image MUST be shorter (narrower) than the bg image for the auto-scroll
    # fill animation to work (verified: ImageBarVal fg 167 < bg 300 scrolls;
    # ImageBar's equal-width fg didn't). See docs/THEME_UNKNOWNS.md.
    bar_info = {}            # id(widget) -> ((bgw,bgh,bgptr),(fgw,fgh,fgptr)) or None
    for w in uiw:
        if UI2BLOB.get(w["type"]) != 0x8b:
            continue
        if not (w.get("showType") == 1 and w.get("bgImagePath")
                and w.get("fgImagePath")):
            bar_info[id(w)] = None                 # colour bar
            continue
        bgp = _resolve_image(images_dir, w["bgImagePath"])
        fgp = _resolve_image(images_dir, w["fgImagePath"])
        if not bgp or not fgp:
            warnings.append(f"image-fill bar image not found: "
                            f"{w['bgImagePath']} / {w['fgImagePath']}")
            bar_info[id(w)] = None
            continue
        from PIL import Image
        with Image.open(bgp) as im:
            bgw, bgh = im.size
        with Image.open(fgp) as im:
            fgw, fgh = im.size
        if fgw >= bgw:
            warnings.append(f"image-fill bar fg ({fgw}px) not shorter than bg "
                            f"({bgw}px); auto-scroll fill will not animate")
        bg_ptr = cursor
        appended += render_bar_image(bgp, bgw, bgh)
        cursor += bgw * bgh * 2
        fg_ptr = cursor
        appended += render_bar_image(fgp, fgw, fgh)
        cursor += fgw * fgh * 2
        bar_info[id(w)] = ((bgw, bgh, bg_ptr), (fgw, fgh, fg_ptr))

    # --- render DateTime glyph strips (dedup by (fontSize, bold, italic)) ---
    dt_index = {}            # (fontSize,bold,italic) -> (ptr, strip_h, advances)
    dt_info = {}             # id(widget) -> (ptr, strip_h, advances)
    for w in uiw:
        if UI2BLOB.get(w["type"]) != 0x8e:
            continue
        key = (w["fontSize"], bool(w["bold"]), bool(w.get("italic", 0)))
        if key not in dt_index:
            strip, strip_h, advances = render_datetime_strip(
                w["fontSize"], w["bold"], w.get("italic", 0))
            dt_index[key] = (cursor, strip_h, advances)
            appended += strip
            cursor += len(strip)
        dt_info[id(w)] = dt_index[key]

    # --- widget table ---
    table = bytearray()
    for i, w in enumerate(uiw, start=1):
        bt = UI2BLOB.get(w["type"])
        if bt is None:
            continue
        if bt == 0x93:
            ptr, mw, mh = st_info[id(w)]
            table += build_entry(w, i, portrait, mask_w=mw, mask_h=mh, mask_ptr=ptr)
        elif bt == 0x84:
            ptr, fc, static = im_info[id(w)]
            table += build_entry(w, i, portrait, img_ptr=ptr, img_frames=fc,
                                 img_static=static)
        elif bt == 0x92:  # Number — synthesised digit strip + metric tail
            ptr, tail = num_info[id(w)]
            table += build_entry(w, i, portrait, num_ptr=ptr, num_tail=tail)
        elif bt == 0x8e:  # DateTime
            skel = datetime_format_skeleton(w.get("dateTimeFormat", ""))
            if skel is not None:
                # From-scratch: synthesised glyph strip + metric tail + inline
                # format skeleton. ONLY the safe skeleton formats are emitted; a
                # freeform format returns None and falls back to base reuse (a
                # wrong format black-screens the panel — see docs/THEME_UNKNOWNS.md).
                ptr, strip_h, advances = dt_info[id(w)]
                table += build_entry(w, i, portrait, dt_ptr=ptr,
                                     dt_tail=(strip_h, advances, skel))
            else:
                # Unsafe/unknown format — reuse the aligned base entry verbatim
                # (its geometry, glyph tail and format skeleton stay valid).
                tmpl = base_pool[bt].popleft() if base_pool[bt] else None
                if tmpl is None:
                    warnings.append(
                        f"DateTime widget #{i} has an unsupported format "
                        f"{w.get('dateTimeFormat','')!r} and no base blob to "
                        f"reuse; emitting geometry-only entry (WILL NOT render)")
                    table += build_entry(w, i, portrait)
                else:
                    e = bytearray(build_entry(w, i, portrait, template=tmpl,
                                              field_override=tmpl[2]))
                    e[1] = i & 0xFF
                    e[2] = 0x15
                    table += bytes(e)
        elif bt == 0x8b:  # ProgressBar — colour (pure transform) or image-fill
            table += build_entry(w, i, portrait, bar_images=bar_info.get(id(w)))
        else:
            table += build_entry(w, i, portrait)

    # --- assemble ---
    out = bytearray(RECORD_OFFSET)          # 0x0000 .. 0x1000, zeroed
    out[0:4] = b"\x96\x02\x00\x00"
    out[1] = 0x02                           # orientation: upright
    out[0x40] = 0x81
    out[0x4b] = 0x00                        # editor uses 0x00 for portrait (NOT 0x01)
    struct.pack_into(">H", out, 0x47, W)
    struct.pack_into(">H", out, 0x49, H)
    if reuse_base_res:
        # keep the base's descriptor bg color / flag / framecount (its bg record
        # is retained); copy [0x4c..0x58) so the pointers stay consistent.
        out[0x4b] = base[0x4b]
        out[0x53] = base[0x53]
        out[0x4c:0x58] = base[0x4c:0x58]
    else:
        struct.pack_into(">H", out, 0x4c, bg_color565 or 0xF79E)
        out[0x50] = own_bg_flag
        # Descriptor frame fields (decoded from AnimatedBg.bin vs static blobs):
        #   [0x52:0x54] BE16 = background frame count (1 static, N animated)
        #   [0x53]      = same count's low byte (editor mirrors it here)
        #   [0x54:0x58] BE32 = 0x0000<delay><01>: [0x56]=per-frame delay (ms/tick,
        #                from <imageDelay>), [0x57]=0x01 constant. Static -> delay 0.
        bg_frames = max(1, own_framecount)
        struct.pack_into(">H", out, 0x52, bg_frames & 0xFFFF)
        out[0x53] = max([bg_frames] + [v[1] for v in img_index.values()]) & 0xFF
        out[0x54] = 0x00
        out[0x55] = 0x00
        out[0x56] = own_bg_delay & 0xFF
        out[0x57] = 0x01
    out[WIDGET_TABLE_START:WIDGET_TABLE_START + len(table)] = table

    out += start_res
    out += appended

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
