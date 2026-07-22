# Theme-blob unknowns & from-scratch authoring test plan

Goal: reach a **fully `.ui` → `.bin` compiler** so themes can be authored on Linux
without the Windows editor. Today the compiler reproduces a real editor blob
**byte-for-byte** for the widget geometry, descriptor, colors, field bindings and
by *reusing* a base blob's resource area. What is **not** solved is everything
that lives in the **resource area** (pre-rendered glyph masks + glyph-metric
tails) and the richer visual widgets (images/animation). Getting those wrong is
exactly what has **bricked the panel** ("MDT Error"), so we close the gaps from
ground truth instead of guessing.

## How to produce ground truth (safe)

The vendor **Windows editor's output is always valid** — downloading an
editor-authored theme never bricks the panel (only *our* from-scratch blobs
have). So for each item below:

1. Author it in the editor (portrait 480×800 unless noted).
2. **Save the `.ui`** (keep the whole theme folder: `*.ui`, `config.ini`,
   `images/`).
3. **Download to the panel while capturing USB** (USBPcap) so we get the
   compiled `.bin`.
4. Hand over the `.ui` + `.pcapng` (or just the theme folder + capture). Each
   pair lets us diff *our* compiled bytes against the editor's and lock a rule.

Reassemble the captured blob with:
`python tools/extract_frames.py capture.pcapng theme end` → concat the 4096-byte
`theme` chunks (see `csm_panel/theme/` + how `neongrid.bin` was made).

One "kitchen-sink" portrait theme can cover most of P1–P3; landscape needs its
own theme.

---

## Priority 1 — RESOURCE AREA (these are what brick the panel)

The firmware font can't be synthesised offline; StaticText/Number/DateTime draw
from 8-bpp coverage masks + per-glyph "metric tails" stored after the background
JPEG. We currently **copy** these from a base blob keyed by (type, fontSize). To
author freely we need the exact format.

### RESOLVED (from the labels + FontMatrix captures)
- **StaticText mask = 8-bpp coverage**, row-major `w*h` bytes, anti-aliased
  (~16 levels), colourised on-device by `[15:17]` RGB565. Pointer `[12:15]` BE24.
- **Font = Liberation Sans** (the editor's "Arial"; metric-compatible).
- **Pixel size = round(fontSize * 4/3)** — i.e. points at **96 DPI**. Verified:
  mask height == FreeType `ascent+descent` at that pixel size (size 24→36, 32→49,
  matches within rounding across sizes 8..76). Width == inked advance.
- **The portrait wide-transform applies to StaticText masks too**, with the same
  multi-band rule as everything else (`k = mask_w // 256`): the mask stores
  `w - 256*k` and its `by` gets `+256*k`. Verified on GeometryEdges' 853-px
  `WIDE_STATIC_TEXT` (k=3).
- **bold/italic** select the matching Liberation Sans face; no entry flag (the
  weight/slant is baked into the rendered mask). Verified on TextStyles.bin.
- **Opaque vs transparent background** is **NOT encoded in the widget entry** —
  an `isTransparent=0` StaticText has byte-identical `[12:64]` to a transparent
  one (`[15:17]`=color, `[17]`=0xff, no extra flag). The opaque background is
  painted on-device from `<backgroundColor>` behind the box; from-scratch we emit
  the same entry either way. (`isTransparent`/`backgroundColor` live in the `.ui`.)
- **Underline & strikethrough are UI-only NO-OPS.** The editor exposes them but
  they don't render on-panel, and `TextStylesDateTime_underline_strikeout.bin`
  confirms **no byte or bit** differs in the StaticText entry (or its mask) versus
  a plain one — nothing is emitted, so nothing draws.
- **Numbers** keep their own digit-glyph resource (separate from text masks).
  **RESOLVED byte-exact** (NumberMatrix.bin, sizes 8..64): a Number's rendered
  digits are a **glyph-major 8-bpp coverage "digit strip"** — the 12 glyphs
  `0 1 2 3 4 5 6 7 8 9 . -` stored **consecutively**, each glyph an
  `advance_width × strip_height` row-major block (NOT a shared raster; NOT
  `[12:15]` like StaticText). Entry layout:
    - `[11]` = **hAlign** (0 left / 1 center / 2 right).
    - `[12:14]` = fontColor RGB565 BE; `[14]=0x00 [15]=0xff [16]=0x00`.
    - `[17:20]` = **BE24 pointer** to the digit strip (deduped per fontSize).
    - `[20:22]` = strip_height (BE u16); `[22:42]` = the 10 digit advances (all
      equal); `[42:44]` = `.` advance; `[44:46]` = `-` advance; rest zero.
    - `[10]` = the `.ui` box height (NOT strip_height).
  Advances == FreeType `getlength()` **exactly** at pixel size
  `round(fontSize*4/3)`; strip_height == FreeType `ascent+descent` (editor within
  ±1px). **Implemented from-scratch** in `compiler.py` (`render_number_strip` +
  `number_metric_tail`) — no base blob needed for Numbers.
- STILL TO NAIL for *byte-exact* AA: per-glyph integer-advance + FreeType hinting
  mode vs the editor's rasteriser (our render is ~4-6px wider over ~8 chars and
  ~0.6 pixel-correlation — same text, not yet identical pixels). Sizes are close
  enough (±1-2px) to emit correctly-*dimensioned* masks now, which is what the
  resource layout needs; byte-exact AA is a later polish.

**P1a — StaticText matrix.** Add many `StaticText` widgets varying ONE property
at a time:
- Every **font size** the dropdown offers (note them all — we've only seen
  18/22/26). One widget per size, same string (e.g. `Ag0.9%`).
- **bold** on/off; **italic** on/off.
- **Color** (a couple distinct RGB565 values) and **transparent vs opaque
  background** (`isTransparent` 0/1 + a `backgroundColor`).
- A **long** string and a **multi-line** one (`isAutoLineBreak` 1).
- A string with **glyph-shape-sensitive** chars: digits `0123456789`, `.`, `:`,
  `-`, `%`, `°`, `/`, space, and a couple uppercase/lowercase letters.
- `characterSet` values other than 0 if the editor exposes them.

What we learn: mask bit layout (8-bpp? row-major? stride/alignment/padding), the
`[12:15]` BE24 resource pointer scheme, how per-size/per-weight advance metrics
are encoded, and how color/alpha interact with the mask.

**P1b — Number matrix.** `Number` widgets covering:
- Each font size; bold/italic.
- `hAlign` left/center/right, `vAlign` top/mid/bottom.
- `isTransparent` 0/1 + a background color.
- `isDiv1204` 0/1 (does the panel divide the field value by 1024? by 1000?).
- A value with a **decimal point** and a **negative** value, and a large value
  (does it render thousands with/without a separator? we believe NO separator —
  confirm) and one wide enough to test overflow/clipping.

What we learn: the Number `[12:64]` metric tail byte meaning (digit advance,
dp/sign glyph, alignment), so we can emit numbers at any size from scratch.
**RESOLVED** — see the Number digit-strip decode under *RESOLVED* above.
`hAlign` at `[11]`; `isDiv1204` has **no** effect on the widget entry bytes (a
render-time value-scaling hint, not stored).

**P1c — DateTime (0x8e).** RESOLVED byte-exact (`DateTimeMatrix.bin`, sizes
16/24/40, formats `yyyy-mm-dd hh:nn:ss` / `hh:nn:ss` / `yyyy/mm/dd`, hAlign 0/2):
- Field id fixed `0x15`/21 (confirmed); `[11]` = **hAlign** — DateTime's
  **right-align (2) works on-panel** (StaticText's right-align does NOT render).
- Tail (differs from Number — note the **32-bit** ptr): `[12:14]` color RGB565 BE;
  `[14]=0xff`; `[15:19]` = **BE32** ptr to the glyph strip; `[19:21]` strip_h (BE16
  = FreeType ascent+descent); `[21:45]` = **12 advances** (BE16) = the 6 real glyph
  advances `0` `.` `-` `:` ` ` `/` then **6 × px** (px = round(fontSize*4/3)).
- **Glyph strip** = glyph-major 8-bpp atlas of the 10 digits `0`..`9`, then
  `.` `-` `:` ` ` `/`, then 6 px-wide (blank) field cells; each glyph an
  `advance × strip_h` block. Strip width matches byte-exact (277/419/686 for
  16/24/40 = 10·digit + `.`+`-`+`:`+` `+`/` + 6·px). Advances match FreeType
  exactly; strip_h within ±1px (same ~1px AA gap as Numbers).
- **Format is packed inline in the entry tail** starting at `[45]`, only ~19
  bytes of room (`[45:64]`): `yyyy-mm-dd hh:nn:ss` → skeleton `"1-2-3 4:5:6"`,
  `hh:nn:ss` → `"4:5:6"`, `yyyy/mm/dd` → `"1/2/3"` (digits 1..6 = year/month/day/
  hour/min/sec field slots; literals verbatim), NUL-padded.
- **CRASH WARNING — a freeform `dateTimeFormat` (not the built-in skeleton) black-
  screens the panel.** `TextStylesDateTime_badformat.bin` shows the freeform text
  written straight into `[45:64]`, **overrunning the 19-byte region** (its last
  byte `[63]` is non-zero / unterminated) — that overflow is what crashes.
- **Implemented from-scratch** in `compiler.py` (`render_datetime_strip` +
  `datetime_format_skeleton`), restricted to the safe skeleton set above. Any
  other format returns `None` and falls back to REUSING a base entry (a base blob
  is then required); with no base it warns and emits a non-rendering geometry-only
  entry rather than risk the crash.

---

## Priority 2 — GEOMETRY (SOLVED byte-exact, incl. all edge cases)

The transform `ui_to_blob_xy` is now confirmed **byte-exact for every widget
type and every width** against the freshly-paired BandGeomFlat / BandGeomImage /
GeometryEdges editor blobs:

```
bl_x = (ux mod 256) + 256*(uy//256)
k    = uw // 256                      # number of 256-band boundaries the width spans
bl_y = (uy mod 256) + 256*k
bl_w = uw - 256*k
```

Verified branches (GeometryEdges): `uw==256` → k=1 (wide branch, bl_w=0);
`uw=512` → k=2; `uw=600` → k=2 (bl_w=88); StaticText `uw=853` → k=3; `uw<256`
→ k=0 (identity). x offsets 0/40/120/250, bands 0/1, and a bottom-band widget
(`y=780`) all match. `[10]` carries only the low byte of the box height.

**CORRECTION — the "per-band vertical offset" was a FALSE ALARM.** It came from
comparing an **edited** SysStatus `.ui` against an **older, unpaired** blob
(`tests/fixtures/SysStatus.*` are out of sync — the `.ui` was modified after the
capture, so its Number/Bar `y` values are 16–32 px off). The **freshly-paired**
BandGeom/GeometryEdges blobs match the pure `ui_to_blob_xy` result with **ZERO
offset** for every ProgressBar and Number, in all four bands, with and without a
bottom-band image. There is **no per-band offset**. The from-scratch compiler now
computes `[3:11]` geometry for ProgressBar **and Number** with the pure transform
(no base-copy); only DateTime still reuses a base entry (its glyph strip/format
skeleton are only partially decoded — see below), and the byte-exact reuse path
(`render_text=False`) still copies base geometry verbatim to stay bit-identical.

Still only inferred: `> 512`-px multi-band **ProgressBars** (GeometryEdges proves
the width rule up to 853 px on a StaticText; bars ≤512 are confirmed) and byte
`[0x53]` = animation frame count of the largest animated Image (30 in SysStatus).

---

## Priority 3 — RICH WIDGETS (images, animation, bar styles)

These are **inferred, not byte-confirmed**, and are the visual features most
likely to be used in polish (and to brick).

**P3a — Image widget (`0x84`).** RESOLVED (SysStatus capture):
- Pixel data is **raw** in the resource area (not a JPEG record), pointer at
  `[12:15]` BE24. **Frame count is byte `[16]`** (1=static, N=animation) and the
  **static/anim flag is byte `[17]`** (`0x01` static, `0x00` animated); `[15]`=0.
  (Earlier notes said count`[15]`/flag`[16]` — off by one; corrected.)
- Frames are stored **consecutively**. Opaque source = RGB565 `w*h*2`/frame
  (verified on SysStatus: WIDE_PATH 480×200×2×9 = 1,728,000).
- **Transparent PNG = PLANAR `w*h*3`/frame** — the whole 8-bit **alpha plane**
  (`w*h`) then the whole **RGB565-LE colour plane** (`w*h*2`), both **full-width
  row-major** (NO band-slicing; the wide-transform only touches the entry's stored
  w). RESOLVED byte-exact vs `AlphaImages.bin` (source R=x,G=y,alpha=170) for
  narrow 200×64, **wide 400×100** and logo 396×100. **CORRECTION: the earlier
  per-pixel `[colorLo,colorHi,alpha]` interleave was WRONG** — it rendered logos
  invisible / panels mis-tinted; the editor stores the two planes separately.
  `w,h` = the widget display size (source rescaled).
- A folder animation (`wide_path_0.jpg`, `_1`, …) loads all numeric siblings in
  the folder, sorted. **Per-frame delay** is not in the *Image* entry tail
  (`<imageDelay>`); for the **background** animation the delay IS decoded — see P3d.
- **Implemented from-scratch** in `compiler.py` (`render_image_resource`, planar).

**P3b — Image-filled ProgressBar.** RESOLVED byte-exact (`ImageBarVal.bin` works
vs `ImageBar.bin` broken). The **fill-mode flag is byte `[11]`**: `0x01` = colour
(value-driven) bar, `0x00` = image-fill. The `.ui` `<style>` selects it:
**`showType=1` + `bgImagePath` + `fgImagePath` → image-fill (`[11]=0`)**; `showType=0`
stays a colour bar (`[11]=1`) even with image paths — this is exactly why the
`showType=0` attempt (`ImageBar.bin`, `[11]=1`) never scrolled. Tail after the 3
RGB565 BE colours `[12:18]`: `[18:20]` bg w (BE16), `[20:22]` bg h, `[22:26]` bg
resource ptr (**BE32**); `[26:28]` fg w, `[28:30]` fg h, `[30:34]` fg ptr (BE32).
Both images are **opaque RGB565 LE** raw pixels (bg then fg, `w*h*2` each). The bar
geometry `[3:11]` is the normal `ui_to_blob_xy` of the `.ui` box (matches byte-exact;
not the image size). **The fg image MUST be shorter (narrower) than the bg image**
for the auto-scroll fill to animate (verified: ImageBarVal fg 167 < bg 300 works;
ImageBar's equal-width fg didn't) — it is **not** value-driven. Frame/scroll timing
is global (undecoded). **Implemented from-scratch** (`render_bar_image`, `showType=1`
→ `[11]=0`; warns if `fg ≥ bg`). A solid-colour bar remains the value-driven choice.

**P3c — ProgressBar `showType`.** We've only seen `showType=0`. Author bars with
each other `showType` the editor allows (vertical? right-to-left? segmented?),
and a `frameColor` clearly different from bg to see border rendering.

**P3d — Animated background.** RESOLVED (`AnimatedBg.bin`, 4 frames, delay 200).
A folder bg (`./images/BGA/anim_0.jpg` + numeric siblings) stores **one JPEG record
per frame** at `0x1000`, in numeric order, **terminated by a zero-size record**;
each frame embedded byte-for-byte (record sizes match the source JPEGs). Descriptor:
**`[0x52:0x54]` BE16 = frame count** (mirrored at `[0x53]`), **`[0x54:0x58]` =
`0x0000<delay>01`** where **`[0x56]` = per-frame delay** (the `.ui` `<imageDelay>`,
e.g. 200) and `[0x57]=0x01` constant. A static bg = count 1, delay 0. `[0x50]=0x10`
whenever a bg record is present. **Implemented from-scratch** (`_compile_from_scratch`
resolves the bg folder's frame siblings, embeds each JPEG, sets count + delay).
NB the current from-scratch code writes framecount to `[0x52:0x54]` (was previously
left 0 for static — a latent mismatch that didn't brick, now corrected to match).

---

## Priority 4 — GLOBAL / ORIENTATION

- A **landscape** (800×480) theme with a couple widgets → confirm the transform
  is truly identity there (we assume so; unverified end-to-end via the compiler).
- Themes saved at each **orientation** setting → map descriptor byte `[1]`
  (`0x00/0x01/0x02/0x03`; `0x02`=upright confirmed) and any editor "rotation".
- `config.ini` keys beyond `[FastBg] path` and `[fastFontColor] color`.

---

## Current status cheat-sheet

| piece | status |
|---|---|
| descriptor / header | CONFIRMED byte-exact |
| widget-table framing, type map, field binding (byte[2]=fastSensor) | CONFIRMED |
| coordinate transform (all widths incl. w=256/512/853, x/band, bottom band) | CONFIRMED byte-exact (k=uw//256) |
| coordinate transform (per-band vertical offset) | FALSE ALARM — no offset exists |
| ProgressBar color layout; percent-fill 0..100 render | CONFIRMED |
| StaticText color + ptr; mask format; bold/italic; opaque bg; underline no-op | CONFIRMED |
| Number(0x92) digit strip + metric tail (from-scratch) | CONFIRMED byte-exact |
| DateTime(0x8e) glyph strip + metric tail + inline format (from-scratch, safe skeletons) | CONFIRMED byte-exact; freeform → reuse base |
| Image widget opaque encoding | CONFIRMED byte-exact (P3) |
| Image widget alpha encoding (PLANAR alpha-plane then colour-plane) | CONFIRMED byte-exact (P3a) |
| animated background (N JPEG records + framecount [0x52] + delay [0x56]) | CONFIRMED byte-exact (P3d) |
| image-filled ProgressBar (bg/fg refs, [11]=0 flag, showType=1, fg<bg scroll) | CONFIRMED byte-exact (P3b) |
| ProgressBar showType: 0=colour bar, 1=image-fill | CONFIRMED (other variants UNKNOWN) |
| landscape / orientation matrix | PARTIAL (P4) |

The from-scratch path (`render_text=True`) can now emit StaticText, **Number**,
**Image** (opaque + planar-alpha), **animated backgrounds**, **DateTime** (safe
skeleton formats), and **image-fill ProgressBars** without a base blob. Only a
DateTime with an **unsupported/freeform** format still needs a base to reuse (a
wrong format **crashes the panel** — see P1c). The `render_text=False` reuse path
stays byte-exact. Never flash a from-scratch blob without validating it in the
editor first, and keep the firmware recovery frames handy (`PROTOCOL_NOTES.md` →
recovery).
