"""
Unit tests for validation/sync.py — TTL clock alignment, with synthetic
two-clock data where the offset and drift are known.
    .venv\\Scripts\\python.exe validation\\tests\\test_sync.py
"""

import os
import sys

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import sync  # noqa: E402


def test_fit_recovers_offset_and_drift():
    # BIOPAC clock event times
    t_acq = np.array([5.0, 65.0, 125.0, 185.0, 245.0])
    slope_true, intercept_true = 1.0004, 12.34   # LSL = 1.0004*acq + 12.34
    t_lsl = slope_true * t_acq + intercept_true
    slope, intercept, max_resid, n = sync.fit_clock_map(t_lsl, t_acq)
    assert n == 5
    assert abs(slope - slope_true) < 1e-6
    assert abs(intercept - intercept_true) < 1e-6
    assert max_resid < 1e-6


def test_single_pulse_offset_only():
    slope, intercept, max_resid, n = sync.fit_clock_map(np.array([112.34]),
                                                        np.array([100.0]))
    assert n == 1
    assert slope == 1.0
    assert abs(intercept - 12.34) < 1e-9


def test_map_times_round_trip():
    t_acq = np.linspace(0, 300, 50)
    t_lsl = 1.0002 * t_acq + 7.5
    slope, intercept, _, _ = sync.fit_clock_map(t_lsl, t_acq)
    mapped = sync.map_times(t_acq, slope, intercept)
    assert np.max(np.abs(mapped - t_lsl)) < 1e-6


def test_detect_pulses_square_wave():
    fs = 1000.0
    dur = 20.0
    sig = np.zeros(int(dur * fs))
    pulse_times = [2.0, 8.0, 14.0, 19.0]
    width = int(0.05 * fs)  # 50 ms pulses
    for pt in pulse_times:
        i = int(pt * fs)
        sig[i:i + width] = 5.0
    found = sync.detect_pulses(sig, fs, min_interval_s=0.5)
    assert found.size == len(pulse_times)
    assert np.max(np.abs(found - np.array(pulse_times))) < 2.0 / fs


def test_detect_pulses_flat_signal():
    assert sync.detect_pulses(np.ones(1000), 1000.0).size == 0


def test_match_pulses_count_mismatch_raises():
    try:
        sync.match_pulses(np.array([1.0, 2.0, 3.0]), np.array([1.0, 2.0]))
    except ValueError:
        return
    raise AssertionError("expected ValueError on count mismatch")


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
