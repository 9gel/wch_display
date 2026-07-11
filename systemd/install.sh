#!/usr/bin/env bash
# Install csm-panel as a systemd service.
set -euo pipefail
REPO="$(cd "$(dirname "$0")/.." && pwd)"

echo "==> Creating the project venv with uv"
cd "$REPO"
uv sync

echo "==> Installing default config to ~/.config/csm-panel/config.toml (if absent)"
mkdir -p "$HOME/.config/csm-panel"
[ -f "$HOME/.config/csm-panel/config.toml" ] || \
    cp "$REPO/config.example.toml" "$HOME/.config/csm-panel/config.toml"

echo "==> Installing systemd unit (system-wide)"
sudo cp "$REPO/systemd/csm-panel.service" /etc/systemd/system/csm-panel.service
sudo sed -i "s#/home/nigel/code/wch_display#$REPO#g; s/^User=nigel/User=$USER/" \
    /etc/systemd/system/csm-panel.service
sudo systemctl daemon-reload
sudo systemctl enable --now csm-panel.service
echo "==> Done. Check status with:  systemctl status csm-panel"
