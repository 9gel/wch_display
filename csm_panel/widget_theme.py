"""No-reset temperature dashboard using on-device widgets.

The panel resets on every theme flash, so for live data we flash ONCE and then
stream 0x66 value frames (no reset). Authoring widgets from scratch renders
blank (the digit glyphs live in a vendor "resource area"), so we reuse a
captured vendor theme blob (`data/base_theme.bin`, the upright "Simplicity"
theme: byte[1]=0x02, three big-number widgets on fields 3/7/12) and only swap
its background image to relabel the three panels. See PROTOCOL_NOTES.md.
"""
import struct
from io import BytesIO

from PIL import Image, ImageDraw

from . import render as R

ORIENT_UPRIGHT = 0x02          # theme blob byte[1]: orientation for the mount
PANEL_FIELDS = [3, 7, 12]      # 0x66 fields of the 3 big-number widgets (top->bottom)
PANEL_Y = [108, 248, 388]      # background y of each panel (matches the widget layout)
PANEL_W = (480, 800)


def render_background(panels, title="TEMPERATURES"):
    """panels: up to 3 (label, subtitle, accent_rgb). -> 480x800 JPEG bytes."""
    bg = Image.new("RGB", PANEL_W, (6, 8, 14))
    d = ImageDraw.Draw(bg)
    d.text((40, 16), title, font=R.font(30), fill=(200, 210, 225))
    for (label, sub, accent), y in zip(panels, PANEL_Y):
        d.rounded_rectangle([40, y, 445, y + 96], radius=10, outline=accent, width=3)
        d.text((52, y + 60), label[:16], font=R.font(28), fill=(232, 240, 150))
        d.text((406, y + 6), "°C", font=R.font(24), fill=(120, 200, 220))
        if sub:
            d.text((52, y + 8), sub[:22], font=R.font(15), fill=(140, 150, 165))
    buf = BytesIO()
    bg.save(buf, format="JPEG", quality=88)
    return buf.getvalue()


def build_theme(base_blob: bytes, background_jpeg: bytes) -> bytes:
    """Reuse a base theme blob, swapping only its background (record0).

    Keeps the widget table + resource area (digit glyphs), sets upright
    orientation, and shifts the 0x93 static-image pointers by the change in
    background size so they still point into the (moved) resource area.
    """
    sz = int.from_bytes(base_blob[0x1000:0x1004], "big")
    res_start = 0x1004 + sz
    content_len = int.from_bytes(base_blob[0x59:0x5c], "big")
    resource = base_blob[res_start:content_len]
    header = bytearray(base_blob[:0x1000])
    header[1] = ORIENT_UPRIGHT
    delta = len(background_jpeg) - sz

    i = 0x80
    while i < 0x1000:
        e = header[i:i + 0x40]
        if not any(e):
            break
        if e[0] == 0x93:
            ptr = int.from_bytes(e[12:15], "big")
            if ptr >= res_start:
                header[i + 12:i + 15] = (ptr + delta).to_bytes(3, "big")
        i += 0x40

    blob = bytearray(bytes(header) + struct.pack(">I", len(background_jpeg))
                     + background_jpeg + resource)
    blob[0x59:0x5c] = len(blob).to_bytes(3, "big")
    return bytes(blob)


def temp_values(temps):
    """Map a list of per-host temps (°C) to the 0x66 field->value dict."""
    return {PANEL_FIELDS[i]: max(0, min(999, int(round(t))))
            for i, t in enumerate(temps[:3]) if t is not None}
