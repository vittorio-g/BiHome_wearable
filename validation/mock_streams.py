"""
mock_streams.py — synthetic LSL streams for developing/verifying the BiHome
validation pipeline WITHOUT real hardware (no Polar, no EmotiBit, no BIOPAC).

It publishes a realistic set of paired streams:

  Wearable side (mimics BiHome's own outlets):
    P01_PolarECG   5 ch  [ECG(uV), ACC_X, ACC_Y, ACC_Z, beat]   ~130 Hz
    P01_EmoEDA     1 ch  [EDA(uS)]                               ~15  Hz
    P01_EmoPPG     1 ch  [PPG(a.u.)]                             ~25  Hz
    P01_EmoTemp    1 ch  [TEMP(degC)]                            ~7.5 Hz

  Reference side (mimics a single multi-channel BIOPAC AcqKnowledge LSL outlet):
    BIOPAC         5 ch  [ECG(mV), EDA(uS), PPG(a.u.), RSP(a.u.), TEMP(degC)]  1000 Hz

Crucially the wearable and BIOPAC channels are derived from the SAME underlying
"truth" signal with KNOWN transforms (unit scale + small fixed lag + measurement
noise), so the agreement metrics computed downstream have a ground truth to be
checked against:

  - ECG : biopac_mV = ecg_truth ;  wearable_uV = ecg_truth * 1000  -> exact 1000x scale
  - EDA : same units (uS), small added noise
  - PPG : same a.u., small added noise
  - TEMP: same degC, small bias + noise
  - RSP : BIOPAC only (no wearable counterpart -> must NOT be paired)

Run standalone to just stream (Ctrl-C to stop):
    .venv\\Scripts\\python.exe validation\\mock_streams.py --duration 0   # 0 = forever

Or import run_outlets() / iter_until() from another script (see make_test_recording.py).
"""

import argparse
import math
import threading
import time
from dataclasses import dataclass, field
from typing import Callable, List, Optional

import numpy as np
from pylsl import StreamInfo, StreamOutlet, local_clock, cf_float32


# ── Synthetic signal "truth" functions (all take absolute time t in seconds) ──

def ecg_truth(t: float, hr_bpm: float = 70.0) -> float:
    """A simple synthetic ECG beat: P, QRS and T waves as Gaussians over the
    cardiac phase. Dimensionless, roughly in [-0.3, 1.0]. Deterministic in t."""
    period = 60.0 / hr_bpm
    phase = (t % period) / period  # 0..1 within a beat
    # (center, amplitude, width) for P, Q, R, S, T
    waves = [
        (0.16, 0.10, 0.025),   # P
        (0.33, -0.12, 0.012),  # Q
        (0.36, 1.00, 0.011),   # R
        (0.39, -0.25, 0.012),  # S
        (0.58, 0.22, 0.040),   # T
    ]
    v = 0.0
    for c, a, w in waves:
        v += a * math.exp(-((phase - c) ** 2) / (2 * w * w))
    return v


def eda_truth(t: float) -> float:
    """EDA in microsiemens: slow tonic drift (~4-8 uS) + occasional phasic SCRs."""
    tonic = 6.0 + 1.5 * math.sin(2 * math.pi * t / 60.0)
    # phasic: an SCR every ~20 s, exponential rise/decay
    scr = 0.0
    period = 20.0
    tau = (t % period)
    if tau < 6.0:
        scr = 1.2 * math.exp(-((tau - 1.5) ** 2) / (2 * 1.0 ** 2))
    return tonic + scr


def ppg_truth(t: float, hr_bpm: float = 70.0) -> float:
    """PPG (a.u.): pulsatile waveform at the heart rate + dicrotic notch."""
    f = hr_bpm / 60.0
    return (math.sin(2 * math.pi * f * t)
            + 0.35 * math.sin(4 * math.pi * f * t + 0.6))


def resp_truth(t: float, rr_bpm: float = 15.0) -> float:
    """Respiration (a.u.): slow sinusoid at the respiration rate."""
    f = rr_bpm / 60.0
    return math.sin(2 * math.pi * f * t)


def temp_truth(t: float) -> float:
    """Skin temperature (degC): slow drift around ~33 C."""
    return 33.0 + 0.5 * math.sin(2 * math.pi * t / 120.0)


# ── Stream definitions ────────────────────────────────────────────────────

@dataclass
class MockStream:
    name: str
    stype: str
    channels: List[tuple]          # list of (label, unit, type)
    srate: float
    fn: Callable[[float, np.random.Generator], List[float]]  # (t, rng) -> sample
    source_id: str
    outlet: Optional[StreamOutlet] = None
    _next_t: float = 0.0           # absolute local_clock time of next sample
    _n: int = 0


def _make_streams(seed: int = 0, lag_s: float = 0.040,
                  ecg_noise_uv: float = 8.0) -> List[MockStream]:
    """Build the mock stream set. `lag_s` is the fixed delay applied to the
    BIOPAC (reference) signals relative to the wearable, so the pipeline's
    lag-estimation can be exercised. `ecg_noise_uv` is wearable ECG noise."""

    def polar_ecg_fn(t, rng):
        ecg_uv = ecg_truth(t) * 1000.0 + rng.normal(0, ecg_noise_uv)
        # synthetic accelerometer: slow sway + noise (mg-ish)
        ax = 50.0 * math.sin(2 * math.pi * 0.2 * t) + rng.normal(0, 2)
        ay = 50.0 * math.cos(2 * math.pi * 0.2 * t) + rng.normal(0, 2)
        az = 1000.0 + rng.normal(0, 2)
        beat = 0.0
        return [ecg_uv, ax, ay, az, beat]

    def emo_eda_fn(t, rng):
        return [eda_truth(t) + rng.normal(0, 0.03)]

    def emo_ppg_fn(t, rng):
        return [ppg_truth(t) + rng.normal(0, 0.02)]

    def emo_temp_fn(t, rng):
        return [temp_truth(t) + rng.normal(0, 0.01)]

    def biopac_fn(t, rng):
        # Reference: same truth, delayed by lag_s, BIOPAC units + own noise.
        tr = t - lag_s
        ecg_mv = ecg_truth(tr) + rng.normal(0, 0.004)          # mV  (== wearable_uV / 1000)
        eda_us = eda_truth(tr) + rng.normal(0, 0.02)           # uS  (same unit as wearable)
        ppg = ppg_truth(tr) + rng.normal(0, 0.015)             # a.u.
        rsp = resp_truth(tr) + rng.normal(0, 0.02)             # a.u. (BIOPAC only)
        temp = temp_truth(tr) + 0.1 + rng.normal(0, 0.008)     # degC, +0.1 bias vs wearable
        return [ecg_mv, eda_us, ppg, rsp, temp]

    return [
        MockStream("P01_PolarECG", "ECG",
                   [("ECG", "uV", "ECG"), ("ACC_X", "mg", "ACC"),
                    ("ACC_Y", "mg", "ACC"), ("ACC_Z", "mg", "ACC"),
                    ("beat", "bool", "marker")],
                   130.0, polar_ecg_fn, "mock_polar_p01"),
        MockStream("P01_EmoEDA", "EDA", [("EDA", "uS", "EDA")],
                   15.0, emo_eda_fn, "mock_emo_eda_p01"),
        MockStream("P01_EmoPPG", "PPG", [("PPG", "a.u.", "PPG")],
                   25.0, emo_ppg_fn, "mock_emo_ppg_p01"),
        MockStream("P01_EmoTemp", "Temperature", [("TEMP", "degC", "Temperature")],
                   7.5, emo_temp_fn, "mock_emo_temp_p01"),
        MockStream("BIOPAC", "Physiology",
                   [("ECG", "mV", "ECG"), ("EDA", "uS", "EDA"),
                    ("PPG", "a.u.", "PPG"), ("RSP", "a.u.", "Respiration"),
                    ("TEMP", "degC", "Temperature")],
                   1000.0, biopac_fn, "mock_biopac"),
    ]


def _build_outlet(ms: MockStream) -> StreamOutlet:
    info = StreamInfo(ms.name, ms.stype, len(ms.channels), ms.srate,
                      cf_float32, ms.source_id)
    chans = info.desc().append_child("channels")
    for label, unit, ctype in ms.channels:
        c = chans.append_child("channel")
        c.append_child_value("label", label)
        c.append_child_value("unit", unit)
        c.append_child_value("type", ctype)
    return StreamOutlet(info, chunk_size=0, max_buffered=360)


def run_outlets(duration: float, stop_event: Optional[threading.Event] = None,
                seed: int = 0, verbose: bool = True) -> None:
    """Stream all mock outlets for `duration` seconds (0 = until stop_event or
    KeyboardInterrupt). Samples are timestamped on the LSL clock so a recorder
    captures a shared, alignable timeline across all streams."""
    streams = _make_streams(seed=seed)
    rng = np.random.default_rng(seed)
    for ms in streams:
        ms.outlet = _build_outlet(ms)
    if verbose:
        print("Mock outlets up:", ", ".join(s.name for s in streams))
        print("Waiting 1s for consumers to discover streams...")
    time.sleep(1.0)

    t0 = local_clock()
    for ms in streams:
        ms._next_t = t0
    t_end = (t0 + duration) if duration and duration > 0 else None

    try:
        while True:
            now = local_clock()
            if t_end is not None and now >= t_end:
                break
            if stop_event is not None and stop_event.is_set():
                break
            for ms in streams:
                period = 1.0 / ms.srate
                # push every sample whose scheduled time has arrived
                while ms._next_t <= now:
                    t = ms._next_t
                    sample = ms.fn(t - t0, rng)  # signal funcs use elapsed time
                    ms.outlet.push_sample([float(v) for v in sample], timestamp=t)
                    ms._n += 1
                    ms._next_t += period
            time.sleep(0.002)
    except KeyboardInterrupt:
        if verbose:
            print("\nInterrupted.")
    finally:
        if verbose:
            total = sum(s._n for s in streams)
            print(f"Stopped. Pushed {total} samples across {len(streams)} streams.")


def main():
    ap = argparse.ArgumentParser(description="Stream synthetic wearable + BIOPAC LSL data.")
    ap.add_argument("--duration", type=float, default=0.0,
                    help="seconds to stream (0 = forever / until Ctrl-C)")
    ap.add_argument("--seed", type=int, default=0)
    args = ap.parse_args()
    run_outlets(args.duration, seed=args.seed)


if __name__ == "__main__":
    main()
