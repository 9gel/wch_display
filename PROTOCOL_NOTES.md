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

## No-reset live updates (the widget path)

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
BE16` (over the true bytes). Device acks `C` (0x43); `0x15` = NAK/resend. Max
image 4 MB. Control commands (64-byte name, rest zero): `boot` (app → bootloader,
`slotBootMode`), `reset` (reboot / jump to app, `slotResetMcu`).

**Recovery:** an invalid hand-authored theme can make the *running app* write a
bad on-device data table ("MDT"), after which the bootloader refuses to launch
the app ("MDT Error") — theme writes are ignored in boot mode, so only a firmware
flash fixes it. With the panel in boot mode:

    csm-panel flash-firmware <update_*.bin> --flash

The correct image for this panel is named **`update_sdnand_800480_*.bin`** (NOT
`update_S021*` / `update_SM050*`, which are other models); get it from the
vendor/seller. See `csm_panel/firmware.py`.

## Theme file format & the `.ui` compiler

The vendor Windows "Theme Editor" saves themes as `*.ui` files and compiles them
to the blob you flash. Both are reverse-engineered; `csm_panel/theme/` reimplements
them on Linux.

**`.ui` files** are **RC4-encrypted XML** (key
`"This product is designed by OuJianbo,zhe ge chan pin shi gzbkey she ji de"`;
RC4 is symmetric so the same op decrypts and encrypts). XML is `<ui><widgetParent
background/>` + `<widget type=N>` with `<geometry>` and, for data widgets,
`<sensor><fastSensor>` (= the `0x66` field id; 0 = static). `.ui` types: 2=StaticText,
3=ProgressBar, 5=Number, 6=DateTime, 4=Image.

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
  **resource area** of 8-bpp coverage masks for StaticText/DateTime glyphs.

**Coordinate transform (portrait themes):** the 480×800 canvas is resliced into
256-px-tall bands packed left-to-right in the panel's landscape framebuffer:
`bl_x = ui_x%256 + 256*(ui_y//256)`, `bl_y = ui_y%256` (landscape themes are
identity). This is why the editor's preview isn't pixel-WYSIWYG.

The compiler (`csm_panel/theme/compiler.py`) rebuilds the descriptor + widget
table from a `.ui` and **reuses a known-good base blob's records + resource area**
(background JPEG + glyph masks), since pixel-exact glyph synthesis isn't
implemented. It reproduces a real vendor blob **byte-for-byte**.

## History / dead-ends (condensed)
Earlier we mis-identified this as a UsbPCMonitor/Turing/XuanFang "smart screen"
and decompiled all three vendor apps — none match (`8040` uses none of their
protocols). The real protocol is the custom `theme`/`model` one above, obtained
by capturing the panel's own vendor software. Full detail in git history.
