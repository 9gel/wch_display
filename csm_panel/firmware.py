"""Boot-mode firmware (ROM) flasher for the old-gen CSM050H800480 (1a86:8040) panel.

Reverse-engineered from the vendor Qt app ``5 inch SmartMonitor.exe`` (frame
sender @0x426600, RX/ACK @0x426390, update launcher @0x4201d0, CRC16 @0x4015b0).
The boot-mode firmware push is byte-for-byte identical to the theme upload
(``Panel.send_theme``) except the command token is ``"update"`` for data frames
and a lone ``"end"`` header terminates it. Each ``update_*.bin`` is sent RAW —
it is already encrypted and the on-device bootloader decrypts it.

Per frame (data) = 64-byte header + 4096-byte chunk = 4160 bytes:
    [0:6]   name      "update"  (terminator frame uses "end")
    [6:8]   block     uint16 BE, 0,1,2,...
    [8:12]  length    uint32 BE = TRUE (unpadded) firmware size
    [12:14] crc       uint16 BE = CRC16-MODBUS over the TRUE firmware bytes
    [14:64] zero
    then the <=4096-byte chunk; the last chunk is zero-padded to 4096.
The device replies 'C' (0x43) on success, 0x15 (NAK) to request a resend.

RECOVERY: if the panel is bricked into "BOOT" / "MDT Error", put it in boot mode
and run ``csm-panel flash-firmware <update_*.bin> --flash``. The correct image
for this panel is named ``update_sdnand_800480_*.bin`` (NOT ``update_S021*`` /
``update_SM050*``); obtain it from the panel vendor/seller.
"""
import time

from .panel import Panel, crc16_modbus

CHUNK = 4096
MAX_FW = 0x3FF001  # vendor 4 MB size gate


def firmware_frames(blob: bytes):
    """Yield the exact OUT frames for a boot-mode firmware push."""
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
    return (f"{len(blob)} bytes (0x{len(blob):x}), crc16-modbus="
            f"{crc16_modbus(blob):04x}, {frames} frames ({frames - 1} data + 1 end)")


def flash_firmware(blob: bytes, port: str = "/dev/ttyACM0", reboot: bool = True,
                   log=print) -> bool:
    """Flash a raw firmware image to a panel already in bootloader mode.

    Returns True on an accepted transfer. Raises on I/O errors.
    """
    if len(blob) >= MAX_FW:
        raise ValueError(f"firmware exceeds 4MB gate (0x{MAX_FW:x})")
    p = Panel(port=port)
    p.ser.timeout = 5
    p.ser.write_timeout = 30
    try:
        model = p.model()
        log(f"panel: {model}")
        if "BOOT" not in model:
            raise RuntimeError(f"panel not in BOOT mode (model={model!r}); "
                               "send 'boot' or hold the back button first")
        p.ser.reset_input_buffer()
        frames = list(firmware_frames(blob))
        for i, fr in enumerate(frames):
            p._write(fr)
            p.ser.flush()
            r = p.ser.read(1)
            if r == b"\x15":
                raise RuntimeError(f"NAK at frame {i} — device rejected the image")
            if r == b"C":
                log(f"ACK 'C' after frame {i}/{len(frames) - 1}")
        if reboot:
            time.sleep(0.5)
            p.reset_mcu()
            log("sent 'reset' — panel should reboot into the app")
        return True
    finally:
        p.close()
