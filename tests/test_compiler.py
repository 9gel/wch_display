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
@unittest.skipUnless(_has_font(), "Liberation Sans not found")
@unittest.skipUnless(os.path.exists(SYS_BLOB), "sysstatus_editor.bin not present")
@unittest.skipUnless(os.path.isdir(SYS_IMAGES), "theme image folder not present")
class TestFromScratchStructural(unittest.TestCase):
    """From-scratch (render_text=True) structural-validity acceptance criteria."""

    @classmethod
    def setUpClass(cls):
        cls.ui = _read(os.path.join(FIX, "SysStatus.ui.xml"))
        cls.ed = _read(SYS_BLOB)
        cls.out = C.compile_ui_to_blob(cls.ui, images_dir=SYS_IMAGES,
                                       base_blob_for_resources=cls.ed,
                                       render_text=True)
        cls.uiw = [w for w in C.parse_ui(cls.ui) if w["tag"] == "widget"]
        cls.ot = _table(cls.out)
        cls.et = _table(cls.ed)
        cls.clen = struct.unpack_from(">I", cls.out, 0x58)[0]
        cls.res_start = struct.unpack_from(">I", cls.out, 0x1000)[0] + 0x1004

    def test_descriptor_valid(self):
        self.assertEqual(self.out[:4], b"\x96\x02\x00\x00")
        self.assertEqual(self.out[0x40], 0x81)
        self.assertEqual(struct.unpack_from(">H", self.out, 0x47)[0], 480)
        self.assertEqual(struct.unpack_from(">H", self.out, 0x49)[0], 800)

    def test_content_len_correct(self):
        self.assertEqual(self.clen, len(self.out))

    def test_one_entry_per_widget(self):
        self.assertEqual(len(self.ot), len(self.uiw))

    def test_types_match_document_order(self):
        for e, w in zip(self.ot, self.uiw):
            self.assertEqual(e[0], C.UI2BLOB[w["type"]])

    def test_base_bound_geometry_present_in_editor(self):
        # Number/DateTime/ProgressBar geometry [3:11] is copied from the base and
        # must therefore appear (as a multiset) among the editor's entries.
        from collections import Counter
        ed_by_type = {}
        for e in self.et:
            ed_by_type.setdefault(e[0], Counter())[_geom(e)] += 1
        for bt in (0x92, 0x8e, 0x8b):
            mine = Counter(_geom(e) for e in self.ot if e[0] == bt)
            missing = mine - ed_by_type.get(bt, Counter())
            self.assertFalse(missing, f"type {hex(bt)} unmatched geometry: {dict(missing)}")

    def test_image_geometry_matches_transform(self):
        # Image geometry is computed by the transform and must match the editor.
        from collections import Counter
        mine = Counter(_geom(e) for e in self.ot if e[0] == 0x84)
        ed = Counter(_geom(e) for e in self.et if e[0] == 0x84)
        self.assertFalse(mine - ed, "image geometry diverges from editor")

    def test_statictext_pointers_in_bounds_and_sized(self):
        st = [e for e in self.ot if e[0] == 0x93]
        self.assertTrue(st)
        for e in st:
            p = _ptr(e)
            w = struct.unpack_from("<H", e, 8)[0]
            h = e[10]
            self.assertTrue(self.res_start <= p < self.clen, "ptr in resource area")
            self.assertLessEqual(p + w * h, self.clen, "mask region fits in blob")

    def test_image_pointers_in_bounds_and_sized(self):
        from PIL import Image
        img = [e for e in self.ot if e[0] == 0x84]
        self.assertTrue(img)
        doc_imgs = [w for w in self.uiw if w["type"] == 4]
        for e, w in zip(img, doc_imgs):
            p = _ptr(e)
            self.assertTrue(self.res_start <= p < self.clen, "image ptr in resource area")
            paths = C._image_frame_paths(SYS_IMAGES, w["imagePath"])
            if not paths:
                continue
            with Image.open(paths[0]) as im:
                alpha = (im.mode in ("RGBA", "LA")
                         and im.getchannel("A").getextrema()[0] < 255)
            bpp = 3 if alpha else 2
            size = w["width"] * w["height"] * bpp * len(paths)
            self.assertLessEqual(p + size, self.clen, "image region fits in blob")
            self.assertEqual(e[16], len(paths), "frame count byte [16]")
            self.assertEqual(e[17], 0x01 if len(paths) == 1 else 0x00, "static flag [17]")

    def test_statictext_mask_dims_within_2px_of_editor(self):
        # For each editor StaticText mask, the .ui text that produced it must
        # render to within 2px. We match editor masks to .ui texts by (fontSize)
        # and content; the editor kept 'kbps' among ASCII texts (79x42) and the
        # non-ASCII degC masks (36x36). (It dropped homelab/homelab2/mac mini
        # from its table entirely — those have no editor mask to compare.)
        ed_dims = {(struct.unpack_from("<H", e, 8)[0], e[10])
                   for e in self.et if e[0] == 0x93}
        _, kw, kh = C.render_text_mask("kbps", 28, 0, 0)
        self.assertTrue(any(abs(ew - kw) <= 2 and abs(eh - kh) <= 2
                            for ew, eh in ed_dims),
                        f"'kbps' {kw}x{kh} not within 2px of any editor mask "
                        f"{sorted(ed_dims)}")

    def test_statictext_height_follows_size_rule(self):
        from PIL import ImageFont
        for w in (w for w in self.uiw if w["type"] == 2):
            px = C._pixel_size(w["fontSize"])
            asc, desc = ImageFont.truetype(
                C._find_font(w["bold"], w.get("italic", 0)), px).getmetrics()
            _, _, mh = C.render_text_mask(w["text"], w["fontSize"], w["bold"],
                                          w.get("italic", 0))
            self.assertEqual(mh, asc + desc, f"{w['text']!r} size {w['fontSize']}")

    def test_roundtrip_parse(self):
        # blob parses cleanly back through the widget table (no stray bytes).
        ents = _table(self.out)
        self.assertEqual(len(ents), len(self.uiw))
        for e in ents:
            self.assertTrue(0x80 <= e[0] <= 0x9f)


if __name__ == "__main__":
    unittest.main(verbosity=2)
