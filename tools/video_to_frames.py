#!/usr/bin/env python3
"""Slice a GIF/MP4 into panel-safe JPEG frames for an animated theme.

The CSM050H800480 panel has a minimal on-board JPEG decoder that only accepts the
standard libjpeg/JFIF layout: **baseline** (no progressive), a JFIF APP0 header,
the standard separate DC/AC luma/chroma Huffman tables, and **4:2:0 or 4:4:4**
chroma sampling. ffmpeg's built-in mjpeg encoder violates all of this (it omits
JFIF, packs its own single Huffman/quant table, and writes a non-standard 1x2
sampling geometry for 4:4:4) — its `.jpg` output renders as random noise on the
panel even though it looks fine on a PC.

So this tool splits the job: **ffmpeg** decodes/selects frames to lossless PNG
(pixels only), then **ImageMagick** (libjpeg) writes the actual panel-safe JPEGs.

    python tools/video_to_frames.py IN.gif --skip 4 --out theme/images/STARRY --prefix starry
    # -> theme/images/STARRY/starry_0.jpg, starry_1.jpg, ...

Assumes IN is already cropped/scaled to the target size (e.g. 480x800 portrait);
this tool does not resize. Requires `ffmpeg` and `magick` on PATH (see shell.nix).
"""
import argparse
import os
import shutil
import subprocess
import sys
import tempfile


def run(cmd):
    p = subprocess.run(cmd, capture_output=True, text=True)
    if p.returncode != 0:
        sys.stderr.write(" ".join(cmd) + "\n" + p.stderr)
        sys.exit(f"command failed (exit {p.returncode})")
    return p


def main():
    ap = argparse.ArgumentParser(description="GIF/MP4 -> panel-safe JPEG frames.")
    ap.add_argument("input", help="source .gif or .mp4 (already cropped/scaled)")
    ap.add_argument("--skip", type=int, default=1, metavar="N",
                    help="keep every Nth frame (1 = every frame; 4 = frames 0,4,8,...)")
    ap.add_argument("--out", required=True, metavar="DIR", help="output directory")
    ap.add_argument("--prefix", required=True, help="output filename prefix (-> PREFIX_%%d.jpg)")
    ap.add_argument("--quality", type=int, default=92, help="JPEG quality 1-100 (default 92)")
    ap.add_argument("--sampling", default="4:2:0", choices=["4:2:0", "4:4:4"],
                    help="chroma subsampling (default 4:2:0; both are panel-safe)")
    ap.add_argument("--max-mb", type=float, default=3.5, metavar="MB",
                    help="total-frames size budget in MiB (default 3.5). If the frames "
                         "exceed it, quality is auto-lowered to fit. The whole theme blob "
                         "must stay under 4 MiB or the panel rejects it. 0 disables.")
    args = ap.parse_args()

    if args.skip < 1:
        sys.exit("--skip must be >= 1")
    for tool in ("ffmpeg", "magick"):
        if not shutil.which(tool):
            sys.exit(f"{tool} not found on PATH (add it via shell.nix / nix-shell)")
    if not os.path.isfile(args.input):
        sys.exit(f"input not found: {args.input}")
    os.makedirs(args.out, exist_ok=True)

    tmp = tempfile.mkdtemp(prefix="v2f_")
    try:
        # 1) ffmpeg: select every Nth frame, write lossless PNG (sequential 0..M-1).
        #    not(mod(n,N)) keeps frames whose index is a multiple of N; passthrough
        #    stops the frame-rate filter from duplicating/dropping.
        run(["ffmpeg", "-nostdin", "-loglevel", "error", "-y", "-i", args.input,
             "-vf", f"select=not(mod(n\\,{args.skip}))", "-fps_mode", "passthrough",
             "-start_number", "0", os.path.join(tmp, "f_%d.png")])
        pngs = sorted((f for f in os.listdir(tmp) if f.endswith(".png")),
                      key=lambda f: int(f[2:-4]))
        if not pngs:
            sys.exit("ffmpeg produced no frames")

        # 2) ImageMagick (libjpeg): encode each PNG to a panel-safe baseline JPEG.
        def encode_all(quality):
            total = 0
            for i, png in enumerate(pngs):
                out = os.path.join(args.out, f"{args.prefix}_{i}.jpg")
                run(["magick", os.path.join(tmp, png),
                     "-interlace", "none",                 # baseline, never progressive
                     "-sampling-factor", args.sampling,    # 4:2:0 / 4:4:4 (not ffmpeg's 1x2)
                     "-quality", str(quality), out])
                total += os.path.getsize(out)
            return total

        q = args.quality
        total = encode_all(q)
        budget = int(args.max_mb * 1024 * 1024) if args.max_mb else 0
        if budget and total > budget:
            # binary-search the highest quality (down to a floor) whose total fits
            lo, hi, best = 20, args.quality - 1, None
            while lo <= hi:
                mid = (lo + hi) // 2
                if encode_all(mid) <= budget:
                    best = mid; lo = mid + 1
                else:
                    hi = mid - 1
            q = best if best is not None else 20
            total = encode_all(q)             # leave the chosen quality on disk
            if best is None:
                sys.stderr.write(f"warning: even q20 is {total/1024/1024:.2f} MiB "
                                 f"(> {args.max_mb} MiB budget); reduce --skip or frame size\n")

        mb = total / 1024 / 1024
        note = f"  [auto-lowered from q{args.quality} to fit {args.max_mb} MiB]" if q != args.quality else ""
        print(f"wrote {len(pngs)} frames: {args.prefix}_0.jpg .. {args.prefix}_{len(pngs)-1}.jpg "
              f"in {args.out} ({args.sampling}, q{q}, total {mb:.2f} MiB){note}")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    main()
