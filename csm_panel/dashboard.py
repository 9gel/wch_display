"""Compose host metrics into a bold 480x800 portrait dashboard.

Designed to be glanceable from ~1.5 m: large ring gauges, sparklines,
and criticality colours. Rendered at 2x and downsampled for smooth edges.
"""
import socket
import time

from PIL import Image, ImageDraw

from . import render as R
from .render import fmt_bytes, fmt_rate, grad_color, hbar, ring_gauge, sparkline

SS = 2  # supersampling factor
BG = (12, 14, 20)
FG = (236, 240, 248)
DIM = (120, 130, 148)

OK = (86, 214, 120)
WARN = (245, 195, 70)
CRIT = (240, 80, 80)


def sev_color(frac, warn=0.75, crit=0.9):
    if frac >= crit:
        return CRIT
    if frac >= warn:
        return WARN
    return OK


def _pick_temps(temps, want=("Tctl", "CPU Temperature", "edge", "Composite",
                             "Chipset", "GPU")):
    """Choose up to 4 interesting temps with friendly short labels."""
    chosen = []
    used = set()
    aliases = {"Tctl": "CPU", "CPU Temperature": "CPU", "edge": "GPU",
               "Composite": "NVMe", "Chipset": "Chipset"}
    for w in want:
        for k, v in temps.items():
            if k in used:
                continue
            if w.lower() in k.lower():
                label = aliases.get(w, k.split("/")[-1])
                if label in [c[0] for c in chosen]:
                    continue
                chosen.append((label, v))
                used.add(k)
                break
    for k, v in temps.items():           # fill remaining slots
        if k not in used and len(chosen) < 4:
            chosen.append((k.split("/")[-1][:10], v))
            used.add(k)
    return chosen[:4]


class _P:
    """Scaled painter: all coords/sizes given in logical px, drawn at SSx."""

    def __init__(self, draw):
        self.d = draw

    def text(self, x, y, s, size, fill, anchor=None, bold=True):
        self.d.text((x * SS, y * SS), s, font=R.font(size * SS, bold), fill=fill, anchor=anchor)

    def tw(self, s, size, bold=True):
        return self.d.textlength(s, font=R.font(size * SS, bold)) / SS


def render(stats, history=None, config=None, hostname=None):
    config = config or {}
    hostname = hostname or socket.gethostname()
    W, H = R.PORTRAIT
    img = Image.new("RGB", (W * SS, H * SS), BG)
    d = ImageDraw.Draw(img)
    p = _P(d)
    M = 20

    def hist(key):
        return history.get(key) if history else []

    # ---- header ----
    p.text(M, 14, hostname[:18], 30, FG)
    clock = time.strftime("%H:%M:%S")
    p.text(W - M, 12, clock, 34, (110, 200, 255), anchor="ra")
    d.line([M * SS, 62 * SS, (W - M) * SS, 62 * SS], fill=(44, 48, 60), width=SS)

    # ---- CPU + RAM rings ----
    cpu = stats["cpu_percent"] / 100.0
    mem = stats["memory"]["percent"] / 100.0
    ring_y = 180
    for cx, frac, label, sub in [
        (128, cpu, "CPU", f"{stats['load'][0]:.1f}·{stats['ncpu']}c"),
        (352, mem, "RAM", f"{fmt_bytes(stats['memory']['used'])}/{fmt_bytes(stats['memory']['total'])}"),
    ]:
        ring_gauge(d, cx * SS, ring_y * SS, 88 * SS, frac, grad_color(frac),
                   thickness=18 * SS)
        p.text(cx, ring_y - 30, f"{frac*100:.0f}", 52, sev_color(frac), anchor="ma")
        p.text(cx, ring_y + 26, "%", 20, DIM, anchor="ma")
        p.text(cx, 74, label, 20, DIM, anchor="ma")
        p.text(cx, ring_y + 100, sub, 17, (170, 178, 194), anchor="ma")

    # sparklines under each ring
    sparkline(d, (128 - 80) * SS, 300 * SS, 160 * SS, 34 * SS, hist("cpu"),
              (110, 200, 255), fill=(28, 44, 66))
    sparkline(d, (352 - 80) * SS, 300 * SS, 160 * SS, 34 * SS, hist("mem"),
              (150, 130, 240), fill=(40, 34, 66))

    # ---- swap + disk bars ----
    y = 358
    rows = []
    sw = stats["swap"]
    if sw["total"] > 0:
        zr = stats.get("zram")
        sub = f"{fmt_bytes(sw['used'])} / {fmt_bytes(sw['total'])}"
        if zr and zr["compressed"]:
            sub += f"  ·zram x{zr['ratio']:.1f}"
        rows.append(("SWAP", sw["percent"] / 100.0, sub))
    for mp, dk in stats["disks"].items():
        rows.append((f"DISK {mp}", dk["percent"] / 100.0,
                     f"{fmt_bytes(dk['used'])} / {fmt_bytes(dk['total'])}"))
    for label, frac, sub in rows:
        p.text(M, y, label, 19, DIM)
        p.text(W - M, y - 4, f"{frac*100:.0f}%", 26, sev_color(frac), anchor="ra")
        hbar(d, M * SS, (y + 26) * SS, (W - 2 * M) * SS, 14 * SS, frac,
             grad_color(frac), radius=6 * SS)
        p.text(M, y + 44, sub, 16, (150, 158, 174))
        y += 74

    # ---- network ----
    net = stats["net"]
    p.text(M, y, "NETWORK", 18, DIM)
    y += 26
    p.text(M, y, f"↓ {fmt_rate(net['rx_bytes_s'])}", 26, OK)
    p.text(W - M, y, f"↑ {fmt_rate(net['tx_bytes_s'])}", 26, (235, 170, 110), anchor="ra")
    y += 36
    half = (W - 2 * M - 12) / 2
    sparkline(d, M * SS, y * SS, half * SS, 30 * SS, hist("rx"), OK, fill=(24, 50, 34))
    sparkline(d, (M + half + 12) * SS, y * SS, half * SS, 30 * SS, hist("tx"),
              (235, 170, 110), fill=(56, 40, 26))
    y += 48

    # ---- temperature tiles ----
    temps = _pick_temps(stats.get("temps") or {})
    if temps:
        p.text(M, y, "TEMPERATURES", 18, DIM)
        y += 26
        tw = (W - 2 * M - 12) / 2
        th = 66
        for i, (label, val) in enumerate(temps):
            tx = M + (i % 2) * (tw + 12)
            ty = y + (i // 2) * (th + 10)
            frac = max(0.0, min(1.0, (val - 40) / 45))   # 40..85C
            d.rounded_rectangle([tx * SS, ty * SS, (tx + tw) * SS, (ty + th) * SS],
                                radius=10 * SS, fill=(26, 30, 40))
            p.text(tx + 12, ty + 8, label[:12], 16, DIM)
            p.text(tx + 12, ty + 26, f"{val:.0f}°", 32, sev_color(frac, 0.55, 0.8))
            hbar(d, (tx + 12) * SS, (ty + th - 12) * SS, (tw - 24) * SS, 6 * SS,
                 frac, grad_color(frac), radius=3 * SS)

    # ---- footer ----
    p.text(M, H - 22, time.strftime("%Y-%m-%d %a"), 15, (86, 94, 108))

    return img.resize((W, H), Image.LANCZOS)
