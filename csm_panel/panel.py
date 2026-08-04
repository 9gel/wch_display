"""Low-level driver for the CSM050H800480 USB panel (1a86:8040)."""
import time

import serial

PORT = "/dev/ttyACM0"
PANEL_W, PANEL_H = 800, 480

# theme-package constants (see PROTOCOL_NOTES.md)
THEME_MAGIC = 918          # u32 LE at blob[0]
CHUNK = 4096               # payload bytes per 'theme' USB frame
HEADER = 64                # command header size
THEME_MAX = 4 * 1024 * 1024  # panel rejects theme blobs larger than 4 MiB
FLASH_BATCH = 256          # panel acks 0x43 'C' after every 256 theme blocks (flow control)


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

    def _read_ack(self, timeout: float = 30.0) -> bytes:
        """Read one ack byte, waiting up to `timeout` s. The panel emits 0x43
        'C' after every FLASH_BATCH theme blocks and once more after 'end'; a
        256-block batch can take ~16 s to NAND-write, so the wait is generous."""
        old = self.ser.timeout
        self.ser.timeout = timeout
        try:
            return self.ser.read(1)
        finally:
            self.ser.timeout = old

    # --- commands ----------------------------------------------------------
    def model(self) -> str:
        """Query the panel model string."""
        self.ser.reset_input_buffer()
        self._write(self._cmd_header(b"model"))
        self.ser.flush()
        time.sleep(0.1)
        return self.ser.read(64).split(b"\x00")[0].decode("latin1", "replace").strip()

    def send_command(self, name: bytes) -> None:
        """Send a bare 64-byte control command (name at [0:], rest zero).

        Known bootloader/app commands: b"boot" (slotBootMode -> reboot into the
        bootloader), b"reset" (slotResetMcu -> reboot / jump to app).
        """
        self._write(self._cmd_header(name))
        self.ser.flush()

    def enter_boot(self):
        """Ask the running app to reboot into the bootloader."""
        self.send_command(b"boot")

    def reset_mcu(self):
        """Reboot the MCU (jump to app after a firmware update / re-validate)."""
        self.send_command(b"reset")

    def send_theme(self, blob: bytes, ack: bool = True,
                   batch: int = FLASH_BATCH, frame_delay: float = 0.0) -> bytes:
        """Upload a theme blob with the panel's flow-control handshake.

        Framing: each frame = 64-byte header (meta = content_length BE32 +
        CRC16-MODBUS(content) BE16) + a 4096-byte chunk; the blob is zero-padded
        to a whole number of chunks.

        FLOW CONTROL (matches the vendor Windows app; verified against a USB
        capture, theme_Cybercity.pcapng): the panel NAND-writes each 4 KB block
        as it arrives (~64 ms/block) and emits a 0x43 'C' ack after every
        `batch` (256) blocks, then once more after 'end'. The host must pace to
        that rate and wait for each periodic ack before continuing — blasting
        all frames back-to-back overruns the writer and flashes a corrupt /
        "Empty Theme" blob (intermittently: sometimes renders, sometimes no
        live updates, sometimes empty). We flush per frame (USB backpressure)
        and read each 256-block ack; if a panel still overruns, pass a small
        `frame_delay` (e.g. 0.06) for explicit per-frame pacing.
        """
        content_len = len(blob)
        if content_len > THEME_MAX:
            raise ValueError(
                f"theme blob is {content_len/1024/1024:.2f} MiB; the panel rejects "
                f"themes larger than {THEME_MAX//1024//1024} MiB (it flashes but shows "
                f"noise / fails). Shrink the background JPEG(s) — see PROTOCOL_NOTES.md.")
        crc = crc16_modbus(blob)
        meta = content_len.to_bytes(4, "big") + crc.to_bytes(2, "big")
        pad = (-len(blob)) % CHUNK
        padded = blob + b"\x00" * pad

        self.ser.reset_input_buffer()
        block = 0
        for off in range(0, len(padded), CHUNK):
            self._write(self._cmd_header(b"theme", block, meta) + padded[off:off + CHUNK])
            self.ser.flush()                      # push out + take USB backpressure
            block += 1
            if frame_delay:
                time.sleep(frame_delay)
            # flow control: the panel acks 'C' after every `batch` blocks — wait
            # for it before sending more, or the NAND writer overruns.
            if batch and block % batch == 0:
                a = self._read_ack()
                if a[:1] != b"C":
                    raise IOError(
                        f"theme flash: expected flow-control ack b'C' after block "
                        f"{block - 1}, got {a!r} — panel likely overran/dropped frames")
        self._write(self._cmd_header(b"end", 0, meta))
        self.ser.flush()
        if ack:
            return self._read_ack()
        return b""

    # --- live data push (0x66) : updates widgets WITHOUT resetting ----------
    @staticmethod
    def data_frame(values: dict, when=None, brightness=100, flags=0x06) -> bytes:
        """Build a 0x66 value-push frame.

        `values` maps field id (0x02..0x15) -> integer value (0..65535). The
        panel updates any widget bound to that field, with NO reset (unlike a
        theme flash). `brightness` 0..100; `flags` is the settings byte (byte
        [10], e.g. orientation). Layout: 66 | len | 01 | datetime(6) |
        [flags, brightness, 01, 00, 00] | 20 records <id><val BE16> | CRC16-BE.
        """
        import time
        lt = when or time.localtime()
        b = bytearray([0x66, 0x00, 0x00, 0x01,
                       lt.tm_year % 100, lt.tm_mon, lt.tm_mday,
                       lt.tm_hour, lt.tm_min, lt.tm_sec,
                       flags & 0xFF, brightness & 0xFF, 0x01, 0x00, 0x00])
        for rid in range(0x02, 0x16):
            v = int(values.get(rid, 0)) & 0xFFFF
            b += bytes([rid, (v >> 8) & 0xFF, v & 0xFF])
        b[2] = len(b) + 2
        b += crc16_modbus(bytes(b)).to_bytes(2, "big")
        return bytes(b)

    def push_data(self, values: dict, **kw):
        """Send one 0x66 value-push frame (non-resetting live update)."""
        self._write(self.data_frame(values, **kw))
        self.ser.flush()

    # --- raw replay (for validation / debugging) ---------------------------
    def replay_frames(self, frames, delay=0.0):
        """Send a list of raw OUT payloads verbatim (e.g. from a capture)."""
        for f in frames:
            self._write(f)
            self.ser.flush()
            if delay:
                time.sleep(delay)
