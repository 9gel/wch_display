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
    interval: float = 15.0           # seconds between samples (history/refresh checks)
    heartbeat: float = 600.0         # force a re-flash at least this often
    quality: int = 88
    columns: int = 2
    rotate: str = "ccw"              # ccw | cw | 180 | none
    hosts: List[HostConfig] = field(default_factory=list)
    beszel: dict = field(default_factory=dict)   # url, email, password/token

    def resolved_beszel(self) -> dict:
        """Beszel settings with secrets resolved from env/command.

        Keeps secrets out of the config file (and the nix store): a `password`
        or `token` may instead come from an environment variable
        (CSM_PANEL_BESZEL_PASSWORD / CSM_PANEL_BESZEL_TOKEN) or a shell command
        (`password_command` / `token_command`, e.g. a secret-manager call).
        """
        b = dict(self.beszel)
        for field_name, env in (("password", "CSM_PANEL_BESZEL_PASSWORD"),
                                ("token", "CSM_PANEL_BESZEL_TOKEN"),
                                ("email", "CSM_PANEL_BESZEL_EMAIL")):
            val = os.environ.get(env)
            if not val and b.get(f"{field_name}_command"):
                import subprocess
                val = subprocess.run(b[f"{field_name}_command"], shell=True,
                                     capture_output=True, text=True).stdout.strip()
            if val:
                b[field_name] = val
        return b


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
        interval=float(panel.get("interval", 15.0)),
        heartbeat=float(panel.get("heartbeat", 600.0)),
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
