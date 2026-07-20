#!/usr/bin/env python3
"""Extract named OUT frames from a USBPcap capture into a raw replay blob.

The panel's firmware CRC is computed over the *decrypted* image and can't be
recomputed host-side, so the only reliable Linux firmware recovery is to replay
the vendor's own boot-mode Update frames captured over USB. Use this to pull the
``update``+``end`` frames out of such a capture:

    python tools/extract_frames.py capture.pcapng update end > recovery.frames
    csm-panel flash-firmware --replay recovery.frames    # panel in boot mode

(Also works for ``theme``+``end`` to replay a captured theme flash.)
"""
import sys

sys.path.insert(0, __file__.rsplit("/", 2)[0] + "/tools")
from pcap_usb import data_xfers  # noqa: E402


def extract(pcap_path, names):
    want = tuple(n.encode() if isinstance(n, str) else n for n in names)
    out = bytearray()
    for x in data_xfers(pcap_path, out=True):
        d = x.data
        head = bytes(d[:6]).split(b"\x00")[0]
        if head in want:
            out += d
    return bytes(out)


if __name__ == "__main__":
    if len(sys.argv) < 3:
        print("usage: extract_frames.py capture.pcapng NAME [NAME ...]  (e.g. update end)",
              file=sys.stderr)
        sys.exit(1)
    blob = extract(sys.argv[1], sys.argv[2:])
    sys.stderr.write(f"extracted {len(blob)} bytes ({len(blob)//4160}+ frames)\n")
    sys.stdout.buffer.write(blob)
