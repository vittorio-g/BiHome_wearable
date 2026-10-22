"""
sync.py — align two independently-clocked recordings (BiHome XDF on one PC,
BIOPAC .acq on another) using a shared TTL trigger.

The same trigger is recorded on both sides: as TTL pulses on a BIOPAC channel
(in the .acq) and as events on the BiHome side (LSL marker stream in the XDF).
Detecting those pulse times in each recording's own clock and fitting a linear
map between the two clocks lets us put the BIOPAC signals on the BiHome (LSL)
timeline, after which the existing agreement analysis applies unchanged.

Why a *linear* map and ≥2 pulses: the two PCs have independent clocks that
differ in both offset AND rate (drift). One pulse fixes only the offset; two or
more let us also fit the drift (slope), which matters over long recordings.

This module is pure-numpy and unit-tested; the .acq/XDF I/O lives elsewhere so
the alignment math can be verified without hardware.
"""

from typing import Tuple

import numpy as np


def detect_pulses(signal: np.ndarray, fs: float, threshold: float = None,
                  min_interval_s: float = 0.5, polarity: str = "rising") -> np.ndarray:
    """Return the times (seconds from the start of `signal`) of TTL edges.

    threshold: level for the high/low decision; if None, the midpoint between
    the signal's min and max is used (works for clean 0/5 V or 0/1 TTL).
    min_interval_s: debounce — edges closer than this are collapsed.
    polarity: 'rising' (default) or 'falling'.
    """
    signal = np.asarray(signal, dtype=float).ravel()
    if signal.size < 2 or fs <= 0:
        return np.array([])
    if threshold is None:
        lo, hi = np.nanmin(signal), np.nanmax(signal)
        if hi - lo < 1e-9:
            return np.array([])  # flat: no pulses
        threshold = (lo + hi) / 2.0
    high = signal > threshold
    if polarity == "rising":
        edges = np.where((~high[:-1]) & (high[1:]))[0] + 1
    elif polarity == "falling":
        edges = np.where((high[:-1]) & (~high[1:]))[0] + 1
    else:
        raise ValueError("polarity must be 'rising' or 'falling'")
    times = edges / fs
    # debounce
    if times.size and min_interval_s > 0:
        keep = [times[0]]
        for t in times[1:]:
            if t - keep[-1] >= min_interval_s:
                keep.append(t)
        times = np.array(keep)
    return times


def rising_edge_times(timestamps: np.ndarray, values: np.ndarray,
                      threshold: float = 0.5, min_interval_s: float = 0.5) -> np.ndarray:
    """Rising-edge times of a non-uniformly-sampled 0/1 channel, using its own
    per-sample timestamps (for the XDF trigger stream, which carries explicit
    LSL timestamps rather than a fixed rate). Returns the timestamp of each
    sample where the value crosses `threshold` upward."""
    ts = np.asarray(timestamps, dtype=float).ravel()
    v = np.asarray(values, dtype=float).ravel()
    n = min(ts.size, v.size)
    ts, v = ts[:n], v[:n]
    if n < 2:
        return np.array([])
    high = v > threshold
    idx = np.where((~high[:-1]) & (high[1:]))[0] + 1
    times = ts[idx]
    if times.size and min_interval_s > 0:
        keep = [times[0]]
        for t in times[1:]:
            if t - keep[-1] >= min_interval_s:
                keep.append(t)
        times = np.array(keep)
    return times


def fit_clock_map(t_ref: np.ndarray, t_target: np.ndarray) -> Tuple[float, float, float, int]:
    """Fit t_ref ≈ slope * t_target + intercept (least squares).

    `t_ref` are the shared-event times in the reference clock you want to map
    INTO (e.g. BiHome/LSL), `t_target` the SAME events in the other clock
    (e.g. BIOPAC .acq). Returns (slope, intercept, max_abs_residual_s, n).
    With a single event, slope is fixed to 1.0 (offset-only).
    """
    t_ref = np.asarray(t_ref, dtype=float).ravel()
    t_target = np.asarray(t_target, dtype=float).ravel()
    n = min(t_ref.size, t_target.size)
    if n == 0:
        raise ValueError("no shared events to fit")
    t_ref, t_target = t_ref[:n], t_target[:n]
    if n == 1:
        slope, intercept = 1.0, float(t_ref[0] - t_target[0])
    elif n == 2:
        slope, intercept = np.polyfit(t_target, t_ref, 1)
    else:
        # Theil-Sen: slope = median of pairwise slopes. Robust to a single pulse
        # with anomalous serial-write latency (USB jitter), which would skew
        # ordinary least squares — and hence the drift estimate.
        slopes = []
        for i in range(n):
            for j in range(i + 1, n):
                dx = t_target[j] - t_target[i]
                if dx != 0:
                    slopes.append((t_ref[j] - t_ref[i]) / dx)
        slope = float(np.median(slopes))
        intercept = float(np.median(t_ref - slope * t_target))
    resid = t_ref - (slope * t_target + intercept)
    max_resid = float(np.max(np.abs(resid))) if n else float("nan")
    return float(slope), float(intercept), max_resid, n


def map_times(t_target: np.ndarray, slope: float, intercept: float) -> np.ndarray:
    """Map times from the target clock into the reference clock."""
    return slope * np.asarray(t_target, dtype=float) + intercept


def match_pulses(a: np.ndarray, b: np.ndarray) -> Tuple[np.ndarray, np.ndarray]:
    """Pair two pulse-time lists in order. Requires equal counts — if they
    differ, raise with guidance (a missing/extra pulse must be resolved before
    a trustworthy fit). Order is assumed chronological."""
    a = np.sort(np.asarray(a, dtype=float).ravel())
    b = np.sort(np.asarray(b, dtype=float).ravel())
    if a.size != b.size:
        raise ValueError(
            f"pulse count mismatch: {a.size} vs {b.size}. Check the trigger "
            f"channel/threshold on each side; both recordings must contain the "
            f"same set of TTL pulses.")
    if a.size == 0:
        raise ValueError("no pulses detected on at least one side")
    # With >=3 pulses the inter-pulse spacings on the two clocks must stay
    # proportional (same drift). If they don't, the counts matched only by
    # coincidence (e.g. a spurious edge on one side + a missing one on the
    # other) and an in-order pairing would silently corrupt the clock fit.
    if a.size >= 3:
        da, db = np.diff(a), np.diff(b)
        if np.any(da <= 0) or np.any(db <= 0):
            raise ValueError("pulse times are not strictly increasing")
        ratios = da / db
        cv = float(np.std(ratios) / np.mean(ratios)) if np.mean(ratios) else 1.0
        if cv > 0.05:
            raise ValueError(
                f"pulse spacings inconsistent between sides (CV={cv:.1%}) — likely a "
                f"missing or spurious pulse. Inspect the trigger channel/threshold "
                f"before trusting the alignment.")
    return a, b
