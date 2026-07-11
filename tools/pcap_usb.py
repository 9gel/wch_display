"""Minimal pcapng + USBPcap parser -> per-transfer USB payloads.

Extracts host->device (OUT) and device->host (IN) bulk/interrupt data from a
USBPcap capture, with timestamps, so we can reverse-engineer the wire protocol.
No external deps.
"""
import struct
import sys
from dataclasses import dataclass


@dataclass
class Xfer:
    frame: int
    t: float          # relative seconds
    endpoint: int     # full address incl direction bit
    transfer: int     # 0=iso 1=int 2=ctrl 3=bulk
    out: bool         # host->device
    data: bytes


_TRANSFER = {0: "iso", 1: "int", 2: "ctrl", 3: "bulk"}


def _parse_usbpcap(pkt: bytes):
    """Return (endpoint, transfer, out, data) or None."""
    if len(pkt) < 27:
        return None
    hdr_len = struct.unpack_from("<H", pkt, 0)[0]
    # info(offset19) bit0: 0=FDO->PDO(submit/OUT), 1=PDO->FDO(complete/IN result)
    info = pkt[19]
    endpoint = pkt[21]
    transfer = pkt[22]
    data_len = struct.unpack_from("<I", pkt, 23)[0]
    data = pkt[hdr_len:hdr_len + data_len]
    out = (endpoint & 0x80) == 0
    return endpoint, transfer, out, data, info


def parse(path):
    with open(path, "rb") as f:
        blob = f.read()
    xfers = []
    off = 0
    ts_num = 1_000_000  # if_tsresol default 1e-6
    frame = 0
    while off + 8 <= len(blob):
        btype, blen = struct.unpack_from("<II", blob, off)
        if blen < 12 or off + blen > len(blob):
            break
        body = blob[off + 8: off + blen - 4]
        if btype == 0x00000006:  # Enhanced Packet Block
            frame += 1
            iface, th, tl, caplen, origlen = struct.unpack_from("<IIIII", body, 0)
            ts = (th << 32) | tl
            pkt = body[20:20 + caplen]
            r = _parse_usbpcap(pkt)
            if r:
                endpoint, transfer, out, data, info = r
                xfers.append(Xfer(frame, ts / ts_num, endpoint, transfer, out, data))
        elif btype == 0x00000003:  # Simple Packet Block
            frame += 1
            (origlen,) = struct.unpack_from("<I", body, 0)
            pkt = body[4:4 + origlen]
            r = _parse_usbpcap(pkt)
            if r:
                endpoint, transfer, out, data, info = r
                xfers.append(Xfer(frame, 0.0, endpoint, transfer, out, data))
        off += blen
    # normalize timestamps
    if xfers:
        t0 = min(x.t for x in xfers)
        for x in xfers:
            x.t -= t0
    return xfers


def data_xfers(path, out=None, min_len=1):
    """Bulk/interrupt transfers carrying data. out=True/False/None filter."""
    res = []
    for x in parse(path):
        if x.transfer not in (1, 3):
            continue
        if len(x.data) < min_len:
            continue
        if out is not None and x.out != out:
            continue
        res.append(x)
    return res


if __name__ == "__main__":
    path = sys.argv[1]
    only = None
    if len(sys.argv) > 2:
        only = sys.argv[2] == "out"
    for x in data_xfers(path, out=only):
        d = x.data
        arrow = "OUT" if x.out else "IN "
        head = d[:48].hex()
        print(f"[{x.frame:5d}] {x.t:8.3f}s {arrow} ep{x.endpoint:#04x} len={len(d):<6d} {head}")
