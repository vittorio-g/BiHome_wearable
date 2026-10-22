"""
agreement.py — method-comparison / agreement metrics for validating a wearable
signal against a reference (BIOPAC) signal.

All functions take two 1-D numpy arrays x (wearable) and y (reference) that are
already time-aligned and the same length. NaNs are handled by pairwise masking
(a pair is dropped if either side is NaN). Functions are pure and dependency
-light (numpy only) so they can be unit-tested against synthetic inputs with a
known ground truth.

Glossary:
  pearson_r   linear correlation (scale/offset invariant)
  spearman_r  rank correlation (monotonic, robust to nonlinearity)
  ccc         Lin's Concordance Correlation Coefficient (penalises scale/offset)
  icc_2_1     ICC(2,1): two-way random effects, single rating, absolute agreement
  bland_altman bias (mean diff), SD of diff, 95% limits of agreement, prop. bias
  rmse/mae/mape error magnitudes; nrmse normalised by reference range
  coverage    fraction of usable (non-NaN) pairs
"""

from typing import Dict, Optional

import numpy as np


def _clean_pair(x: np.ndarray, y: np.ndarray):
    """Return (x, y, mask) with NaN/inf pairs removed."""
    x = np.asarray(x, dtype=float).ravel()
    y = np.asarray(y, dtype=float).ravel()
    n = min(x.size, y.size)
    x, y = x[:n], y[:n]
    mask = np.isfinite(x) & np.isfinite(y)
    return x[mask], y[mask], mask


def _ranks(a: np.ndarray) -> np.ndarray:
    """Average ranks (ties shared), numpy-only — avoids a scipy dependency."""
    order = np.argsort(a, kind="mergesort")
    ranks = np.empty(a.size, dtype=float)
    ranks[order] = np.arange(1, a.size + 1, dtype=float)
    # average tied ranks
    a_sorted = a[order]
    i = 0
    while i < a_sorted.size:
        j = i
        while j + 1 < a_sorted.size and a_sorted[j + 1] == a_sorted[i]:
            j += 1
        if j > i:
            avg = (i + j) / 2.0 + 1.0  # +1 because ranks are 1-based
            ranks[order[i:j + 1]] = avg
        i = j + 1
    return ranks


def pearson_r(x: np.ndarray, y: np.ndarray) -> float:
    x, y, _ = _clean_pair(x, y)
    if x.size < 2 or np.std(x) == 0 or np.std(y) == 0:
        return float("nan")
    return float(np.corrcoef(x, y)[0, 1])


def spearman_r(x: np.ndarray, y: np.ndarray) -> float:
    x, y, _ = _clean_pair(x, y)
    if x.size < 2:
        return float("nan")
    return pearson_r(_ranks(x), _ranks(y))


def ccc(x: np.ndarray, y: np.ndarray) -> float:
    """Lin's Concordance Correlation Coefficient."""
    x, y, _ = _clean_pair(x, y)
    if x.size < 2:
        return float("nan")
    mx, my = np.mean(x), np.mean(y)
    vx, vy = np.var(x), np.var(y)
    cov = np.mean((x - mx) * (y - my))
    denom = vx + vy + (mx - my) ** 2
    if denom == 0:
        return float("nan")
    return float(2 * cov / denom)


def icc_2_1(x: np.ndarray, y: np.ndarray) -> float:
    """ICC(2,1): two-way random effects, single measurement, absolute agreement.
    Computed from the n x 2 matrix [x, y] via mean squares."""
    x, y, _ = _clean_pair(x, y)
    n = x.size
    if n < 2:
        return float("nan")
    M = np.column_stack([x, y])
    k = 2  # raters
    grand = M.mean()
    row_means = M.mean(axis=1)
    col_means = M.mean(axis=0)
    # Sums of squares
    ss_total = ((M - grand) ** 2).sum()
    ss_row = k * ((row_means - grand) ** 2).sum()
    ss_col = n * ((col_means - grand) ** 2).sum()
    ss_err = ss_total - ss_row - ss_col
    ms_row = ss_row / (n - 1)
    ms_col = ss_col / (k - 1)
    ms_err = ss_err / ((n - 1) * (k - 1))
    denom = ms_row + (k - 1) * ms_err + k * (ms_col - ms_err) / n
    if denom == 0:
        return float("nan")
    return float((ms_row - ms_err) / denom)


def bland_altman(x: np.ndarray, y: np.ndarray) -> Dict[str, float]:
    """Bias and 95% limits of agreement of (x - y), plus a proportional-bias
    slope (OLS of diff on mean)."""
    x, y, _ = _clean_pair(x, y)
    if x.size < 2:
        return {k: float("nan") for k in
                ("bias", "sd_diff", "loa_lower", "loa_upper", "prop_bias_slope")}
    diff = x - y
    mean = (x + y) / 2.0
    bias = float(np.mean(diff))
    sd = float(np.std(diff, ddof=1))
    slope = float(np.polyfit(mean, diff, 1)[0]) if np.std(mean) > 0 else float("nan")
    return {
        "bias": bias,
        "sd_diff": sd,
        "loa_lower": bias - 1.96 * sd,
        "loa_upper": bias + 1.96 * sd,
        "prop_bias_slope": slope,
    }


def rmse(x: np.ndarray, y: np.ndarray) -> float:
    x, y, _ = _clean_pair(x, y)
    if x.size == 0:
        return float("nan")
    return float(np.sqrt(np.mean((x - y) ** 2)))


def mae(x: np.ndarray, y: np.ndarray) -> float:
    x, y, _ = _clean_pair(x, y)
    if x.size == 0:
        return float("nan")
    return float(np.mean(np.abs(x - y)))


def mape(x: np.ndarray, y: np.ndarray) -> float:
    """Mean absolute percentage error of x relative to reference y (%)."""
    x, y, _ = _clean_pair(x, y)
    nz = np.abs(y) > 1e-12
    if nz.sum() == 0:
        return float("nan")
    return float(np.mean(np.abs((x[nz] - y[nz]) / y[nz])) * 100.0)


def nrmse(x: np.ndarray, y: np.ndarray) -> float:
    """RMSE normalised by the reference value range (%)."""
    x, y, _ = _clean_pair(x, y)
    if x.size == 0:
        return float("nan")
    rng = np.ptp(y)
    if rng == 0:
        return float("nan")
    return float(rmse(x, y) / rng * 100.0)


def coverage(x: np.ndarray, y: np.ndarray) -> float:
    """Fraction of finite, usable sample pairs."""
    x = np.asarray(x, dtype=float).ravel()
    y = np.asarray(y, dtype=float).ravel()
    n = min(x.size, y.size)
    if n == 0:
        return float("nan")
    mask = np.isfinite(x[:n]) & np.isfinite(y[:n])
    return float(mask.sum() / n)


def xcorr_lag(x: np.ndarray, y: np.ndarray, fs: float, max_lag_s: float = 2.0) -> float:
    """Estimate the lag (seconds) of x relative to y by cross-correlation.
    Positive lag means x is delayed relative to y. Returns NaN if undefined."""
    x, y, _ = _clean_pair(x, y)
    if x.size < 2 or fs <= 0:
        return float("nan")
    x = x - np.mean(x)
    y = y - np.mean(y)
    if np.std(x) == 0 or np.std(y) == 0:
        return float("nan")
    max_lag = int(round(max_lag_s * fs))
    max_lag = min(max_lag, x.size - 1)
    # FFT cross-correlation: O(n log n) instead of np.correlate's O(n^2), which
    # is minutes-slow on long high-rate recordings (e.g. 130 Hz x 1 h ECG).
    try:
        from scipy.signal import correlate as _correlate
        corr = _correlate(x, y, mode="full", method="fft")
    except Exception:
        corr = np.correlate(x, y, mode="full")
    lags = np.arange(-(x.size - 1), x.size)
    center = x.size - 1
    lo, hi = center - max_lag, center + max_lag + 1
    window = corr[lo:hi]
    best = np.argmax(window)
    lag_samples = lags[lo:hi][best]
    return float(lag_samples / fs)


def compute_all(x: np.ndarray, y: np.ndarray, fs: Optional[float] = None) -> Dict[str, float]:
    """Compute the full agreement panel for a paired (wearable x, reference y)
    aligned signal. Returns a flat dict of metric -> value."""
    out: Dict[str, float] = {
        "n": int(_clean_pair(x, y)[0].size),
        "coverage": coverage(x, y),
        "pearson_r": pearson_r(x, y),
        "spearman_r": spearman_r(x, y),
        "ccc": ccc(x, y),
        "icc_2_1": icc_2_1(x, y),
        "rmse": rmse(x, y),
        "mae": mae(x, y),
        "mape_pct": mape(x, y),
        "nrmse_pct": nrmse(x, y),
    }
    out.update(bland_altman(x, y))
    if fs:
        out["lag_s"] = xcorr_lag(x, y, fs)
    return out
