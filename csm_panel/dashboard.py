"""Render normalized Host snapshots into a 480x800 portrait dashboard.

Every metric is drawn as a sparkline card with a clear fixed top/bottom scale,
a big current value, and criticality colour. Lays out 1-3 hosts adaptively.
Rendered at 2x and downsampled for smooth edges.
"""
import math
import time

from PIL import Image, ImageDraw

from . import render as R
from .render import peak_color, temp_color

SS = 2
BG = (12, 14, 20)
FG = (236, 240, 248)
DIM = (120, 130, 148)
OK = (86, 214, 120)
WARN = (245, 195, 70)
CRIT = (240, 80, 80)
CARD_BG = (24, 28, 38)
GRID = (52, 58, 72)

ACCENT = {"rx": (96, 200, 140), "tx": (235, 170, 110)}


def sev_color(frac, warn=0.75, crit=0.9):
    if frac >= crit:
        return CRIT
    if frac >= warn:
        return WARN
    return OK


def _accent(metric):
    if metric.kind == "temp":
        return temp_color(metric.value)
    if metric.key in ACCENT:
        return ACCENT[metric.key]
    return sev_color(metric.sev)


class _P:
    def __init__(self, draw):
        self.d = draw

    def text(self, x, y, s, size, fill, anchor=None, bold=True):
        self.d.text((x * SS, y * SS), s, font=R.font(int(size * SS), bold),
                    fill=fill, anchor=anchor)

    def tw(self, s, size, bold=True):
        return self.d.textlength(s, font=R.font(int(size * SS), bold)) / SS


def _card(d, p, x, y, w, h, m, value_size=26):
    d.rounded_rectangle([x * SS, y * SS, (x + w) * SS, (y + h) * SS],
                        radius=8 * SS, fill=CARD_BG)
    pad = 8
    col = _accent(m)
    # title is ~80% of the value number size (user request)
    p.text(x + pad, y + 6, m.label[:14], round(value_size * 0.8), (196, 204, 218))
    p.text(x + w - pad, y + 3, m.text, value_size, col, anchor="ra")
    # sparkline area
    sx, sy = x + pad, y + int(value_size * 1.25) + 6
    sw, sh = w - 2 * pad, y + h - sy - 8
    vals = m.hist or [m.value]
    if m.vmax is not None and m.vmin is not None:
        d.line([sx * SS, sy * SS, (sx + sw) * SS, sy * SS], fill=GRID, width=1)
        d.line([sx * SS, (sy + sh) * SS, (sx + sw) * SS, (sy + sh) * SS], fill=GRID, width=1)
        vmin, vmax = m.vmin, m.vmax
    else:
        vmin, vmax = min(vals + [0]), (max(vals) or 1)
    # every sparkline: blue at the bottom of its scale, red at the peaks
    span = (vmax - vmin) or 1.0
    _sparkline(d, sx, sy, sw, sh, vals, vmin, vmax,
               lambda v: peak_color((v - vmin) / span))


def _sparkline(d, x, y, w, h, values, vmin, vmax, color_of):
    """Fixed-scale sparkline; each segment coloured by value via color_of(v)."""
    span = (vmax - vmin) or 1.0
    n = len(values)
    step = w / max(n - 1, 1)

    def yof(v):
        return y + h - max(0.0, min(1.0, (v - vmin) / span)) * h
    pts = [(x + i * step, yof(v)) for i, v in enumerate(values)]
    if len(pts) == 1:
        pts = [(x, pts[0][1]), (x + w, pts[0][1])]
        values = [values[0], values[0]]
    poly = [(px * SS, py * SS) for px, py in pts]
    # translucent blue area fill (keeps the "mostly blue" feel)
    d.polygon([(pts[0][0] * SS, (y + h) * SS)] + poly + [(pts[-1][0] * SS, (y + h) * SS)],
              fill=(26, 42, 66))
    for i in range(len(poly) - 1):
        d.line([poly[i], poly[i + 1]], fill=color_of(values[i + 1]),
               width=2 * SS, joint="curve")


def render(hosts, config=None):
    config = config or {}
    cols = int(config.get("columns", 2))
    W, H = R.PORTRAIT
    img = Image.new("RGB", (W * SS, H * SS), BG)
    d = ImageDraw.Draw(img)
    p = _P(d)
    M = 16

    # compute adaptive card height so everything fills the full height
    y0 = 8
    title_h = 40
    gap = 8
    rows_total = sum(math.ceil(max(1, len(h.metrics)) / cols) for h in hosts)
    avail = H - y0 - 8 - len(hosts) * (title_h + gap)
    card_h = int(max(56, min(170, avail / max(rows_total, 1) - gap)))
    card_w = (W - 2 * M - (cols - 1) * gap) / cols
    value_size = int(max(18, min(38, card_h * 0.34 * 0.8)))   # 80% of previous

    y = y0
    for host in hosts:
        # host title: inverted (light) band so sections are easy to tell apart
        band_h = 32
        d.rounded_rectangle([8 * SS, y * SS, (W - 8) * SS, (y + band_h) * SS],
                            radius=8 * SS, fill=(224, 228, 236))
        dot = (36, 160, 92) if host.online else (200, 60, 60)
        d.ellipse([(M) * SS, (y + 11) * SS, (M + 12) * SS, (y + 23) * SS], fill=dot)
        p.text(M + 20, y + 4, host.name[:16], 22, (22, 26, 34))
        if host.updated:
            p.text(W - M, y + 4, host.updated, 22, (36, 84, 168), anchor="ra")
        y += title_h
        # cards grid
        for i, m in enumerate(host.metrics):
            cx = M + (i % cols) * (card_w + gap)
            cy = y + (i // cols) * (card_h + gap)
            _card(d, p, cx, cy, card_w, card_h, m, value_size)
        rows = math.ceil(max(1, len(host.metrics)) / cols)
        y += rows * (card_h + gap) + 6

    return img.resize((W, H), Image.LANCZOS)
