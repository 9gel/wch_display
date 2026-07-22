"""Unit tests for csm_panel.theme.compiler.

Run with the project venv and stdlib unittest:

    .venv/bin/python -m unittest discover -s tests -v

Fixtures live in tests/fixtures/ (the two decoded .ui.xml + the 72 KB NeonGrid
editor blob are committed). The 3.5 MB SysStatus editor blob and the NAS theme
image folder are large / machine-local, so the from-scratch tests that need them
are gated on presence and SKIP when absent (they never hard-fail CI).
"""
import os
import struct
import unittest

from csm_panel.theme import compiler as C

HERE = os.path.dirname(os.path.abspath(__file__))
FIX = os.path.join(HERE, "fixtures")

# Optional large/local inputs (env override, else conventional locations).
SYS_BLOB = os.environ.get("SYSSTATUS_BLOB",
                          os.path.join(FIX, "sysstatus_editor.bin"))
SYS_IMAGES = os.environ.get("SYSSTATUS_IMAGES",
                            "/home/nigel/nas/Kiosk LCD Panel/theme_SysStatus/images")


def _read(p):
    with open(p, "rb") as f:
        return f.read()


def _table(b):
    ents, off = [], 0x80
    while off + 64 <= len(b):
        e = b[off:off + 64]
        if not (0x80 <= e[0] <= 0x9f):
            break
        ents.append(e)
        off += 64
    return ents


def _geom(e):
    return (e[3], struct.unpack_from("<H", e, 4)[0], struct.unpack_from("<H", e, 6)[0],
            struct.unpack_from("<H", e, 8)[0], e[10])


def _ptr(e):
    return (e[12] << 16) | (e[13] << 8) | e[14]


def _has_font():
    try:
        C._find_font(False, False)
        return True
    except Exception:
        return False


# ---------------------------------------------------------------------------
class TestPureHelpers(unittest.TestCase):
    def test_rgb565_known(self):
        # f1f1f1 rounds to the vendor background constant 0xf79e
        self.assertEqual(C.rgb565("fff1f1f1"), 0xF79E)
        self.assertEqual(C.rgb565("ff000000"), 0x0000)
        self.assertEqual(C.rgb565("ffffffff"), 0xFFFF)
        self.assertEqual(C.rgb565("ffff0000"), 0xF800)  # pure red
        self.assertEqual(C.rgb565("ff00ff00"), 0x07E0)  # pure green
        self.assertEqual(C.rgb565("ff0000ff"), 0x001F)  # pure blue
        self.assertEqual(C.rgb565(""), 0)               # empty -> 0

    def test_ui_to_blob_xy_narrow(self):
        # narrow (uw<=256): bl_x = ux%256 + 256*(uy//256); bl_y = uy%256; bl_w=uw
        # verified samples (NeonGrid numbers, docs/THEME_UNKNOWNS.md)
        self.assertEqual(C.ui_to_blob_xy(338, 80, 60, True), (82, 80, 60))
        self.assertEqual(C.ui_to_blob_xy(40, 264, 60, True), (296, 8, 60))

    def test_ui_to_blob_xy_wide(self):
        # wide (uw>256): bl_y = uy%256 + 256; bl_w = uw-256 (single band-cross)
        self.assertEqual(C.ui_to_blob_xy(40, 122, 300, True), (40, 378, 44))
        self.assertEqual(C.ui_to_blob_xy(40, 306, 300, True), (296, 306, 44))

    def test_ui_to_blob_xy_landscape_identity(self):
        self.assertEqual(C.ui_to_blob_xy(123, 456, 300, False), (123, 456, 300))
        self.assertEqual(C.ui_to_blob_xy(0, 0, 60, False), (0, 0, 60))


    def test_ui_to_blob_xy_multiband(self):
        # GeometryEdges edge cases: k = uw // 256 bands subtracted (byte-exact).
        self.assertEqual(C.ui_to_blob_xy(40, 40, 256, True), (40, 296, 0))    # w==256 -> k=1
        self.assertEqual(C.ui_to_blob_xy(40, 120, 512, True), (40, 632, 0))   # k=2
        self.assertEqual(C.ui_to_blob_xy(40, 170, 600, True), (40, 682, 88))  # k=2
        self.assertEqual(C.ui_to_blob_xy(250, 300, 300, True), (506, 300, 44))


# ---------------------------------------------------------------------------
@unittest.skipUnless(_has_font(), "Liberation Sans not found under /nix/store")
class TestRenderTextMask(unittest.TestCase):
    def test_height_follows_ascent_descent_rule(self):
        from PIL import ImageFont
        for size in (8, 12, 16, 24, 28, 32):
            px = C._pixel_size(size)
            asc, desc = ImageFont.truetype(C._find_font(False, False), px).getmetrics()
            _, w, h = C.render_text_mask("Ag0.9", size, 0, 0)
            self.assertEqual(h, asc + desc, f"size {size}")
            self.assertGreater(w, 0, f"size {size}")

    def test_pixel_size_rule(self):
        self.assertEqual(C._pixel_size(24), 32)   # round(24*4/3)
        self.assertEqual(C._pixel_size(28), 37)
        self.assertEqual(C._pixel_size(40), 53)

    def test_mask_is_8bpp_coverage(self):
        mb, w, h = C.render_text_mask("kbps", 28, 0, 0)
        self.assertEqual(len(mb), w * h)          # w*h bytes, row-major
        self.assertLessEqual(max(mb), 255)
        self.assertGreaterEqual(min(mb), 0)
        self.assertGreater(max(mb), 0)            # something got inked

    def test_empty_text(self):
        self.assertEqual(C.render_text_mask("", 24, 0, 0), (b"", 0, 0))


# ---------------------------------------------------------------------------
class TestReusePathByteExact(unittest.TestCase):
    """Regression guard: render_text=False must reproduce editor blobs exactly."""

    def test_neongrid_byte_exact(self):
        ui = _read(os.path.join(FIX, "NeonGrid.ui.xml"))
        ed = _read(os.path.join(FIX, "neon_editor.bin"))
        out = C.compile_ui_to_blob(ui, None, ed, render_text=False)
        self.assertEqual(len(out), len(ed))
        ndiff = sum(1 for a, b in zip(out, ed) if a != b)
        self.assertEqual(ndiff, 0, f"{ndiff} differing bytes")

    @unittest.skipUnless(os.path.exists(SYS_BLOB), "sysstatus_editor.bin not present")
    def test_sysstatus_byte_exact(self):
        ui = _read(os.path.join(FIX, "SysStatus.ui.xml"))
        ed = _read(SYS_BLOB)
        out = C.compile_ui_to_blob(ui, None, ed, render_text=False)
        self.assertEqual(len(out), len(ed))
        ndiff = sum(1 for a, b in zip(out, ed) if a != b)
        self.assertEqual(ndiff, 0, f"{ndiff} differing bytes")


# ---------------------------------------------------------------------------
class TestFromScratchGeometryByteExact(unittest.TestCase):
    """The from-scratch geometry [3:11] must match the editor byte-for-byte for
    ProgressBar/Number, computed purely by ui_to_blob_xy (no base-copy). Verified
    against the freshly-paired BandGeom* / GeometryEdges editor blobs. This is the
    correction of the old (false-alarm) "per-band vertical offset"."""

    def _check(self, name):
        ui = _read(os.path.join(FIX, name + ".ui.xml"))
        ed = _read(os.path.join(FIX, name + ".bin"))
        uiw = [w for w in C.parse_ui(ui) if w["tag"] == "widget"]
        et = _table(ed)
        self.assertEqual(len(et), len(uiw))
        for i, (e, w) in enumerate(zip(et, uiw)):
            bt = C.UI2BLOB[w["type"]]
            if bt not in (0x8b, 0x92):     # bars + numbers use the pure transform
                continue
            bx, by, bw = C.ui_to_blob_xy(w["x"], w["y"], w["width"], True)
            pred = (w["x"] // 256, bx, by, bw, w["height"] & 0xFF)
            self.assertEqual(_geom(e), pred,
                             f"{name} widget #{i} type {hex(bt)} geometry")

    def test_bandgeom_flat(self):
        self._check("BandGeomFlat")

    def test_bandgeom_image(self):
        self._check("BandGeomImage")

    def test_geometry_edges(self):
        self._check("GeometryEdges")


# ---------------------------------------------------------------------------
@unittest.skipUnless(_has_font(), "Liberation Sans not found")
class TestNumberGlyphs(unittest.TestCase):
    """Decoded Number(0x92) digit-strip + metric tail, verified against
    NumberMatrix.bin (Numbers at sizes 8..64 + hAlign/isDiv1204 variants)."""

    @classmethod
    def setUpClass(cls):
        cls.ed = _read(os.path.join(FIX, "NumberMatrix.bin"))
        cls.et = _table(cls.ed)
        # NumberMatrix .ui doc order: sizes 8,12,16,20,24,32,40,48,64, then 28x5.
        cls.sizes = [8, 12, 16, 20, 24, 32, 40, 48, 64, 28, 28, 28, 28, 28]

    def _ed_tail(self, e):
        # strip_h + 12 advances (BE u16) at [20:46]
        return [struct.unpack_from(">H", e, o)[0] for o in range(20, 46, 2)]

    def test_advances_match_editor_exactly(self):
        for e, sz in zip(self.et, self.sizes):
            self.assertEqual(e[0], 0x92)
            _, strip_h, adv = C.render_number_strip(sz, 0, 0)
            ed = self._ed_tail(e)
            ed_adv = ed[1:13]
            self.assertEqual(adv, ed_adv, f"size {sz} advances")

    def test_strip_height_within_1px(self):
        for e, sz in zip(self.et, self.sizes):
            _, strip_h, _ = C.render_number_strip(sz, 0, 0)
            ed_h = self._ed_tail(e)[0]
            self.assertLessEqual(abs(strip_h - ed_h), 1, f"size {sz} strip_h")

    def test_strip_bytes_length_matches_span(self):
        # Each glyph is advance_w x strip_h; total strip == sum(adv)*strip_h.
        for e, sz in zip(self.et, self.sizes):
            strip, strip_h, adv = C.render_number_strip(sz, 0, 0)
            self.assertEqual(len(strip), sum(adv) * strip_h, f"size {sz}")

    def test_tail_builder_layout(self):
        strip, strip_h, adv = C.render_number_strip(28, 0, 0)
        tail = C.number_metric_tail(strip_h, adv)
        self.assertEqual(len(tail), 44)                       # [20:64]
        self.assertEqual(struct.unpack_from(">H", tail, 0)[0], strip_h)
        got_adv = [struct.unpack_from(">H", tail, 2 + 2 * i)[0] for i in range(12)]
        self.assertEqual(got_adv, adv)


# ---------------------------------------------------------------------------
@unittest.skipUnless(_has_font(), "Liberation Sans not found")
class TestNumberMatrixFromScratch(unittest.TestCase):
    """A from-scratch (render_text=True) compile of NumberMatrix (no base blob
    needed — no DateTime) must produce valid, correctly-dimensioned Numbers whose
    digit strips fit in the blob and whose geometry/tail match the editor."""

    @classmethod
    def setUpClass(cls):
        cls.ui = _read(os.path.join(FIX, "NumberMatrix.ui.xml"))
        cls.ed = _read(os.path.join(FIX, "NumberMatrix.bin"))
        cls.out = C.compile_ui_to_blob(cls.ui, images_dir=None,
                                       base_blob_for_resources=None,
                                       render_text=True)
        cls.uiw = [w for w in C.parse_ui(cls.ui) if w["tag"] == "widget"]
        cls.ot = _table(cls.out)
        cls.et = _table(cls.ed)
        cls.clen = struct.unpack_from(">I", cls.out, 0x58)[0]

    def test_descriptor_and_content_len(self):
        self.assertEqual(self.out[:4], b"\x96\x02\x00\x00")
        self.assertEqual(self.out[0x40], 0x81)
        self.assertEqual(self.clen, len(self.out))

    def test_one_number_entry_per_widget(self):
        self.assertEqual(len(self.ot), len(self.uiw))
        for e in self.ot:
            self.assertEqual(e[0], 0x92)

    def test_geometry_matches_editor(self):
        for o, e in zip(self.ot, self.et):
            self.assertEqual(_geom(o), _geom(e))

    def test_halign_byte_matches_editor(self):
        for o, e in zip(self.ot, self.et):
            self.assertEqual(o[11], e[11], "hAlign byte [11]")

    def test_strips_in_bounds_and_sized(self):
        res_start = 0x1000
        for o in self.ot:
            p = (o[17] << 16) | (o[18] << 8) | o[19]
            strip_h = struct.unpack_from(">H", o, 20)[0]
            adv = [struct.unpack_from(">H", o, 22 + 2 * i)[0] for i in range(12)]
            self.assertTrue(res_start <= p < self.clen, "strip ptr in resource area")
            self.assertLessEqual(p + sum(adv) * strip_h, self.clen,
                                 "strip fits in blob")

    def test_tail_metrics_match_editor(self):
        # our advances match the editor exactly; strip_h within 1px.
        for o, e in zip(self.ot, self.et):
            o_adv = [struct.unpack_from(">H", o, 22 + 2 * i)[0] for i in range(12)]
            e_adv = [struct.unpack_from(">H", e, 22 + 2 * i)[0] for i in range(12)]
            self.assertEqual(o_adv, e_adv)


# ---------------------------------------------------------------------------
class TestDocumentedByteFacts(unittest.TestCase):
    """Guards for the secondary decoded facts (DateTime align/format, underline
    no-op) so the docs and code stay honest."""

    def test_datetime_align_and_field(self):
        # DateTime entry: [2]=0x15 fixed field id, [11]=hAlign (2=right).
        et = _table(_read(os.path.join(FIX, "TextStylesDateTime.bin")))
        dt = [e for e in et if e[0] == 0x8e]
        self.assertEqual(len(dt), 1)
        self.assertEqual(dt[0][2], 0x15)
        self.assertEqual(dt[0][11], 2)          # .ui hAlign=2 (right)

    def test_datetime_format_skeleton_inline(self):
        # yyyy-mm-dd hh:nn:ss is stored inline as "1-2-3 4:5:6" in the tail.
        et = _table(_read(os.path.join(FIX, "TextStylesDateTime.bin")))
        dt = [e for e in et if e[0] == 0x8e][0]
        self.assertIn(b"1-2-3 4:5:6", bytes(dt))

    def test_badformat_overflows_tail(self):
        # The crashing freeform format overruns the 19-byte skeleton region: the
        # entry's last byte is non-zero (skeleton spilled to the entry boundary),
        # unlike the safe theme whose tail is zero-terminated.
        ok = _table(_read(os.path.join(FIX, "TextStylesDateTime.bin")))
        bad = _table(_read(os.path.join(FIX, "TextStylesDateTime_badformat.bin")))
        ok_dt = [e for e in ok if e[0] == 0x8e][0]
        bad_dt = [e for e in bad if e[0] == 0x8e][0]
        self.assertEqual(ok_dt[63], 0x00)       # safe: skeleton fits, NUL-padded
        self.assertNotEqual(bad_dt[63], 0x00)   # crash: freeform format overran

    def test_underline_strikeout_are_noops(self):
        # The underline/strikeout StaticTexts carry NO distinguishing byte/bit in
        # the widget entry vs a plain StaticText (UI-only; never rendered).
        et = _table(_read(os.path.join(FIX,
                    "TextStylesDateTime_underline_strikeout.bin")))
        st = [e for e in et if e[0] == 0x93]
        # entry [15:17]=color, [17]=0xff; bytes [12:15]=ptr; nothing else set.
        for e in st:
            self.assertTrue(all(b == 0 for b in e[20:]), "no extra flag bytes")


if __name__ == "__main__":
    unittest.main(verbosity=2)


class TestStaticTextWideBoxY(unittest.TestCase):
    """Regression: a StaticText's stored y/width derive from the MASK width only.
    Previously the wide-band wrap was applied twice (box width via ui_to_blob_xy,
    then mask width), pushing wide-box labels a band down -> on-panel noise."""

    def test_wide_box_narrow_and_wide_mask_y(self):
        from csm_panel.theme.compiler import build_entry
        base = dict(x=20, height=42, fastSensor=0, fontColor="ff00e5ff",
                    bgColor="ff000000", fgColor="ff000000", frameColor="ff000000",
                    text="x", fontSize=28, bold=0, italic=0, hAlign=0, imageDelay=0)
        # wide box (380 > 256) but NARROW mask (75): y must be uy%256 (+256*0)
        w = dict(base, y=10, width=380, type=2)
        e = build_entry(w, 1, True, mask_w=75, mask_h=42, mask_ptr=0x2000)
        self.assertEqual(e[6] | (e[7] << 8), 10, "narrow mask must not inherit box wrap")
        self.assertEqual(e[8] | (e[9] << 8), 75)
        # wide box AND wide mask (279): one band wrap only -> uy%256 + 256
        e = build_entry(w, 1, True, mask_w=279, mask_h=42, mask_ptr=0x2000)
        self.assertEqual(e[6] | (e[7] << 8), 10 + 256, "wide mask wraps exactly one band")
        self.assertEqual(e[8] | (e[9] << 8), 279 - 256)
