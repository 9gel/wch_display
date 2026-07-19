"""csm-panel command-line interface."""
import argparse
import json
import sys

from . import config as cfgmod


def main(argv=None):
    ap = argparse.ArgumentParser(prog="csm-panel",
                                 description="Drive the CSM050H800480 USB panel.")
    ap.add_argument("-c", "--config", help="path to config.toml")
    sub = ap.add_subparsers(dest="cmd")

    sub.add_parser("run", help="run the dashboard service (default)")
    sub.add_parser("once", help="render and flash a single frame")
    sub.add_parser("model", help="query the panel model string")
    p_prev = sub.add_parser("preview", help="render a frame to a PNG (no hardware)")
    p_prev.add_argument("output", nargs="?", default="preview.png")
    p_img = sub.add_parser("image", help="flash an arbitrary image file")
    p_img.add_argument("path")
    sub.add_parser("beszel-probe", help="dump what the Beszel hub returns")
    p_fw = sub.add_parser("flash-firmware",
                          help="recover a bricked panel by flashing a boot-mode firmware image")
    p_fw.add_argument("path", help="the update_*.bin firmware file")
    p_fw.add_argument("--flash", action="store_true",
                      help="actually write to the panel (default: dry-run)")
    p_fw.add_argument("--no-reboot", action="store_true",
                      help="do not send 'reset' after a successful transfer")

    args = ap.parse_args(argv)
    cfg = cfgmod.load(args.config)
    cmd = args.cmd or "run"

    if cmd == "model":
        from .panel import Panel
        p = Panel(port=cfg.port)
        print(p.model())
        p.close()
        return 0

    if cmd == "beszel-probe":
        return _beszel_probe(cfg)

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

    if cmd == "preview":
        from .service import Service
        svc = Service(cfg)
        # warm up a little history for nicer sparklines
        import time
        for _ in range(8):
            svc.collect()
            time.sleep(0.2)
        from . import dashboard
        img = dashboard.render(svc.collect(), config={"columns": cfg.columns})
        img.save(args.output)
        print(f"wrote {args.output} ({img.size[0]}x{img.size[1]})")
        return 0

    if cmd == "image":
        from PIL import Image
        from . import render
        from .panel import Panel
        img = render.to_panel(Image.open(args.path).convert("RGB"), rotate=cfg.rotate)
        p = Panel(port=cfg.port)
        print("ack:", p.flash(img, quality=cfg.quality))
        p.close()
        return 0

    from .service import Service
    svc = Service(cfg)
    if cmd == "once":
        print("ack:", svc.flash_once())
        return 0
    try:
        svc.run()
    except KeyboardInterrupt:
        print("\nstopped")
    return 0


def _beszel_probe(cfg):
    from .beszel import BeszelClient
    b = cfg.resolved_beszel()
    if not b:
        print("no [beszel] section in config", file=sys.stderr)
        return 1
    cli = BeszelClient(url=b.get("url", "http://127.0.0.1:8090"),
                       email=b.get("email"), password=b.get("password"),
                       token=b.get("token"))
    cli.authenticate()
    systems = cli.systems()
    print(f"== {len(systems)} system(s) ==")
    for s in systems:
        print(f"\n# {s.get('name')}  status={s.get('status')}  id={s.get('id')}")
        print("info:", json.dumps(s.get("info", {}), indent=2)[:1200])
        try:
            st = cli.stats(s["id"], count=1)
            if st:
                print("latest stats:", json.dumps(st[-1].get("stats", {}), indent=2)[:1500])
        except Exception as e:
            print("stats error:", e)
    return 0


if __name__ == "__main__":
    sys.exit(main())
