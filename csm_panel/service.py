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
            self.beszel = BeszelSource([hc for _, hc in beszel_hosts],
                                       self.cfg.resolved_beszel())

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
        if self.panel is not None:
            return
        # the panel re-enumerates after a flash and sleeps when idle; retry opens.
        last = None
        for _ in range(30):
            try:
                self.panel = Panel(port=self.cfg.port)
                return
            except Exception as e:
                last = e
                time.sleep(1)
        raise last

    def flash_once(self):
        self._ensure_panel()
        buf = self.frame()
        return self.panel.flash(buf, quality=self.cfg.quality)

    @staticmethod
    def _signature(hosts):
        """A coarse fingerprint of the displayed values, to detect real change.

        Each theme flash resets the panel (it writes NAND + reboots to apply),
        so we only re-flash when the rounded values change — not every tick.
        Network rates are ignored (too noisy to drive a re-flash).
        """
        sig = []
        for h in hosts:
            sig.append(h.online)
            for m in h.metrics:
                if m.kind == "rate":
                    continue
                sig.append(round(m.value))
        return tuple(sig)

    # --- no-reset widget mode (temperatures) -------------------------------
    @staticmethod
    def _host_temp(host):
        """Pick the temperature to display for a host (prefer a CPU sensor)."""
        temps = [m for m in host.metrics if m.kind == "temp"]
        if not temps:
            return None
        for m in temps:
            if "cpu" in m.label.lower():
                return m.value
        return max(m.value for m in temps)

    def run(self):
        if self.cfg.mode == "widget":
            return self.run_widget()
        return self.run_image()

    def run_widget(self):
        """Flash a widget theme ONCE, then stream 0x66 temps forever (no reset)."""
        import os

        from . import widget_theme as wt
        base_path = self.cfg.base_theme
        if not os.path.isabs(base_path):
            base_path = os.path.join(os.path.dirname(os.path.dirname(__file__)), base_path)
        base = open(base_path, "rb").read()
        accents = [(70, 200, 120), (90, 170, 230), (230, 170, 90)]

        print(f"csm-panel [widget]: {len(self.cfg.hosts)} host(s) on {self.cfg.port}, "
              f"push every {self.cfg.interval}s (flash once, then no-reset 0x66)")
        flashed_labels = None
        while True:
            t0 = time.monotonic()
            try:
                hosts = self.collect()
                labels = tuple(h.name for h in hosts[:3])
                self._ensure_panel()
                if labels != flashed_labels:            # (re)flash only if labels change
                    panels = [(h.name, "online" if h.online else "offline", accents[i])
                              for i, h in enumerate(hosts[:3])]
                    theme = wt.build_theme(base, wt.render_background(panels))
                    ack = self.panel.send_theme(theme)
                    if ack == b"C":
                        flashed_labels = labels
                    time.sleep(6)                       # let it re-enumerate after flash
                    self.panel = None
                    self._ensure_panel()
                temps = [self._host_temp(h) for h in hosts[:3]]
                self.panel.push_data(wt.temp_values(temps))
            except Exception as e:
                print(f"[error] {type(e).__name__}: {e}; reopening panel")
                try:
                    if self.panel:
                        self.panel.close()
                except Exception:
                    pass
                self.panel = None
                flashed_labels = None
            dt = time.monotonic() - t0
            time.sleep(max(0.0, self.cfg.interval - dt))

    def run_image(self):
        print(f"csm-panel [image]: {len(self.cfg.hosts)} host(s), sampling every "
              f"{self.cfg.interval}s on {self.cfg.port} "
              f"(re-flash on change; heartbeat {self.cfg.heartbeat}s)")
        last_sig = None
        last_flash = 0.0
        while True:
            t0 = time.monotonic()
            try:
                hosts = self.collect()               # always sample (advances history)
                sig = self._signature(hosts)
                now = time.monotonic()
                if sig != last_sig or (now - last_flash) >= self.cfg.heartbeat:
                    self._ensure_panel()
                    img = dashboard.render(hosts, config={"columns": self.cfg.columns})
                    buf = render.to_panel(img, rotate=self.cfg.rotate)
                    ack = self.panel.flash(buf, quality=self.cfg.quality)
                    if ack != b"C":
                        print(f"[warn] panel did not ack (got {ack!r})")
                    last_sig = sig
                    last_flash = time.monotonic()
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
