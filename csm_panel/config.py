"""Configuration loading (TOML). See ``config.example.toml`` for the schema.

The service is deliberately data-source agnostic: it flashes a theme blob once,
then on each interval runs a *provider* command that prints the field values to
stream to the panel. Where those values come from (local sensors, a monitoring
hub, an API, …) lives entirely in your provider — never in this repo.
"""
import os
from dataclasses import dataclass
from typing import Optional

try:
    import tomllib
except ModuleNotFoundError:  # pragma: no cover  (Python < 3.11)
    tomllib = None

DEFAULT_PATHS = [
    os.path.expanduser("~/.config/csm-panel/config.toml"),
    "/etc/csm-panel/config.toml",
]


@dataclass
class Config:
    port: str = "/dev/ttyACM0"
    # Theme blob to flash once at startup (a compiled .ui, or a vendor capture).
    # None: don't flash — drive whatever theme is already on the panel.
    theme: Optional[str] = None
    interval: float = 10.0        # seconds between 0x66 pushes (keep < 30 to stay awake)
    brightness: int = 100         # 0..100
    # Command whose stdout is JSON {field_id: value} for the fields to update
    # (valid field ids are 2..21). Run once per interval. None: push nothing.
    provider: Optional[str] = None


def load(path: Optional[str] = None) -> Config:
    paths = [path] if path else DEFAULT_PATHS
    data = {}
    for p in paths:
        if p and os.path.exists(p):
            if tomllib is None:
                raise RuntimeError("Python 3.11+ (tomllib) required to read config")
            with open(p, "rb") as f:
                data = tomllib.load(f)
            break
    panel = data.get("panel", {})
    prov = data.get("provider", {})
    return Config(
        port=panel.get("port", "/dev/ttyACM0"),
        theme=panel.get("theme"),
        interval=float(panel.get("interval", 10.0)),
        brightness=int(panel.get("brightness", 100)),
        provider=prov.get("command"),
    )
