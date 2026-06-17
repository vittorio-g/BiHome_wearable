"""
Integration test for the TTL .acq<->XDF path (validation/acq_sync.py), using
synthetic two-clock data so it runs without a real .acq or bioread file I/O.

It checks the full numeric path: TTL pulse detection -> clock fit -> resample &
map BIOPAC channels onto the LSL clock -> inject as a stream -> the existing
analyze_xdf alignment + agreement recover a wearable built from the same truth.
    .venv\\Scripts\\python.exe validation\\tests\\test_acq_sync.py
"""

import os
import sys

import numpy as np

_VDIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _VDIR)
import sync          # noqa: E402
import acq_sync      # noqa: E402
import analyze_xdf   # noqa: E402
import agreement     # noqa: E402


def _truth(t):
    return 33.0 + 0.5 * np.sin(2 * np.pi * t / 50.0) + 0.1 * np.sin(2 * np.pi * t / 7.0)


def _synthetic_acq(slope, intercept, fs_bio=100.0, dur=300.0, pulses=(20., 150., 280.)):
    t_acq = np.arange(0.0, dur, 1.0 / fs_bio)
    bio_temp = _truth(slope * t_acq + intercept)        # BIOPAC sees truth on LSL time
    ttl = np.zeros_like(t_acq)
    for p in pulses:
        ttl[(t_acq >= p) & (t_acq < p + 0.05)] = 5.0    # 50 ms pulses
    return {"TEMP": {"fs": fs_bio, "samples": bio_temp},
            "TTL": {"fs": fs_bio, "samples": ttl}}, np.array(pulses)


def test_full_ttl_alignment_recovers_agreement():
    slope, intercept = 1.0003, 34.5
    chans, pulse_acq = _synthetic_acq(slope, intercept)

    detected = sync.detect_pulses(chans["TTL"]["samples"], chans["TTL"]["fs"])
    assert detected.size == pulse_acq.size

    marker_times = slope * pulse_acq + intercept        # XDF/LSL clock
    streams = {}
    diag = acq_sync._align_and_inject(streams, marker_times, chans, detected,
                                      "TTL", "BIOPAC")
    assert "BIOPAC" in streams
    assert abs(diag["slope"] - slope) < 1e-4
    assert diag["max_resid_s"] < 0.05
    assert diag["channels"] == ["TEMP"]

    # Wearable stream on the LSL clock, same truth, lower rate
    t_lsl = np.arange(intercept + 5, intercept + 300.0 * slope - 5, 1 / 7.5)
    wstream = acq_sync.make_pseudo_stream("P01_EmoTemp", t_lsl,
                                          _truth(t_lsl).reshape(-1, 1), ["TEMP"], 7.5)
    streams["P01_EmoTemp"] = wstream

    ri = analyze_xdf._resolve_channel(streams["BIOPAC"], "TEMP")
    grid, w, r, fs, lag = analyze_xdf._align_pair(streams["P01_EmoTemp"], 0,
                                                  streams["BIOPAC"], ri, 1.0)
    m = agreement.compute_all(w, r)
    assert m["pearson_r"] > 0.999
    assert abs(m["bias"]) < 0.05


def test_build_stream_multirate():
    chans = {"ECG": {"fs": 1000.0, "samples": np.random.default_rng(0).normal(0, 1, 30000)},
             "EDA": {"fs": 100.0, "samples": np.random.default_rng(1).normal(5, 0.1, 3000)}}
    st = acq_sync.build_biopac_stream(chans, 1.0, 0.0, "BIOPAC")
    assert st["time_series"].shape[1] == 2
    assert int(st["info"]["channel_count"][0]) == 2
    # common grid at the higher rate, over the shared 30 s duration
    assert st["time_series"].shape[0] > 29000


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
