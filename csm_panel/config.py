"""Configuration loading. TOML-driven so the UI is easy to change without code.

See config.example.toml for the full annotated schema.
"""
import os
from dataclasses import dataclass, field
from typing import List, Optional

try:
    import tomllib
except ModuleNotFoundError:  # pragma: no cover
    tomllib = None

DEFAULT_PATHS = [
    os.path.expanduser("~/.config/csm-panel/config.toml"),
    "/etc/csm-panel/config.toml",
]


@dataclass
class HostConfig:
    name: str
    source: str = "local"            # local | beszel
    metrics: Optional[List[str]] = None   # None/["*"] = all; else ordered keys
    mounts: List[str] = field(default_factory=lambda: ["/"])
    system: Optional[str] = None     # beszel: system name (defaults to name)
    options: dict = field(default_factory=dict)


@dataclass
class Config:
    port: str = "/dev/ttyACM0"
    interval: float = 2.0
    quality: int = 88
    columns: int = 2
    rotate: str = "ccw"              # ccw | cw | 180 | none
    hosts: List[HostConfig] = field(default_factory=list)
    beszel: dict = field(default_factory=dict)   # url, email, password/token


def _default_config() -> Config:
    import socket
    return Config(hosts=[HostConfig(name=socket.gethostname(), source="local")])


def load(path: Optional[str] = None) -> Config:
    paths = [path] if path else DEFAULT_PATHS
    for pth in paths:
        if pth and os.path.exists(pth):
            return _parse(pth)
    return _default_config()


def _parse(path: str) -> Config:
    if tomllib is None:
        raise RuntimeError("Python 3.11+ (tomllib) required to read config files")
    with open(path, "rb") as f:
        raw = tomllib.load(f)
    panel = raw.get("panel", {})
    cfg = Config(
        port=panel.get("port", "/dev/ttyACM0"),
        interval=float(panel.get("interval", 2.0)),
        quality=int(panel.get("quality", 88)),
        columns=int(panel.get("columns", 2)),
        rotate=panel.get("rotate", "ccw"),
        beszel=raw.get("beszel", {}),
    )
    for h in raw.get("host", []):
        cfg.hosts.append(HostConfig(
            name=h["name"],
            source=h.get("source", "local"),
            metrics=h.get("metrics"),
            mounts=h.get("mounts", ["/"]),
            system=h.get("system"),
            options={k: v for k, v in h.items()
                     if k not in ("name", "source", "metrics", "mounts", "system")},
        ))
    if not cfg.hosts:
        cfg.hosts = _default_config().hosts
    return cfg
