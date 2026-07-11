"""Host metrics collected from /proc and /sys — no external dependencies.

Rates (CPU %, network throughput) are computed between successive .sample()
calls, so keep one Collector instance and call it on your refresh interval.
"""
import os
import time


def _read(path, default=""):
    try:
        with open(path) as f:
            return f.read()
    except OSError:
        return default


def loadavg():
    p = _read("/proc/loadavg").split()
    return tuple(float(x) for x in p[:3]) if len(p) >= 3 else (0.0, 0.0, 0.0)


def _meminfo():
    d = {}
    for line in _read("/proc/meminfo").splitlines():
        k, _, rest = line.partition(":")
        d[k.strip()] = int(rest.split()[0]) * 1024  # kB -> bytes
    return d


def memory():
    m = _meminfo()
    total = m.get("MemTotal", 0)
    avail = m.get("MemAvailable", m.get("MemFree", 0))
    used = total - avail
    return {
        "total": total, "used": used, "avail": avail,
        "percent": (used / total * 100) if total else 0.0,
        "cached": m.get("Cached", 0), "buffers": m.get("Buffers", 0),
    }


def swap():
    m = _meminfo()
    total = m.get("SwapTotal", 0)
    free = m.get("SwapFree", 0)
    used = total - free
    return {"total": total, "used": used, "free": free,
            "percent": (used / total * 100) if total else 0.0}


def zram():
    """Aggregate zram device usage from /sys/block/zram*/mm_stat."""
    orig = compr = 0
    found = False
    import glob
    for dev in glob.glob("/sys/block/zram*/mm_stat"):
        found = True
        parts = _read(dev).split()
        if len(parts) >= 3:
            orig += int(parts[0])
            compr += int(parts[2])
    if not found:
        return None
    ratio = (orig / compr) if compr else 0.0
    return {"orig": orig, "compressed": compr, "ratio": ratio}


def disks(mounts=("/",)):
    out = {}
    for mp in mounts:
        try:
            s = os.statvfs(mp)
        except OSError:
            continue
        total = s.f_blocks * s.f_frsize
        free = s.f_bavail * s.f_frsize
        used = total - free
        out[mp] = {"total": total, "used": used, "free": free,
                   "percent": (used / total * 100) if total else 0.0}
    return out


def temperatures():
    """CPU/other temps in °C from /sys/class/hwmon and /sys/class/thermal.

    Often unavailable inside containers; use a Beszel source there instead.
    """
    import glob
    temps = {}
    for zone in glob.glob("/sys/class/thermal/thermal_zone*"):
        t = _read(os.path.join(zone, "type")).strip()
        raw = _read(os.path.join(zone, "temp")).strip()
        if t and raw:
            temps[t] = int(raw) / 1000.0
    for hw in glob.glob("/sys/class/hwmon/hwmon*"):
        name = _read(os.path.join(hw, "name")).strip() or os.path.basename(hw)
        for inp in glob.glob(os.path.join(hw, "temp*_input")):
            raw = _read(inp).strip()
            if raw:
                lbl = _read(inp.replace("_input", "_label")).strip()
                key = f"{name}/{lbl}" if lbl else f"{name}/{os.path.basename(inp)}"
                temps[key] = int(raw) / 1000.0
    # drop implausible readings from unused/disconnected sensors
    return {k: v for k, v in temps.items() if -10.0 < v < 125.0}


def _cpu_times():
    line = _read("/proc/stat").splitlines()[0].split()[1:]
    vals = [int(x) for x in line]
    idle = vals[3] + (vals[4] if len(vals) > 4 else 0)
    total = sum(vals)
    return total, idle


def _net_bytes():
    rx = tx = 0
    for line in _read("/proc/net/dev").splitlines()[2:]:
        iface, _, data = line.partition(":")
        iface = iface.strip()
        if iface == "lo":
            continue
        f = data.split()
        if len(f) >= 9:
            rx += int(f[0])
            tx += int(f[8])
    return rx, tx


class History:
    """Rolling history of scalar series for sparklines."""

    def __init__(self, maxlen=64):
        from collections import defaultdict, deque
        self.maxlen = maxlen
        self.series = defaultdict(lambda: deque(maxlen=maxlen))

    def push(self, stats):
        s = self.series
        s["cpu"].append(stats["cpu_percent"])
        s["mem"].append(stats["memory"]["percent"])
        s["rx"].append(stats["net"]["rx_bytes_s"])
        s["tx"].append(stats["net"]["tx_bytes_s"])

    def get(self, key):
        return list(self.series.get(key, []))


class Collector:
    """Stateful collector; call sample() each refresh for rate-based metrics."""

    def __init__(self, mounts=("/",)):
        self.mounts = mounts
        self._t = time.monotonic()
        self._cpu = _cpu_times()
        self._net = _net_bytes()

    def sample(self):
        now = time.monotonic()
        dt = max(now - self._t, 1e-6)

        total, idle = _cpu_times()
        dtotal = total - self._cpu[0]
        didle = idle - self._cpu[1]
        cpu_pct = (1 - didle / dtotal) * 100 if dtotal > 0 else 0.0

        rx, tx = _net_bytes()
        rx_rate = (rx - self._net[0]) / dt
        tx_rate = (tx - self._net[1]) / dt

        self._t, self._cpu, self._net = now, (total, idle), (rx, tx)

        return {
            "cpu_percent": max(0.0, min(100.0, cpu_pct)),
            "load": loadavg(),
            "ncpu": os.cpu_count() or 1,
            "memory": memory(),
            "swap": swap(),
            "zram": zram(),
            "disks": disks(self.mounts),
            "net": {"rx_bytes_s": rx_rate, "tx_bytes_s": tx_rate},
            "temps": temperatures(),
        }
