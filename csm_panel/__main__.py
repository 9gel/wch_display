"""csm-panel command-line interface."""
import argparse
import json
import sys

from . import config as cfgmod


def main(argv=None):
    ap = argparse.ArgumentParser(prog="csm-panel",
                                 description="Drive the CSM050H800480 (1a86:8040) USB panel.")
    ap.add_argument("-c", "--config", help="path to config.toml")
    sub = ap.add_subparsers(dest="cmd")

    sub.add_parser("run", help="run the streaming service (flash theme once, push 0x66)")
    sub.add_parser("model", help="query the panel model string")

    p_push = sub.add_parser("push", help="push one 0x66 update: JSON {field: value} on argv/stdin")
    p_push.add_argument("json", nargs="?", help='e.g. \'{"2": 45, "3": 46}\' (default: read stdin)')

    p_flash = sub.add_parser("flash", help="flash a theme blob (.bin) to the panel")
    p_flash.add_argument("path")

    p_fw = sub.add_parser("flash-firmware",
                          help="recover a bricked panel by flashing a boot-mode firmware image")
    p_fw.add_argument("path", help="the update_*.bin firmware file")
    p_fw.add_argument("--flash", action="store_true",
                      help="actually write to the panel (default: dry-run)")
    p_fw.add_argument("--no-reboot", action="store_true",
                      help="do not send 'reset' after a successful transfer")

    p_dec = sub.add_parser("ui-decode", help="decrypt a vendor .ui theme file to XML (stdout)")
    p_dec.add_argument("path")

    p_com = sub.add_parser("ui-compile", help="compile a .ui theme into a flashable blob")
    p_com.add_argument("ui", help="the .ui theme file (encrypted)")
    p_com.add_argument("base", help="a known-good base blob (same resolution) for glyph resources")
    p_com.add_argument("-o", "--output", default="theme.bin")
    p_com.add_argument("--images", help="theme images dir (for a new background JPEG)")

    args = ap.parse_args(argv)
    cfg = cfgmod.load(args.config)
    cmd = args.cmd or "run"

    if cmd == "model":
        from .panel import Panel
        p = Panel(port=cfg.port); print(p.model()); p.close()
        return 0

    if cmd == "push":
        from .panel import Panel
        raw = args.json if args.json else sys.stdin.read()
        values = {int(k): int(round(float(v))) for k, v in json.loads(raw).items()}
        p = Panel(port=cfg.port); p.push_data(values, brightness=cfg.brightness); p.close()
        print("pushed", values)
        return 0

    if cmd == "flash":
        from .panel import Panel
        blob = open(args.path, "rb").read()
        p = Panel(port=cfg.port)
        print("ack:", p.send_theme(blob)); p.close()
        return 0

    if cmd == "flash-firmware":
        from . import firmware
        blob = open(args.path, "rb").read()
        print(f"firmware {args.path}: {firmware.describe(blob)}")
        if not args.flash:
            print("DRY RUN — re-run with --flash to write to the panel.")
            return 0
        ok = firmware.flash_firmware(blob, port=cfg.port, reboot=not args.no_reboot)
        print("done" if ok else "failed")
        return 0 if ok else 1

    if cmd == "ui-decode":
        from .theme import decode
        sys.stdout.buffer.write(decode(open(args.path, "rb").read()))
        return 0

    if cmd == "ui-compile":
        from .theme import decode, compile_ui_to_blob
        ui = decode(open(args.ui, "rb").read())
        base = open(args.base, "rb").read()
        blob = compile_ui_to_blob(ui, args.images, base)
        with open(args.output, "wb") as f:
            f.write(blob)
        print(f"wrote {args.output} ({len(blob)} bytes)")
        return 0

    from .service import Service
    try:
        Service(cfg).run()
    except KeyboardInterrupt:
        print("\nstopped")
    return 0


if __name__ == "__main__":
    sys.exit(main())
