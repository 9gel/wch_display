"""Pluggable metric sources -> a normalized, render-ready schema.

A `Source` produces `Host` snapshots; the renderer only knows about `Host`
and `Metric`, so new backends (Beszel, Glances, Prometheus, …) just implement
`snapshot()`.
"""
from dataclasses import dataclass, field
from typing import List, Optional

from . import metrics as _m
from .render import fmt_bytes, fmt_rate


@dataclass
class Metric:
    key: str
    label: str
    value: float               # numeric current value
    text: str                  # formatted for display, e.g. "45%" / "3.9M/s"
    hist: List[float] = field(default_factory=list)
    vmin: Optional[float] = None   # fixed scale bottom (None = auto)
    vmax: Optional[float] = None   # fixed scale top
    sev: float = 0.0               # 0..1 criticality for colour
    kind: str = "pct"              # pct | rate | temp  (drives colouring)


@dataclass
class Host:
    name: str
    metrics: List[Metric]
    subtitle: str = ""
    online: bool = True


class Source:
    def snapshot(self) -> List[Host]:
        raise NotImplementedError


# metric-selection aliases used in config: friendly name -> key predicate
def select_metrics(host: "Host", spec) -> "Host":
    """Return `host` with its metrics filtered/ordered by a config spec list.

    Spec entries: "*" (all), a metric key ("cpu", "mem", "swap", "disk:/",
    "rx", "tx", "temp:CPU"), or a group ("net"=rx+tx, "temps"=all temp:*,
    "disk"=all disk:*). Aliases: "ram"->mem.
    """
    if not spec or "*" in spec:
        return host
    by_key = {m.key: m for m in host.metrics}
    order = []
    for entry in spec:
        e = "mem" if entry == "ram" else entry
        if e in by_key:
            order.append(by_key[e])
        elif e == "net":
            order += [m for m in host.metrics if m.key in ("rx", "tx")]
        elif e == "temps":
            order += [m for m in host.metrics if m.key.startswith("temp:")]
        elif e == "disk":
            order += [m for m in host.metrics if m.key.startswith("disk:")]
        elif e.startswith("temp:"):
            # allow fuzzy label match, e.g. temp:CPU
            want = e.split(":", 1)[1].lower()
            order += [m for m in host.metrics
                      if m.key.startswith("temp:") and want in m.label.lower()]
    # de-dup preserving order
    seen, uniq = set(), []
    for m in order:
        if id(m) not in seen:
            seen.add(id(m))
            uniq.append(m)
    host.metrics = uniq
    return host


def _pct_metric(key, label, pct, hist):
    return Metric(key, label, pct, f"{pct:.0f}%", hist, vmin=0, vmax=100, sev=pct / 100)


class LocalSource(Source):
    """Zero-config source: the machine running the service, via /proc."""

    def __init__(self, name=None, mounts=("/",), temps=("Tctl", "CPU Temperature",
                 "edge", "Composite")):
        import socket
        self.name = name or socket.gethostname()
        self.mounts = tuple(mounts)
        self.temp_want = tuple(temps)
        self._c = _m.Collector(mounts=self.mounts)
        self._h = _m.History(maxlen=64)
        self._temp_hist = {}
        self._c.sample()

    def snapshot(self) -> List[Host]:
        s = self._c.sample()
        self._h.push(s)
        ms = [
            _pct_metric("cpu", "CPU", s["cpu_percent"], self._h.get("cpu")),
            _pct_metric("mem", "RAM", s["memory"]["percent"], self._h.get("mem")),
        ]
        sw = s["swap"]
        if sw["total"] > 0:
            ms.append(_pct_metric("swap", "SWAP", sw["percent"], []))
        for mp, dk in s["disks"].items():
            lbl = "DISK" if mp == "/" else f"DISK {mp}"
            ms.append(_pct_metric(f"disk:{mp}", lbl, dk["percent"], []))
        net = s["net"]
        ms.append(Metric("rx", "NET ↓", net["rx_bytes_s"], fmt_rate(net["rx_bytes_s"]),
                         self._h.get("rx"), vmin=0, sev=0.0, kind="rate"))
        ms.append(Metric("tx", "NET ↑", net["tx_bytes_s"], fmt_rate(net["tx_bytes_s"]),
                         self._h.get("tx"), vmin=0, sev=0.0, kind="rate"))
        # temperatures (with per-sensor history)
        for name, val in _pick(s.get("temps") or {}, self.temp_want):
            h = self._temp_hist.setdefault(name, [])
            h.append(val)
            del h[:-64]
            ms.append(Metric(f"temp:{name}", name, val, f"{val:.0f}°", list(h),
                             vmin=30, vmax=90, sev=max(0, min(1, (val - 45) / 40)),
                             kind="temp"))
        sub = f"{fmt_bytes(s['memory']['total'])} · {s['ncpu']}c"
        return [Host(self.name, ms, subtitle=sub)]


def _pick(temps, want):
    aliases = {"Tctl": "CPU", "CPU Temperature": "CPU", "edge": "GPU",
               "Composite": "NVMe", "Chipset": "Chipset"}
    out, used = [], set()
    for w in want:
        for k, v in temps.items():
            if k in used:
                continue
            if w.lower() in k.lower():
                label = aliases.get(w, k.split("/")[-1][:10])
                if label not in [o[0] for o in out]:
                    out.append((label, v))
                    used.add(k)
                break
    return out[:4]
