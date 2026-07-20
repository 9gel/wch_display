"""Boot-mode firmware (ROM) recovery for the CSM050H800480 (1a86:8040) panel.

If a bad theme corrupts the on-device data table, the bootloader shows "MDT Error"
and won't start the app. Recovery is a firmware reflash over the same serial link.
The frames look just like a theme upload (``Panel.send_theme``) but use the
``"update"`` command token + a lone ``"end"`` terminator; the ``update_*.bin`` is
sent RAW (it is encrypted; the bootloader decrypts it).

Per frame (data) = 64-byte header + 4096-byte chunk = 4160 bytes:
    [0:6]   name      "update"  (terminator frame uses "end")
    [6:8]   block     uint16 BE, 0,1,2,...
    [8:12]  length    uint32 BE = TRUE (unpadded) firmware size
    [12:14] crc       uint16 BE
    [14:64] zero      then the <=4096-byte chunk (last one zero-padded to 4096).
The device replies 'C' (0x43) once, after the final 'end' frame (NOT per frame).

## IMPORTANT: the firmware CRC cannot be computed host-side
A real vendor capture shows the per-frame CRC is computed over the **decrypted**
image (device-side key) — it matches NO standard CRC over the encrypted bytes we
transmit (e.g. capture CRC ``0x16a7`` vs ``crc16_modbus``=``0x3a41`` over the same
payload). So :func:`firmware_frames` (which uses CRC16-MODBUS, correct for *themes*)
produces frames a real device will NAK. The reliable recovery is to **replay the
vendor's own boot-mode Update frames captured over USB** — see
:func:`flash_raw_frames` and PROTOCOL_NOTES.md.
"""
import time

from .panel import Panel, crc16_modbus

CHUNK = 4096
MAX_FW = 0x3FF001  # vendor 4 MB size gate


def firmware_frames(blob: bytes):
    """Yield firmware push frames with a CRC16-MODBUS checksum.

    NOTE: valid framing/transport, but the CRC is wrong for firmware (the device
    wants the CRC over the *decrypted* image — see module docstring). Real devices
    NAK these. Kept for reference / structural tests. Use replay for recovery.
    """
    meta = len(blob).to_bytes(4, "big") + crc16_modbus(blob).to_bytes(2, "big")
    padded = blob + b"\x00" * ((-len(blob)) % CHUNK)
    for block, off in enumerate(range(0, len(padded), CHUNK)):
        h = bytearray(64)
        h[0:6] = b"update"
        h[6] = (block >> 8) & 0xFF
        h[7] = block & 0xFF
        h[8:14] = meta
        yield bytes(h) + padded[off:off + CHUNK]
    end = bytearray(64)          # terminator: lone "end" header, same meta
    end[0:3] = b"end"
    end[8:14] = meta
    yield bytes(end)


def describe(blob: bytes) -> str:
    frames = sum(1 for _ in firmware_frames(blob))
    return (f"{len(blob)} bytes (0x{len(blob):x}), {frames} frames "
            f"({frames - 1} data + 1 end)")


def flash_raw_frames(frames, port: str = "/dev/ttyACM0", reboot: bool = True,
                     log=print) -> bytes:
    """Write pre-built frames to a panel in bootloader mode, then read the ack.

    ``frames`` is a ``bytes`` blob (or an iterable of frames) — typically the
    ``update``+``end`` OUT frames extracted from a vendor USB capture of the
    boot-mode Update. This REPLAYS the vendor's exact bytes (with the device's
    real, uncomputable CRC), which is the only reliable Linux recovery.

    Transport (confirmed): write everything back-to-back, then read a single 'C'
    ack after the final frame. (Reading per-frame stalls for tens of seconds and
    the flaky boot-mode USB re-enumerates mid-transfer.) Returns the ack byte.
    """
    data = frames if isinstance(frames, (bytes, bytearray)) else b"".join(frames)
    p = Panel(port=port)
    p.ser.timeout = 8
    p.ser.write_timeout = 30
    try:
        p.ser.reset_input_buffer()
        p._write(data)
        p.ser.flush()
        ack = p.ser.read(1)
        log(f"flashed {len(data)} bytes, ack={ack!r}")
        if ack == b"C" and reboot:
            time.sleep(0.5)
            p.reset_mcu()
            log("sent 'reset' — panel should reboot into the app")
        return ack
    finally:
        p.close()


def flash_firmware(blob: bytes, port: str = "/dev/ttyACM0", reboot: bool = True,
                   log=print) -> bool:
    """Flash a firmware image by computing frames from the .bin.

    WARNING: the computed CRC is wrong for firmware (see module docstring), so a
    real device will NAK these frames. This is retained for reference and dry
    runs; use :func:`flash_raw_frames` with captured vendor frames for actual
    recovery. Returns True on a 'C' ack.
    """
    if len(blob) >= MAX_FW:
        raise ValueError(f"firmware exceeds 4MB gate (0x{MAX_FW:x})")
    ack = flash_raw_frames(firmware_frames(blob), port=port, reboot=reboot, log=log)
    if ack == b"\x15":
        log("NAK — device rejected the image (expected: the firmware CRC cannot "
            "be computed host-side; replay captured vendor frames instead)")
    return ack == b"C"
