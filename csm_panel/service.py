"""Dashboard service: collect metrics -> render -> flash, on an interval."""
import time

from . import dashboard, render
from .config import Config
from .panel import Panel
from .sources import LocalSource, StubSource, select_metrics


class Service:
    def __init__(self, config: Config):
        self.cfg = config
        self.panel = None
        self._build_sources()

    def _build_sources(self):
        self.local = {}          # host index -> LocalSource
        self.beszel = None
        beszel_hosts = []
        for i, hc in enumerate(self.cfg.hosts):
            if hc.source == "local":
                self.local[i] = LocalSource(name=hc.name, mounts=tuple(hc.mounts))
            elif hc.source == "stub":
                self.local[i] = StubSource(name=hc.name,
                                           sensors=hc.options.get("sensors"))
            elif hc.source == "beszel":
                beszel_hosts.append((i, hc))
        if beszel_hosts:
            from .beszel import BeszelSource
            self._beszel_idx = [i for i, _ in beszel_hosts]
            self.beszel = BeszelSource([hc for _, hc in beszel_hosts], self.cfg.beszel)

    def collect(self):
        """Return Host list in config order, with per-host metric selection."""
        beszel_hosts = {}
        if self.beszel is not None:
            try:
                snaps = self.beszel.snapshot()
                beszel_hosts = dict(zip(self._beszel_idx, snaps))
            except Exception as e:  # keep panel alive if hub is down
                print(f"[beszel] {e}")
        out = []
        for i, hc in enumerate(self.cfg.hosts):
            if i in self.local:
                host = self.local[i].snapshot()[0]
            elif i in beszel_hosts:
                host = beszel_hosts[i]
            else:
                continue
            out.append(select_metrics(host, hc.metrics))
        return out

    def frame(self):
        hosts = self.collect()
        img = dashboard.render(hosts, config={"columns": self.cfg.columns})
        return render.to_panel(img, rotate=self.cfg.rotate)

    def _ensure_panel(self):
        if self.panel is None:
            self.panel = Panel(port=self.cfg.port)

    def flash_once(self):
        self._ensure_panel()
        buf = self.frame()
        return self.panel.flash(buf, quality=self.cfg.quality)

    def run(self):
        print(f"csm-panel: {len(self.cfg.hosts)} host(s), every {self.cfg.interval}s "
              f"on {self.cfg.port}")
        while True:
            t0 = time.monotonic()
            try:
                self._ensure_panel()
                buf = self.frame()
                ack = self.panel.flash(buf, quality=self.cfg.quality)
                if ack != b"C":
                    print(f"[warn] panel did not ack (got {ack!r})")
            except Exception as e:
                print(f"[error] {type(e).__name__}: {e}; reopening panel")
                try:
                    if self.panel:
                        self.panel.close()
                except Exception:
                    pass
                self.panel = None
            dt = time.monotonic() - t0
            time.sleep(max(0.0, self.cfg.interval - dt))
