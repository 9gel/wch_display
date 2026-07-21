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
- **The portrait wide-transform applies to StaticText masks too**: a mask wider
  than 256 stores `w-256` (76pt "76:Ag5.2%" → stored 237 = 493-256) and its `by`
  gets +256, same rule as ProgressBars.
- **Numbers** keep their own digit-glyph resource (separate from text masks; the
  ~36 KB region after the text masks). Digit-glyph exact layout still TODO.
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

**P1c — DateTime.** One `DateTime` per available **format** (date-only,
time-only, combined; 12/24h if offered) and size. What we learn: the format-code
byte(s) and its metric tail. (Field id is fixed `0x15`/21 — confirmed.)

---

## Priority 2 — GEOMETRY edge cases (transform is solved for the common case)

Confirmed byte-exact: narrow widgets and **wide (256<w<512)** ProgressBars that
wrap one band (`bl_y += 256`, `bl_w -= 256`). Still unverified:

- A ProgressBar **exactly 256 px** wide (narrow or wide branch?).
- One **> 512 px** wide (spans 2 band boundaries — does `bl_y += 512`,
  `bl_w -= 512`? our rule only subtracts one band).
- Bars at **x offsets other than 40** (e.g. 0, 120, 250, 300) so the wrap point
  varies within the band.
- A widget in the **bottom band** `y ∈ [768, 800)` (32-px-tall last band) — we've
  only tested up to y≈716.
- A **tall** widget: `height > 32`, and `height > 255` (is height only byte
  `[10]`, or is there a high byte we've always seen as 0?).
- One **static text / number** that is itself > 256 wide (does the wide rule
  apply to non-bars too, or only ProgressBars?).
- Confirm `x ∈ [256,480)` band-1 widgets for every type.

---

## Priority 3 — RICH WIDGETS (images, animation, bar styles)

These are **inferred, not byte-confirmed**, and are the visual features most
likely to be used in polish (and to brick).

**P3a — Image widget (`0x84`).**
- A **static** `Image` (one PNG/JPEG). How is its pixel data stored — a JPEG
  record like the background, or raw? How does `[12:15]`/framecount point to it?
- An **animated** `Image` (a frame sequence with a delay, e.g. the vendor fan:
  `fan0..7.png`, 30 ms). How are N frames + per-frame delay encoded (record
  list? framecount `[0x54]`? a delay field)?

**P3b — Image-filled ProgressBar.** A ProgressBar using `<bgImagePath>` /
`<fgImagePath>` (image fill instead of solid color). How is the image ref stored
in the `0x8b` entry, and how does fill clip the fg image?

**P3c — ProgressBar `showType`.** We've only seen `showType=0`. Author bars with
each other `showType` the editor allows (vertical? right-to-left? segmented?),
and a `frameColor` clearly different from bg to see border rendering.

**P3d — Animated background.** If the editor supports a multi-frame background
(the `Flash-animated` capture had 125 JPEG records), author a short animated bg
and note frame count + delay controls → confirm descriptor `[0x54]` framecount
and where per-frame delay lives.

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
| coordinate transform (narrow + wide 256<w<512) | CONFIRMED byte-exact |
| coordinate transform (w=256, w>512, h>255, bottom band) | UNKNOWN (P2) |
| ProgressBar color layout; percent-fill 0..100 render | CONFIRMED |
| StaticText/Number/DateTime color + ptr/flags | CONFIRMED |
| glyph MASK format + metric tails (from-scratch) | UNKNOWN → reuse base (P1) |
| Image widget + animation encoding | INFERRED (P3) |
| image-filled bars, showType variants | UNKNOWN (P3) |
| landscape / orientation matrix | PARTIAL (P4) |

Until P1 is cracked, keep authoring via the editor (or the compiler's
`render_text=False` reuse path with a base blob). Never flash a `render_text=True`
/ from-scratch blob without validating it in the editor first, and keep the
firmware recovery frames handy (`PROTOCOL_NOTES.md` → recovery).
