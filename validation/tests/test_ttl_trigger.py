"""
Unit tests for validation/ttl_trigger.py — verify the STP100D/BBTK serial
protocol formatting against a fake serial port (no hardware).
    .venv\\Scripts\\python.exe validation\\tests\\test_ttl_trigger.py
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import ttl_trigger  # noqa: E402


class FakeSerial:
    """Captures every write so we can assert the exact bytes sent."""
    def __init__(self):
        self.writes = []
        self.is_open = True
        self.flushed = 0

    def write(self, data):
        self.writes.append(bytes(data))
        return len(data)

    def flush(self):
        self.flushed += 1

    def close(self):
        self.is_open = False


def test_init_sends_RR():
    fs = FakeSerial()
    t = ttl_trigger.TtlTrigger(fs)
    t.init()
    assert fs.writes == [b"RR"]


def test_send_value_hex_pairs():
    fs = FakeSerial()
    t = ttl_trigger.TtlTrigger(fs)
    t.send_value(1);   t.send_value(4);   t.send_value(255);  t.send_value(0)
    assert fs.writes == [b"01", b"04", b"FF", b"00"]


def test_send_value_out_of_range():
    t = ttl_trigger.TtlTrigger(FakeSerial())
    for bad in (-1, 256, 1000):
        try:
            t.send_value(bad)
        except ValueError:
            continue
        raise AssertionError(f"expected ValueError for {bad}")


def test_pulse_sends_value_then_zero():
    fs = FakeSerial()
    t = ttl_trigger.TtlTrigger(fs)
    t.pulse(8, width_ms=1)
    assert fs.writes == [b"08", b"00"]


def test_close_resets_and_closes():
    fs = FakeSerial()
    t = ttl_trigger.TtlTrigger(fs)
    t.close()
    assert fs.writes == [b"00"]   # reset before closing
    assert fs.is_open is False
    assert t.is_open is False


def test_tx_count_accumulates():
    fs = FakeSerial()
    t = ttl_trigger.TtlTrigger(fs)
    t.init(); t.send_value(255)
    assert t.tx_count == 4   # "RR" + "FF" = 2 + 2 bytes


def test_parse_value():
    assert ttl_trigger.parse_value(4) == 4
    assert ttl_trigger.parse_value("FF") == 255
    assert ttl_trigger.parse_value("1") == 1
    for bad in ("ZZ", 300):
        try:
            ttl_trigger.parse_value(bad)
        except ValueError:
            continue
        raise AssertionError(f"expected ValueError for {bad!r}")


def test_write_requires_open_port():
    t = ttl_trigger.TtlTrigger(None)
    try:
        t.init()
    except RuntimeError:
        return
    raise AssertionError("expected RuntimeError when port not open")


def _run_standalone():
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    failed = 0
    for fn in fns:
        try:
            fn()
            print(f"PASS  {fn.__name__}")
        except AssertionError as e:
            failed += 1
            print(f"FAIL  {fn.__name__}: {e}")
        except Exception as e:
            failed += 1
            print(f"ERROR {fn.__name__}: {type(e).__name__}: {e}")
    print(f"\n{len(fns) - failed}/{len(fns)} passed.")
    return failed


if __name__ == "__main__":
    sys.exit(1 if _run_standalone() else 0)
