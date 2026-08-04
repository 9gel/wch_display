# CSM050H800480 panel — serial protocol (reverse-engineered)

Device: `1a86:8040` USB CDC-Serial. `model` query returns
**`CSM050H800480_14 NAND V0.2.8`** → 5.0″ 800×480, NAND-backed, fw 0.2.8.
Single WCH MCU (markings filed off) running custom firmware; board
`HJ-5.0-LCD-V03` / `FUTURE LIFE`. Sold as a 5″ 800×480 USB PC sub-display.

Protocol decoded from USBPcap captures in `captures/` (see `tools/pcap_usb.py`).

## Transport
- CDC-ACM serial, `/dev/ttyACM0`. 115200 8N1, DTR+RTS asserted (baud is nominal).
- Host→device on bulk **EP 0x02 (OUT)**; device→host on **EP 0x82 (IN)**.
- Commands are ASCII-named, NUL-padded to a fixed 64-byte header, optionally
  followed by a payload. Device acks some commands with a single byte `C` (0x43).

## Commands seen
| ASCII name | header | payload | reply | meaning |
|---|---|---|---|---|
| `model` | 64 B | — | model string (e.g. `CSM050H800480_14 NAND V0.2.8`) | identify |
| `theme` | 64 B | 4096 B chunk | — | one block of a theme-package upload |
| `end`   | 64 B | — | `C` | finish theme upload |

### `theme` / `end` header layout (64 bytes)
```
[0:6]  "theme\0" or "end\0..."
[7:9]  block index, 16-bit BIG-endian (0,1,2,… over the upload)
[9:..] per-upload id / metadata (constant across a given upload)
rest   zero
```
Each `theme` frame carries a 4096-byte slice of the **theme blob**; slices are
concatenated in block order. `end` closes the transfer; device replies `C`.

### Theme blob layout
```
0x000  u32 LE magic = 918 (0x396)           # constant, all themes
0x040  descriptor:
        0x40 u8   0x81
        0x47 u16 BE  width   (0x0320 = 800)   # swapped W/H selects orientation
        0x49 u16 BE  height  (0x01e0 = 480)
        0x4c u16     0xf79e  (constant)
        0x50 u16 BE  data offset = 0x1000
        0x52 u16 BE  frame count (e.g. 125)
        0x59.. 3-byte theme id (also echoed in each frame header)
0x1000 records: repeated [u32 BE jpeg_size][jpeg bytes]  # each 800×480 JPEG
after  trailing layout/.ui region (on-device text + data-region definitions,
       separately encoded — NOT needed for full-screen image display)
```
Verified: the 125 records in `Flash-animated` match the source `star01_*.jpg`
files byte-for-byte. A minimal blob of just `magic + zeros` blanks the screen.

**Consequence:** the panel is effectively a **JPEG framebuffer**. We render an
800×480 image on the host, JPEG-encode it, wrap it in a theme package, and
upload — full flexibility without touching the vendor's on-device layout system.

**Accepted JPEG flavor (the on-board decoder is minimal and picky):** each record
must be a **baseline** (never progressive) JPEG with a **JFIF APP0** header, the
**standard separate DC/AC luma+chroma Huffman tables**, and **4:2:0 or 4:4:4**
chroma sampling. Anything else decodes to on-screen **noise** even though it flashes
fine and opens on a PC. In particular **ffmpeg's built-in mjpeg encoder is NOT
usable** — it omits JFIF, writes a single combined Huffman/quant table, and emits a
non-standard `1×2` sampling geometry for 4:4:4. Encode with **libjpeg** (Pillow
`subsampling=0|2, progressive=False`, or ImageMagick `-interlace none
-sampling-factor 4:2:0`). Use ffmpeg only to *extract* frames to PNG, then encode
with libjpeg — this is what `tools/video_to_frames.py` does.

**Background images need enough detail (editor-side gotcha).** The Windows editor
**silently drops a background** whose JPEG is too small/low-detail — it writes a
**0-byte bg record** (panel then shows uninitialized-framebuffer noise). It is not
the JPEG flavor (a solid/gradient PIL JPEG is byte-structurally identical to an
accepted one) — it's the *content*: smooth images compress below a size floor
(~between 11 KB and ~48 KB at 480×800) and get treated as empty. Real photos
(starry 48 KB, neon_bg 58 KB) and random noise (306 KB) embed fine; a flat gradient
(11 KB) does not. Fix for a synthetic background: add light per-pixel noise
(±8/channel dithering ≈ film grain → ~90 KB). The editor stores the accepted bg
**byte-for-byte** (record size == source file size; no re-encode). NOTE this only
affects the **background**; Image *widgets* accept smooth/synthetic sources fine.

**Theme size cap: 4 MiB.** The panel rejects theme blobs larger than ~4 MiB (it
flashes/acks but shows noise). Backgrounds dominate the size — for an animated
background (N JPEG records) drop JPEG quality until the total blob fits (a
mostly-dark starry sky at 480×800 is ~50 KB/frame at q75 vs ~140 KB at q92).
`Panel.send_theme` raises above 4 MiB; `tools/video_to_frames.py --max-mb` auto-
lowers quality to a frame-size budget.

## No-reset live updates (the widget path)

**Theme-flash flow control (app mode).** Unlike the boot-mode `update` flow
(which acks once after `end` — see recovery section), the app-mode `theme`
upload is **paced with per-batch flow control**: the panel NAND-writes each 4 KB
block as it arrives (~64 ms/block ≈ 62 KiB/s) and emits a `C` (0x43) ack **after
every 256 blocks**, then once more after `end`. The host must wait for each
periodic ack before sending more; blasting all frames back-to-back overruns the
writer and flashes a corrupt / **"Empty Theme"** blob (intermittently: sometimes
renders, sometimes no live updates, sometimes empty). Verified against the vendor
Windows app in `theme_Cybercity.pcapng` (acks after blocks 255/511/767, ~64 ms
between frames). Implemented in `Panel.send_theme` (flush per frame + read the
256-block acks). The blob itself is byte-for-byte equivalent to the vendor's — the
difference was purely the flash transport, not the generator.

A `theme` flash **re-enumerates/reboots** the panel (writes NAND to apply), so it
can't be done frequently. The vendor's live path avoids this: flash a *widget*
theme ONCE, then stream `0x66` value frames — they update on-device widgets
bound to fields with **no reset**, and also keep the panel awake (it sleeps /
backlight-off after ~30s without `0x66`; an ENXIO on open just means it's asleep
or mid-re-enumeration, not bricked).

- **Orientation** is baked into the blob at **byte[1]** (`0x96` is byte[0]):
  `0x00/0x01/0x03` = upside-down/rotated for this mount, **`0x02` = upright**.
- **Widget table** @0x80 (64-byte entries): `[0]`=type, `[1]`=id, `[2]`=field id
  (the `0x66` field it shows), `[4:12]`=x,y,w,h LE16, then per-type data. Types:
  `0x8b`=big number (color RGB565 @[0x0e]&[0x10]), `0x92`=gauge+number, `0x8e`=
  curve, `0x93`=static image (`[12:15]` BE24 pointer into the resource area).
- **Digit glyphs live in a "resource area"** (~35 KB after the background record),
  so authoring widgets from scratch renders blank. We therefore **reuse a captured
  vendor theme blob** (`data/base_theme.bin` = the upright "Simplicity" theme,
  three `0x8b` numbers on fields 3/7/12) and swap only its background JPEG
  (record0), shifting `0x93` pointers by the size delta. Relabelling the three
  background panels gives a custom KIOSK/HOMESERVER/NAS temperature display.
  Implemented in `csm_panel/widget_theme.py`; driven by `service.run_widget()`.

## Live-data push (`0x66`) and keepalive (`0x6e`)
The vendor app, after loading a theme, streams two periodic binary frames on
EP 0x02:
- `0x66` "data" frame: `66 00 <len> 01 <YY MM DD HH MM SS> <field records...>`
  where field records are `<tag><u16 value>` for tags 0x02..0x15 (CPU%, temps,
  mem, net, clock, …). This updates on-device text/gauge regions defined by the
  theme. len at byte[2] (e.g. 0x4d = 77).
- `0x6e` keepalive: e.g. `6e 00 05 1e d0` / `6e 00 0e …` (byte[2]=len).

We drive the panel by **host-rendered full-screen JPEGs** instead, so the
`0x66` region system is optional (documented here for completeness).

## Boot-mode firmware update / recovery

The bootloader (`model` reports `...BOOT V1.6`) accepts a firmware image over the
same serial link. The vendor "Update" flow (reverse-engineered from
`5 inch SmartMonitor.exe`) is **identical to the theme upload** except:

* data frames use the command token **`update`** (not `theme`);
* the stream is terminated by a lone **`end`** header (no data body);
* the `update_*.bin` is sent **raw** — it is already encrypted and the
  bootloader decrypts it on-device.

Frame = 64-byte header + 4096-byte chunk (last chunk zero-padded); header
`[0:6]=name`, `[6:8]=block BE16`, `[8:12]=len BE32` (true size), `[12:14]=CRC16
BE16`. Device acks `C` (0x43) **once, after the final `end` frame** (not per
frame) — so write all frames back-to-back then read one byte; reading per-frame
stalls tens of seconds and the flaky boot-mode USB re-enumerates mid-transfer.
Max image 4 MB. Control commands (64-byte name, rest zero): `boot` (app →
bootloader, `slotBootMode`), `reset` (reboot / jump to app, `slotResetMcu`).

**The firmware CRC cannot be computed host-side.** A real vendor capture
(`Firware_Theme_Flash.pcapng`) shows the `[12:14]` CRC is computed over the
**decrypted** image (device-side key): capture CRC `0x16a7` matches *no* standard
CRC16 over the encrypted bytes we transmit (`crc16_modbus`=`0x3a41`, and an
exhaustive poly/init/reflection sweep finds nothing). Themes are different — their
CRC *is* `crc16_modbus` over the (plaintext) blob (verified: capture theme CRC
`0x5a76` == `crc16_modbus(theme)`).

**Recovery:** an invalid theme can make the *running app* write a bad data table
("MDT"), after which the bootloader shows "MDT Error" and won't launch the app
(theme writes are ignored in boot mode; only a firmware flash fixes it). Because
the firmware CRC can't be recomputed, the reliable Linux recovery is to **replay
the vendor's own Update frames** captured over USB:

    python tools/extract_frames.py Firware_Theme_Flash.pcapng update end > recovery.frames
    csm-panel flash-firmware --replay recovery.frames --flash    # panel in boot mode

The correct image for this panel is `update_sdnand_800480_*.bin` (NOT `update_S021*`
/ `update_SM050*`, other models). See `csm_panel/firmware.py`.

## Theme file format & the `.ui` compiler

The vendor Windows "Theme Editor" saves themes as `*.ui` files and compiles them
to the blob you flash. Both are reverse-engineered; `csm_panel/theme/` reimplements
them on Linux.

**`.ui` files** are **RC4-encrypted XML** (key
`"This product is designed by OuJianbo,zhe ge chan pin shi gzbkey she ji de"`;
RC4 is symmetric so the same op decrypts and encrypts). XML is `<ui><widgetParent
background/>` + `<widget type=N>` with `<geometry>` and, for data widgets,
`<sensor><fastSensor>` (= the `0x66` field id → blob byte `[2]`; 0 = static). `.ui`
types: 2=StaticText, 3=ProgressBar, 5=Number, 6=DateTime, 4=Image.

The editor's **"数据类型 / value type"** dropdown (`<sensorTypeName>` +
`<readingName>`) is what sets `<fastSensor>`: picking a *fast* metric assigns a
non-zero field id, while picking a *named* library reading (e.g. "Disk Total",
"Core 0 Clock") sets `<fastSensor>0`, moving the widget onto the slow named-sensor
path — so **for our `0x66`-driven use, every data widget needs a non-zero
`<fastSensor>`.** (Confirmed by diffing two captures of the same theme where only
the value-type dropdown changed: the *only* blob delta was byte `[2]` of the
edited widgets.)

**The blob** (`96 02 00 00` magic; `[1]`=orientation 0x02):
- descriptor `0x40–0x80`: marker `0x81`; width BE16 @`0x47`, height BE16 @`0x49`;
  const `f79e` @`0x4c`; background-record flag @`0x50` (`0x10` if a bg JPEG is
  present); framecount BE32 @`0x54`; content-length BE32 @`0x58`.
- widget table @`0x80`: 64-byte entries, one per `<widget>` in document order.
  `[0]`=blob type (StaticText→`0x93`, ProgressBar→`0x8b`, Number→`0x92`,
  DateTime→`0x8e`, Image→`0x84`); `[1]`=id; `[2]`=field id (== `fastSensor`;
  DateTime fixed `0x15`); `[3]`=portrait x-band `ui_x//256`; `[4:6]`,`[6:8]`=x,y
  LE16; `[8:10]`=w LE16; `[10]`=height low byte; `[11]`=per-type flag. Colors are
  **RGB565 big-endian** in the tail; StaticText carries a BE24 pointer `[12:15]`
  into the resource area.
- background JPEG record at `0x1000` (BE32 size + JPEG) when present; then the
  **resource area** of coverage masks + image bitmaps (see below).

**Records vs resource area.** `0x1000` holds the **JPEG records** (`[BE32 size][JPEG]`
repeated, size=0 terminates) — these are the animated **background** frames (one
record per frame). Everything a widget points to (StaticText/DateTime glyph masks,
**Image widget bitmaps**) lives in the **resource area** *after* the records, indexed
by BE24 byte offsets from blob start.

**Image widget (`0x84`)** tail: `[12:15]` = BE24 pointer into the resource area,
`[15]` = **frame count** (1 = static; N = animation), `[16]` = static/anim flag
(`0x01` static, `0x00` animated). Image pixels are **raw, uncompressed** in the
resource area, `frame_count` frames stored consecutively:
- **opaque** image (e.g. a JPEG/GIF source): **RGB565, `w*h*2` bytes/frame**
  (verified: WIDE_PATH 9 frames × 480×200×2 = 1,728,000 B).
- **alpha** image (PNG w/ transparency): **PLANAR, `w*h*3` bytes/frame** — the whole
  **8-bit alpha plane (`w*h` bytes)** first, then the whole **RGB565 little-endian
  colour plane (`w*h*2` bytes)**, both **full-width row-major** (no band-slicing;
  the wide-transform only affects the widget entry's stored w). CONFIRMED byte-exact
  against `AlphaImages.bin` (source pattern R=x,G=y,alpha=170 → every stored pixel
  reveals its coordinate) for a narrow (200×64), wide (400×100) and logo (396×100)
  image. **NB: an earlier per-pixel `[colorLo,colorHi,alpha]` interleave was WRONG**
  (logos rendered invisible, panels mis-tinted); the editor is planar.
  `w,h` are the widget's *display* size (the editor rescales the source to fit).

**Consequences for authoring:**
- **Animations are stored raw, so they are huge** — a 480×200 animation costs
  192 KB/frame; the 4 MiB theme cap is reached fast. Keep animated regions small.
- **A "static" background still bundles every JPEG in its source folder** (30 STARRY
  frames = ~1.5 MB were shipped for a static bg). For a static background put a
  **single** frame in the folder.

**ProgressBar tail** (`0x8b`, bytes `[12:]`): `[12:14]` bgColor, `[14:16]`
fgColor, `[16:18]` frameColor (all RGB565 BE), then a u16 at `[18:20]` and again
at `[26:28]` that is a **constant editor default** (`0x76a0`=30368 / `0xb910`=47376
depending on editor version) — *not* a per-bar range. It has no effect on fill.
Byte `[11]` = **fill mode**: `0x01` = colour (value-driven) bar, `0x00` = image-fill.

**Image-fill ProgressBar** (`0x8b` with `[11]=0`). CONFIRMED byte-exact
(`ImageBarVal.bin`, works) vs a broken attempt (`ImageBar.bin`, didn't scroll).
The `.ui` `<style>` distinguishes them: **`showType=1` + `bgImagePath` + `fgImagePath`
→ image-fill (`[11]=0`)**; `showType=0` stays a colour bar (`[11]=1`) even if image
paths are set (this is exactly why the `showType=0` attempt didn't animate). Tail
after the 3 colours: `[18:20]` bg image w (BE16), `[20:22]` bg h, `[22:26]` bg
resource ptr (**BE32**); `[26:28]` fg w, `[28:30]` fg h, `[30:34]` fg ptr (BE32).
Both images are **opaque RGB565 LE** raw pixels in the resource area (bg then fg,
`w*h*2` each). The bar geometry `[3:11]` is the normal `ui_to_blob_xy` of the `.ui`
box. **The fg image MUST be shorter (narrower) than the bg image** for the auto-
scroll fill to animate (fg scrolls across the bg, wrapping); it is not value-driven.

### How a bound widget updates (verified on-panel)
Byte `[2]` = the `0x66` fast-field id (`== <fastSensor>`; `0` = unbound/static).
The panel renders the incoming field value **by widget type**:
- **Number** (`0x92`): shows the value **verbatim** as an integer (no thousands
  separator — the glyph set is digits + `.`/`-`/`:` only). Any field 2..21 works.
- **ProgressBar** (`0x8b`): fills **`value` interpreted as a percent 0..100**
  (clamped) — the fgColor sweeps that fraction of the width. It does **not** use
  the `[18:20]` "scale". Sending e.g. `30368` renders empty/garbage (overflow);
  sending `0..100` fills cleanly. Confirmed by a live staircase test and by the
  vendor `Wakeup-stats` theme, whose CPU-usage bar is bound to field 3 (which its
  own `0x66` stream carries as `12..61`, i.e. a percentage).
- **StaticText/DateTime**: masks/glyphs from the resource area (not field-driven,
  except DateTime's clock).

So a "sparkline" of N history samples is N ProgressBars, each bound to its own
field, fed the sample **normalised to 0..100** on the host side.

**`0x66` field semantics are fixed by firmware.** In the vendor scheme fields
2..21 mean specific PC metrics (2=CPU MHz, 3=CPU load %, 10/11=mem MB, 12/13=temps,
GPU slots 4..9 sit at `0` when absent, …). We ignore those meanings and just treat
each field as an opaque 16-bit slot bound to our widgets — a Number shows it raw,
a ProgressBar shows it as a 0..100 %. Field id **1 is not carried** by the `0x66`
frame (records run 0x02..0x15); some vendor themes bind bars to field 1, which is
then driven by a different (slow named-sensor) path we don't use.

**Coordinate transform (portrait themes):** the 480×800 canvas is resliced into
256-px-tall bands packed left-to-right in the panel's landscape framebuffer.
A widget whose width spans `k = ui_w // 256` band boundaries wraps down `k` bands:
`bl_x = ui_x%256 + 256*(ui_y//256)`, `bl_y = ui_y%256 + 256*k`, `bl_w = ui_w −
256*k` (landscape themes are identity). Confirmed byte-exact for every widget type
and width (incl. `ui_w` = 256/512/853) against fresh editor blobs; there is **no**
per-band vertical offset (an earlier "offset" was a stale-fixture artefact). This
is why the editor's preview isn't pixel-WYSIWYG.

**Number glyphs (0x92) — decoded, synthesisable.** A Number's digits are a
**glyph-major 8-bpp "digit strip"** in the resource area: the 12 glyphs
`0..9 . -` stored consecutively, each an `advance × strip_height` row-major block.
The entry carries `[11]`=hAlign, `[12:14]`=color RGB565 BE, `[17:20]`=BE24 strip
pointer, `[20:22]`=strip_height and `[22:46]`=12 glyph advances (BE u16). Advances
equal FreeType `getlength()` at pixel size `round(fontSize*4/3)`; strip_height =
`ascent+descent`. The compiler renders these from scratch (Liberation Sans).

**DateTime (0x8e) — DECODED, synthesisable (safe skeletons only).** Tail
(`DateTimeMatrix.bin`, sizes 16/24/40): `[12:14]` fontColor RGB565 BE; `[14]=0xff`;
`[15:19]` = **BE32** pointer to the glyph strip (NB 32-bit, unlike Number's 24-bit
`[17:20]`); `[19:21]` strip_height (BE16 = FreeType ascent+descent); `[21:45]` =
**12 advances** (BE16): the 6 real glyph advances `0` `.` `-` `:` ` ` `/` followed
by **6 × px** cells (px = `round(fontSize*4/3)`); `[45:64]` = the inline format
skeleton, NUL-terminated. The **glyph strip** is a glyph-major 8-bpp atlas of the
10 digits `0`..`9`, then `.` `-` `:` ` ` `/`, then 6 px-wide field-slot cells — each
an `advance × strip_h` block (strip width verified byte-exact: 277/419/686 for sizes
16/24/40). Advances match FreeType `getlength()` exactly; strip_h within ±1px.
`[11]` = hAlign (right-align `2` works on-panel; StaticText right-align does not).
**Format is packed inline in `[45:64]`** (`yyyy-mm-dd hh:nn:ss` → `"1-2-3 4:5:6"`,
`hh:nn:ss` → `"4:5:6"`, `yyyy/mm/dd` → `"1/2/3"`; year=1 month=2 day=3 hour=4 min=5
sec=6, literals verbatim). **A freeform `dateTimeFormat` overruns `[45:64]` and
BLACK-SCREENS the panel** — so the compiler emits DateTime from scratch **only for
these safe skeletons**; any other format falls back to reusing a base entry (a base
blob is then required), and warns if no base is available.

**Animated background — DECODED** (`AnimatedBg.bin`). A folder bg
(`./images/BGA/anim_0.jpg` with numeric siblings) stores **one JPEG record per frame**
at `0x1000`, in numeric order, **terminated by a zero-size record**; each frame is
embedded byte-for-byte (verified: 4 records, sizes match the source JPEGs). The
descriptor carries the frame count at **`[0x52:0x54]` BE16** (and mirrored at
`[0x53]`), and **`[0x54:0x58]` = `0x0000<delay>01`** where **`[0x56]` = per-frame
delay** (from the `.ui` `<imageDelay>`, e.g. 200) and `[0x57]=0x01` (constant); a
static bg has count 1 and delay 0. `[0x50]=0x10` whenever a bg record is present.

The compiler (`csm_panel/theme/compiler.py`) rebuilds the descriptor + widget
table from a `.ui`. Its `render_text=False` reuse path copies a known-good base
blob's records + resource area verbatim and reproduces a real vendor blob
**byte-for-byte**. Its `render_text=True` from-scratch path now synthesises
StaticText masks, **Number digit strips**, **Image pixels** (opaque + planar-alpha),
**animated backgrounds** (multi-frame JPEG records + delay), **DateTime** (glyph
strip + metric tail + inline format, for the safe skeletons only), and **image-fill
ProgressBars** (bg/fg RGB565 refs, `showType=1`→`[11]=0`, fg<bg) with no base blob.
DateTime with an unsupported/freeform format still needs a base to reuse.

## History / dead-ends (condensed)
Earlier we mis-identified this as a UsbPCMonitor/Turing/XuanFang "smart screen"
and decompiled all three vendor apps — none match (`8040` uses none of their
protocols). The real protocol is the custom `theme`/`model` one above, obtained
by capturing the panel's own vendor software. Full detail in git history.
