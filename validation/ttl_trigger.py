"""
ttl_trigger.py — headless driver for the BIOPAC trigger path
    PC --USB-TTL (COMx, 115200 8N1)--> STP100D --> MP160 digital D8-D15 --> AcqKnowledge

Protocol (from the working technician setup: ttl_debug_tool_v4.py + the STP100D
hardware guide), identical for the BBTK USB-TTL module and the STP100D:
  - serial 115200, 8, N, 1, NO flow control; open the port ONCE per session
  - send ALWAYS 2-char UPPERCASE ASCII hex pairs (a single char hangs the module)
  - "RR" = init/reset (mandatory before any trigger)
  - "00".."FF" = set the 8 digital lines (LATCH: stay until the next command)
  - "00" = all lines low (reset after a pulse)
  - "VE" = firmware query (optional)

This is a GUI-free, reusable extraction of SerialWorker so BiHome can fire TTL
markers in sync with an LSL marker. The write path takes any serial-like object
(duck-typed: .write/.flush/.close), so the protocol is unit-tested without
hardware (tests/test_ttl_trigger.py).

Requires pyserial only when actually opening a real port.
"""

import time
from typing import Optional


class TtlTrigger:
    def __init__(self, port=None):
        # `port` is an OPEN serial-like object (or None until open()).
        self._port = port
        self._tx = 0

    # ── lifecycle ────────────────────────────────────────────────────────────
    @classmethod
    def open(cls, port_name: str, baud: int = 115200, timeout: float = 0.1) -> "TtlTrigger":
        """Open a real COM port with the STP100D settings (pyserial)."""
        import serial
        sp = serial.Serial(
            port=port_name, baudrate=baud,
            bytesize=serial.EIGHTBITS, parity=serial.PARITY_NONE,
            stopbits=serial.STOPBITS_ONE, timeout=timeout, write_timeout=1,
            rtscts=False, dsrdtr=False, xonxoff=False,   # NO flow control
        )
        return cls(sp)

    @property
    def is_open(self) -> bool:
        return self._port is not None and getattr(self._port, "is_open", True)

    @property
    def tx_count(self) -> int:
        return self._tx

    def close(self) -> None:
        """Reset lines to 0 then close (matches the technician's OnApplicationQuit)."""
        if self._port is not None:
            try:
                self.reset()
            except Exception:
                pass
            try:
                self._port.close()
            except Exception:
                pass
            self._port = None

    # ── protocol ──────────────────────────────────────────────────────────────
    def _write_pair(self, pair: str) -> int:
        if self._port is None:
            raise RuntimeError("TTL port not open")
        if len(pair) != 2:
            raise ValueError(f"command must be exactly 2 chars, got {pair!r}")
        data = pair.upper().encode("ascii")
        n = self._port.write(data)
        try:
            self._port.flush()
        except Exception:
            pass
        self._tx += int(n or 0)
        return int(n or 0)

    def init(self) -> int:
        '''Send "RR" — mandatory at the start of a session.'''
        return self._write_pair("RR")

    def reset(self) -> int:
        '''Send "00" — all digital lines low.'''
        return self._write_pair("00")

    def firmware_query(self) -> int:
        '''Send "VE" — firmware version request.'''
        return self._write_pair("VE")

    def send_value(self, value: int) -> int:
        """Set the 8 lines to `value` (0-255) as an uppercase hex pair (LATCH)."""
        if not 0 <= value <= 255:
            raise ValueError(f"value out of range 0-255: {value}")
        return self._write_pair(f"{value:02X}")

    def pulse(self, value: int, width_ms: float = 50.0) -> None:
        """Drive `value` HIGH for width_ms, then reset to 0 — one event marker."""
        self.send_value(value)
        time.sleep(max(0.0, width_ms) / 1000.0)
        self.reset()


def parse_value(s) -> int:
    """Accept an int (0-255) or a 1-2 char hex string ('1','01','FF') -> int."""
    if isinstance(s, int):
        v = s
    else:
        v = int(str(s), 16)
    if not 0 <= v <= 255:
        raise ValueError(f"value out of range 0-255: {v}")
    return v
