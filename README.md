# csm-panel

A Linux driver and toolkit for the **CSM050H800480** 5″ 800×480 USB panel (USB id
`1a86:8040`, sold as a "PC sub-display / AIDA64 screen", board `HJ-5.0-LCD-V03`).
The whole protocol — the theme format, the live-update frames, the recovery
bootloader, and the vendor editor's `.ui` file encryption — was reverse-engineered
from USB captures and the vendor software. See [`PROTOCOL_NOTES.md`](PROTOCOL_NOTES.md).

It gives you three things, all from Linux with no Windows:

1. **Theme toolkit** — decode/encode the vendor editor's `.ui` files and compile
   them into the binary the panel flashes.
2. **A streaming service** — flash a theme once, then push live values to its
   widgets with the panel's no-reset `0x66` update frame. The service is
   data-source agnostic: *you* supply a tiny "provider" that prints the numbers.
3. **Recovery** — reflash the panel's firmware over serial if a bad theme ever
   bricks it into the bootloader ("MDT Error").

## Install

The panel enumerates as `/dev/ttyACM0`; your user needs the `dialout` group
(`sudo usermod -aG dialout $USER`, then re-login).

**With [direnv](https://direnv.net/):** `direnv allow` and the dev environment
(via `.envrc` → the flake / `uv`) is ready — just `csm-panel …`.

**With [uv](https://docs.astral.sh/uv/):**

```bash
uv sync
uv run csm-panel model      # sanity check -> "CSM050H800480_14 NAND V0.2.8"
```

Only dependency is `pyserial`.

## Quick start

```bash
csm-panel model                       # query the panel
csm-panel push '{"2": 45, "3": 46}'   # push one 0x66 update (field -> value)
csm-panel flash theme.bin             # flash a compiled theme blob
csm-panel run -c config.toml          # flash a theme once, then stream (Ctrl-C to stop)
```

## The provider model

The service knows nothing about *where* your numbers come from. Each interval it
runs your `[provider] command`, reads a JSON object `{field_id: value}` from its
stdout, and streams those values to the panel. Field ids are `2..21` (field 1 is
not addressable by the `0x66` frame).

```toml
# config.toml
[panel]
port = "/dev/ttyACM0"
theme = "theme.bin"      # flashed once at startup (omit to keep the current theme)
interval = 10.0          # seconds; keep < 30 so the panel stays awake

[provider]
command = "python examples/provider_stub.py"
```

`examples/provider_stub.py` emits demo values so you can see it work with zero
setup. A real provider is just any script that prints the JSON — pull from
sensors, a monitoring API, a database, whatever you like. See
[`config.example.toml`](config.example.toml).

## Making a theme

A theme is a layout of widgets (numbers, progress bars, static text, images,
clock), each optionally bound to a `fastSensor` field id that `0x66` updates. Design
it in the vendor's Windows "Theme Editor" (it saves a `.ui`), then compile it to a
flashable blob on Linux:

```bash
csm-panel ui-decode Mytheme.ui > mytheme.xml        # inspect / hand-edit the XML
csm-panel ui-compile Mytheme.ui base.bin -o theme.bin
csm-panel flash theme.bin
```

`ui-compile` reuses a **base blob** (a known-good theme of the same
resolution/orientation, e.g. one you captured flashing a stock theme) for the
pre-rendered glyph/text resources, and rebuilds the descriptor + widget table
(types, positions, colors, field bindings) from your `.ui` — byte-exact. The full
blob format and the portrait↔panel coordinate transform are documented in
[`PROTOCOL_NOTES.md`](PROTOCOL_NOTES.md); the codec/compiler live in
`csm_panel/theme/`.

## Recovery

Flashing a malformed theme can corrupt the panel's data table so the bootloader
refuses to start the app ("MDT Error"). It's recoverable from Linux with the
correct firmware image (`update_*.bin`, from the panel vendor):

```bash
csm-panel flash-firmware update_*.bin           # dry-run (safe)
csm-panel flash-firmware update_*.bin --flash    # actually reflash (panel in boot mode)
```

## Run as a service (NixOS flake)

This repo is a flake exposing `packages.default`, `overlays.default` and
`nixosModules.default`:

```nix
{
  inputs.csm-panel.url = "github:you/wch_display";
  # in your nixosSystem modules:
  imports = [ inputs.csm-panel.nixosModules.default ];

  services.csm-panel = {
    enable = true;
    settings = {
      panel = { port = "/dev/ttyACM0"; theme = "/var/lib/csm-panel/theme.bin"; interval = 10.0; };
      provider.command = "/etc/csm-panel/provider";
    };
    # user/group to run unprivileged (needs the dialout group for the port).
    # environmentFile = "-/run/csm-panel/env";   # secrets for your provider
  };
}
```

The module renders `config.toml` from `settings` and runs `csm-panel run` as a
systemd service. Your theme blob and provider are yours to supply; keep any
secrets out of `settings` (the Nix store is world-readable) and pass them to your
provider via `environmentFile`.

## Code layout

| path | purpose |
|------|---------|
| `csm_panel/panel.py` | serial driver: `model()`, `send_theme()`, `push_data()` (0x66), `boot`/`reset` |
| `csm_panel/theme/ui_codec.py` | decode/encode the vendor's RC4 `.ui` files |
| `csm_panel/theme/compiler.py` | compile a `.ui` → flashable theme blob |
| `csm_panel/firmware.py` | boot-mode firmware (re)flasher for recovery |
| `csm_panel/service.py` | flash-once + `0x66` streaming loop (runs your provider) |
| `examples/provider_stub.py` | reference provider |
| `tools/pcap_usb.py` | USBPcap decoder used to reverse the protocol |

## Troubleshooting

- **`could not open /dev/ttyACM0`** — the device is claimed elsewhere (commonly a
  VM's USB passthrough). Detach it there so Linux's `cdc_acm` binds it.
- **Permission denied on the port** — add yourself to `dialout` and re-login.
- **The panel disappears/re-enumerates** — it sleeps after ~30s with no `0x66`;
  keep `interval < 30`. The service re-opens the port automatically.
