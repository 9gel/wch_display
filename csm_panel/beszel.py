"""Beszel hub as a metric source (multi-host).

Beszel stores each machine in the PocketBase `systems` collection (current
`info`) and time-series in `system_stats` (used for sparklines + temperatures).
Configure hub url + credentials in config; use `csm-panel beszel-probe` to see
exactly what your hub returns if field names need adjusting.
"""
import json
import urllib.error
import urllib.parse
import urllib.request
from typing import List

from .render import fmt_rate
from .sources import Host, Metric, Source


class BeszelClient:
    def __init__(self, url, email=None, password=None, token=None, timeout=6):
        self.url = url.rstrip("/")
        self.email, self.password, self.token = email, password, token
        self.timeout = timeout

    def _req(self, path, data=None, auth=True):
        url = self.url + path
        headers = {"Content-Type": "application/json"}
        if auth and self.token:
            headers["Authorization"] = self.token
        body = json.dumps(data).encode() if data is not None else None
        req = urllib.request.Request(url, data=body, headers=headers,
                                     method="POST" if data is not None else "GET")
        with urllib.request.urlopen(req, timeout=self.timeout) as r:
            return json.loads(r.read().decode())

    def authenticate(self):
        if self.token:
            return self.token
        for coll in ("users", "_superusers"):
            try:
                r = self._req(f"/api/collections/{coll}/auth-with-password",
                              {"identity": self.email, "password": self.password},
                              auth=False)
                self.token = r["token"]
                return self.token
            except urllib.error.HTTPError:
                continue
        raise RuntimeError("Beszel auth failed (check url/email/password)")

    def systems(self):
        r = self._req("/api/collections/systems/records?perPage=200")
        return r.get("items", [])

    def stats(self, system_id, count=60, kind="1m"):
        flt = urllib.parse.quote(f"system='{system_id}' && type='{kind}'")
        r = self._req(f"/api/collections/system_stats/records"
                      f"?filter={flt}&sort=-created&perPage={count}")
        return list(reversed(r.get("items", [])))  # oldest -> newest


def _g(dct, *keys, default=0.0):
    for k in keys:
        if isinstance(dct, dict) and k in dct and dct[k] is not None:
            return dct[k]
    return default


class BeszelSource(Source):
    def __init__(self, hosts_cfg, beszel_cfg):
        self.client = BeszelClient(
            url=beszel_cfg.get("url", "http://127.0.0.1:8090"),
            email=beszel_cfg.get("email"), password=beszel_cfg.get("password"),
            token=beszel_cfg.get("token"))
        self.hosts_cfg = hosts_cfg   # list of HostConfig with source=="beszel"
        self._authed = False

    def _ensure(self):
        if not self._authed:
            self.client.authenticate()
            self._authed = True

    def snapshot(self) -> List[Host]:
        self._ensure()
        systems = {s.get("name"): s for s in self.client.systems()}
        out = []
        for hc in self.hosts_cfg:
            want = hc.system or hc.name
            sysrec = systems.get(want)
            if not sysrec:
                out.append(Host(hc.name, [], subtitle="not found", online=False))
                continue
            out.append(self._build(hc, sysrec))
        return out

    def _build(self, hc, sysrec):
        info = sysrec.get("info", {}) or {}
        online = sysrec.get("status") in ("up", "running", None) and \
            sysrec.get("status") != "down"
        series = []
        try:
            series = self.client.stats(sysrec["id"], count=60)
        except Exception:
            pass
        stats = [s.get("stats", {}) or {} for s in series]

        def hist(*keys):
            return [float(_g(s, *keys)) for s in stats] if stats else []

        cpu = float(_g(info, "cpu"))
        mem = float(_g(info, "mp", "memPct", "mem"))
        disk = float(_g(info, "dp", "diskPct"))
        ms = [
            Metric("cpu", "CPU", cpu, f"{cpu:.0f}%", hist("cpu"), 0, 100, cpu / 100),
            Metric("mem", "RAM", mem, f"{mem:.0f}%", hist("mp", "memPct"), 0, 100, mem / 100),
        ]
        if disk:
            ms.append(Metric("disk:/", "DISK", disk, f"{disk:.0f}%",
                             hist("dp", "diskPct"), 0, 100, disk / 100))
        # network (Beszel stores sent/recv, often MB/s)
        ns = float(_g(info, "ns", "bs", default=0)) * 1e6
        nr = float(_g(info, "nr", "br", default=0)) * 1e6
        if series:
            ms.append(Metric("rx", "NET ↓", nr, fmt_rate(nr),
                             [float(_g(s, "nr", "br")) * 1e6 for s in stats], 0, None, kind="rate"))
            ms.append(Metric("tx", "NET ↑", ns, fmt_rate(ns),
                             [float(_g(s, "ns", "bs")) * 1e6 for s in stats], 0, None, kind="rate"))
        # temperatures: latest stats["t"] is a {sensor: celsius} map
        temps = {}
        for s in stats:
            t = s.get("t") or s.get("temps") or {}
            if isinstance(t, dict):
                temps = t
        for name, val in list(temps.items())[:6]:
            val = float(val)
            th = [float((s.get("t") or {}).get(name, val)) for s in stats]
            ms.append(Metric(f"temp:{name}", name[:12], val, f"{val:.0f}°", th,
                             30, 90, max(0, min(1, (val - 45) / 40)), kind="temp"))
        sub = _g(info, "m", "h", default="") or ""
        return Host(hc.name, ms, subtitle=str(sub)[:22], online=bool(online))
