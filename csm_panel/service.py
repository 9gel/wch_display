"""The panel service: flash a theme once, then stream field values via 0x66.

Data-source agnostic. Each interval it runs the configured *provider* command,
reads ``{field_id: value}`` JSON from its stdout, and pushes those values to the
panel (a no-reset 0x66 update). Keeping the interval under ~30s also keeps the
panel awake. Survives the panel re-enumerating (it re-opens the port).
"""
import json
import subprocess
import time

from .config import Config
from .panel import Panel


class Service:
    def __init__(self, config: Config):
        self.cfg = config
        self.panel = None

    # --- panel connection (the panel re-enumerates after a flash / on wake) ---
    def _open(self, retries=30):
        for _ in range(retries):
            try:
                self.panel = Panel(port=self.cfg.port)
                self.panel.ser.write_timeout = 8
                return self.panel
            except Exception:
                time.sleep(1)
        return None

    def _flash_theme(self):
        with open(self.cfg.theme, "rb") as f:
            blob = f.read()
        if self._open() is None:
            raise RuntimeError(f"cannot open {self.cfg.port} to flash theme")
        ack = self.panel.send_theme(blob)
        self.panel.close()
        self.panel = None
        print(f"csm-panel: flashed theme {self.cfg.theme} ({len(blob)} bytes), ack={ack!r}")
        time.sleep(6)   # let it re-enumerate

    def _provider_values(self):
        """Run the provider command; parse its stdout as {field: value} JSON."""
        out = subprocess.run(self.cfg.provider, shell=True, capture_output=True,
                             text=True, timeout=self.cfg.interval + 10).stdout.strip()
        if not out:
            return {}
        return {int(k): int(round(float(v))) for k, v in json.loads(out).items()}

    def run(self):
        if self.cfg.theme:
            self._flash_theme()
        print(f"csm-panel: streaming 0x66 to {self.cfg.port} every {self.cfg.interval}s"
              + ("" if self.cfg.provider else " (no provider configured — nothing to push)"))
        while True:
            t0 = time.monotonic()
            values = {}
            if self.cfg.provider:
                try:
                    values = self._provider_values()
                except Exception as e:
                    print(f"[provider] {type(e).__name__}: {e}")
            if values:
                if self.panel is None and self._open() is None:
                    time.sleep(2); continue
                try:
                    self.panel.push_data(values, brightness=self.cfg.brightness)
                except Exception:
                    try: self.panel.close()
                    except Exception: pass
                    self.panel = None
            time.sleep(max(0.0, self.cfg.interval - (time.monotonic() - t0)))
