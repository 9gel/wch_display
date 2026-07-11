"""Low-level driver for the CSM050H800480 USB panel (1a86:8040)."""
import time

import serial

PORT = "/dev/ttyACM0"
PANEL_W, PANEL_H = 800, 480

# theme-package constants (see PROTOCOL_NOTES.md)
THEME_MAGIC = 918          # u32 LE at blob[0]
CHUNK = 4096               # payload bytes per 'theme' USB frame
HEADER = 64                # command header size


def crc16_modbus(data: bytes) -> int:
    """CRC-16/MODBUS (poly 0x8005, init 0xFFFF, reflected). Panel theme checksum."""
    crc = 0xFFFF
    for b in data:
        crc ^= b
        for _ in range(8):
            crc = (crc >> 1) ^ 0xA001 if (crc & 1) else crc >> 1
    return crc


class Panel:
    def __init__(self, port=PORT, connect=True):
        self.ser = serial.Serial(
            port=port, baudrate=115200, bytesize=8,
            parity=serial.PARITY_NONE, stopbits=serial.STOPBITS_ONE,
            timeout=1.0, write_timeout=20,
        )
        self.ser.dtr = True
        self.ser.rts = True
        self.ser.reset_input_buffer()
        self.ser.reset_output_buffer()

    def close(self):
        try:
            self.ser.close()
        except Exception:
            pass

    def __enter__(self):
        return self

    def __exit__(self, *a):
        self.close()

    # --- framing -----------------------------------------------------------
    @staticmethod
    def _cmd_header(name: bytes, block: int = 0, meta: bytes = b"") -> bytes:
        """64-byte command header.

        Layout: name\\0 [0:6], block index BE16 [6:8], then `meta` at [8:]
        (content_length BE32 + CRC16 BE = 6 bytes for theme/end frames).
        """
        h = bytearray(HEADER)
        h[0:len(name)] = name
        h[6] = (block >> 8) & 0xFF
        h[7] = block & 0xFF
        h[8:8 + len(meta)] = meta
        return bytes(h)

    def _write(self, data: bytes):
        self.ser.write(data)

    # --- commands ----------------------------------------------------------
    def model(self) -> str:
        """Query the panel model string."""
        self.ser.reset_input_buffer()
        self._write(self._cmd_header(b"model"))
        self.ser.flush()
        time.sleep(0.1)
        return self.ser.read(64).split(b"\x00")[0].decode("latin1", "replace").strip()

    def send_theme(self, blob: bytes, ack: bool = True) -> bytes:
        """Upload a theme blob. Each frame header carries meta =
        content_length (BE24) + CRC16-MODBUS(content) (BE16); the blob is
        zero-padded to a whole number of 4096-byte chunks for transport.
        """
        content_len = len(blob)
        crc = crc16_modbus(blob)
        meta = content_len.to_bytes(4, "big") + crc.to_bytes(2, "big")
        pad = (-len(blob)) % CHUNK
        padded = blob + b"\x00" * pad

        self.ser.reset_input_buffer()
        block = 0
        for off in range(0, len(padded), CHUNK):
            self._write(self._cmd_header(b"theme", block, meta) + padded[off:off + CHUNK])
            block += 1
        self._write(self._cmd_header(b"end", 0, meta))
        self.ser.flush()
        if ack:
            return self.ser.read(1)
        return b""

    # --- raw replay (for validation / debugging) ---------------------------
    def replay_frames(self, frames, delay=0.0):
        """Send a list of raw OUT payloads verbatim (e.g. from a capture)."""
        for f in frames:
            self._write(f)
            self.ser.flush()
            if delay:
                time.sleep(delay)
