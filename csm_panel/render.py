"""Rendering helpers: a portrait canvas that maps to the panel buffer.

The panel is physically mounted portrait. Content is authored on a 480x800
portrait canvas ("what the user sees") and rotated CCW to the 800x480 buffer
the hardware expects (confirmed on real hardware).
"""
from PIL import Image, ImageDraw, ImageFont

PORTRAIT = (480, 800)     # authoring canvas (user's view)
PANEL = (800, 480)        # hardware buffer

_FONT_CACHE = {}
_PATH_CACHE = {}
# fixed candidate paths first (fast); fc-match resolves the rest on nix/others.
_CANDIDATES = {
    True: ["/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"],
    False: ["/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"],
}


def _resolve_path(bold):
    if bold in _PATH_CACHE:
        return _PATH_CACHE[bold]
    import os
    import subprocess
    path = None
    # explicit override (set by packaging/service so no fontconfig is needed)
    env = os.environ.get("CSM_PANEL_FONT_BOLD" if bold else "CSM_PANEL_FONT")
    if env and os.path.exists(env):
        _PATH_CACHE[bold] = env
        return env
    for p in _CANDIDATES[bold]:
        if os.path.exists(p):
            path = p
            break
    if path is None:
        pattern = "DejaVu Sans:bold" if bold else "DejaVu Sans"
        try:
            out = subprocess.run(["fc-match", "-f", "%{file}", pattern],
                                 capture_output=True, text=True, timeout=5)
            if out.stdout.strip() and os.path.exists(out.stdout.strip()):
                path = out.stdout.strip()
        except (OSError, subprocess.SubprocessError):
            pass
    _PATH_CACHE[bold] = path
    return path


def font(size, bold=True):
    key = (size, bold)
    if key in _FONT_CACHE:
        return _FONT_CACHE[key]
    path = _resolve_path(bold)
    f = ImageFont.truetype(path, size) if path else ImageFont.load_default()
    _FONT_CACHE[key] = f
    return f


def new_canvas(bg=(12, 14, 20)):
    img = Image.new("RGB", PORTRAIT, bg)
    return img, ImageDraw.Draw(img)


_ROTATE = {
    "ccw": Image.ROTATE_90,    # default: confirmed correct on hardware
    "cw": Image.ROTATE_270,
    "180": Image.ROTATE_180,
}


def to_panel(portrait_img: Image.Image, rotate: str = "ccw") -> Image.Image:
    """Map the 480x800 portrait canvas into the 800x480 panel buffer.

    `rotate` selects the mounting orientation (ccw is the verified default).
    "none" assumes the image is already 800x480.
    """
    if rotate == "none":
        return portrait_img if portrait_img.size == PANEL else portrait_img.resize(PANEL)
    if portrait_img.size != PORTRAIT:
        portrait_img = portrait_img.resize(PORTRAIT)
    return portrait_img.transpose(_ROTATE.get(rotate, Image.ROTATE_90))


def ring_gauge(draw, cx, cy, r, frac, fg, bg=(38, 42, 52), thickness=22):
    """Circular gauge: background ring + foreground arc for `frac`."""
    frac = max(0.0, min(1.0, frac))
    box = [cx - r, cy - r, cx + r, cy + r]
    draw.arc(box, 0, 360, fill=bg, width=thickness)
    if frac > 0:
        draw.arc(box, -90, -90 + int(360 * frac), fill=fg, width=thickness)


def sparkline(draw, x, y, w, h, values, color, fill=None, baseline=True):
    """Area/line sparkline of `values` scaled to its own min..max."""
    if baseline:
        draw.line([x, y + h, x + w, y + h], fill=(46, 50, 62), width=1)
    if not values:
        return
    vmin, vmax = min(values), max(values)
    span = (vmax - vmin) or 1.0
    n = len(values)
    step = w / max(n - 1, 1)
    pts = [(x + i * step, y + h - (v - vmin) / span * h) for i, v in enumerate(values)]
    if fill:
        draw.polygon([(x, y + h)] + pts + [(x + w, y + h)], fill=fill)
    if len(pts) > 1:
        draw.line(pts, fill=color, width=2, joint="curve")


def hbar(draw, x, y, w, h, frac, fg, bg=(40, 44, 54), radius=4):
    frac = max(0.0, min(1.0, frac))
    draw.rounded_rectangle([x, y, x + w, y + h], radius=radius, fill=bg)
    fw = int(w * frac)
    if fw > 2:
        draw.rounded_rectangle([x, y, x + fw, y + h], radius=radius, fill=fg)


def _interp_stops(x, stops):
    """Interpolate an RGB colour from (value, color) stops (ascending)."""
    if x <= stops[0][0]:
        return stops[0][1]
    if x >= stops[-1][0]:
        return stops[-1][1]
    for (x0, c0), (x1, c1) in zip(stops, stops[1:]):
        if x0 <= x <= x1:
            t = (x - x0) / (x1 - x0)
            return tuple(int(c0[i] + (c1[i] - c0[i]) * t) for i in range(3))
    return stops[-1][1]


# temperature colour scale: blue (cool) -> red (hot)
TEMP_STOPS = [(30, (70, 140, 255)), (45, (60, 200, 200)), (58, (90, 210, 120)),
              (70, (240, 200, 70)), (80, (240, 130, 55)), (90, (240, 55, 55))]


def temp_color(celsius):
    return _interp_stops(celsius, TEMP_STOPS)


# generic sparkline scale: blue (low) -> red (peak), used for every metric
PEAK_STOPS = [(0.0, (74, 150, 255)), (0.45, (66, 200, 190)), (0.7, (235, 205, 70)),
              (0.88, (240, 130, 55)), (1.0, (240, 60, 60))]


def peak_color(frac):
    return _interp_stops(max(0.0, min(1.0, frac)), PEAK_STOPS)


def grad_color(frac, cold=(70, 200, 120), warm=(240, 190, 60), hot=(235, 80, 80)):
    """Green→amber→red as frac goes 0→1."""
    frac = max(0.0, min(1.0, frac))
    if frac < 0.5:
        t = frac / 0.5
        a, b = cold, warm
    else:
        t = (frac - 0.5) / 0.5
        a, b = warm, hot
    return tuple(int(a[i] + (b[i] - a[i]) * t) for i in range(3))


def fmt_bytes(n, suffix="B"):
    n = float(n)
    for unit in ("", "K", "M", "G", "T", "P"):
        if abs(n) < 1024:
            return f"{n:.0f}{unit}{suffix}" if unit == "" else f"{n:.1f}{unit}{suffix}"
        n /= 1024
    return f"{n:.1f}E{suffix}"


def fmt_rate(bytes_per_s):
    return fmt_bytes(bytes_per_s) + "/s"
