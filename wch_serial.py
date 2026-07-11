"""Correct, non-hanging serial access for the 1a86:8040 USB CDC-Serial panel.

Key fixes vs. the original scripts:
  * Open the port the *normal* pyserial way. The old code did os.open(...,
    O_NONBLOCK) and hand-poked ser.fd, which fights pyserial's own open() and
    leaves DTR de-asserted -- several of these WCH panels only drain their USB
    bulk-OUT endpoint once DTR is high, so writes blocked forever.
  * Always set write_timeout, so a stalled panel raises instead of hanging.
"""
import serial

PORT = "/dev/ttyACM0"
BAUD = 115200


def open_port(port: str = PORT, write_timeout: float = 5.0, read_timeout: float = 2.0) -> serial.Serial:
    ser = serial.Serial(
        port=port,
        baudrate=BAUD,          # CDC-ACM ignores baud, but pyserial needs a value
        timeout=read_timeout,
        write_timeout=write_timeout,
    )
    ser.dtr = True              # assert DTR/RTS: makes the panel drain bulk-OUT
    ser.rts = True
    ser.reset_input_buffer()
    ser.reset_output_buffer()
    return ser
