"""
signal_specific.py — domain-specific agreement for ECG and EDA, on top of the
generic amplitude panel in agreement.py.

Both functions receive the wearable (w) and reference (r) signals ALREADY
aligned on a common time grid at sampling rate fs (as produced by
analyze_xdf._align_pair, lag removed). They run neurokit2 to extract features
and quantify agreement on what actually matters clinically:

  ECG: heart-rate time series agreement, HRV metric agreement (RMSSD/SDNN/
       pNN50/MeanHR), and R-peak detection agreement (sensitivity/PPV/F1 +
       mean absolute beat-timing error) via tolerance beat-matching.

  EDA: tonic (SCL) agreement, and phasic SCR agreement (count, event-matching
       F1, amplitude agreement).

neurokit2 is an optional/heavy dependency; import errors and per-signal
failures degrade gracefully to NaN-filled dicts so the general panel still
reports.
"""

from typing import Dict, Optional, Tuple

import numpy as np

import agreement as ag

try:
    import neurokit2 as nk
    _HAVE_NK = True
except Exception:  # pragma: no cover
    _HAVE_NK = False


def _nan(keys) -> Dict[str, float]:
    return {k: float("nan") for k in keys}


def _instantaneous_hr(peaks_idx: np.ndarray, fs: float, grid_n: int,
                      hr_fs: float = 4.0) -> Tuple[np.ndarray, np.ndarray]:
    """Beat times -> instantaneous HR (bpm) resampled on a uniform hr_fs grid
    spanning the recording. Returns (t_grid, hr)."""
    if peaks_idx.size < 3:
        return np.array([]), np.array([])
    beat_t = peaks_idx / fs
    rr = np.diff(beat_t)
    hr = 60.0 / rr
    hr_t = beat_t[1:]  # HR assigned to the later beat of each RR pair
    dur = grid_n / fs
    grid = np.arange(0, dur, 1.0 / hr_fs)
    hr_grid = np.interp(grid, hr_t, hr, left=hr[0], right=hr[-1])
    return grid, hr_grid


def _beat_match(pw: np.ndarray, pr: np.ndarray, fs: float,
                tol_s: float = 0.1) -> Dict[str, float]:
    """Match wearable R-peaks (pw) to reference R-peaks (pr) within tol_s.
    Reference peaks are ground truth -> sensitivity = TP/(TP+FN),
    PPV = TP/(TP+FP). Also mean absolute timing error of matched beats."""
    if pw.size == 0 or pr.size == 0:
        return _nan(("beat_sensitivity", "beat_ppv", "beat_f1",
                     "beat_abs_err_ms", "n_beats_ref", "n_beats_wear"))
    tol = tol_s * fs
    used = np.zeros(pr.size, dtype=bool)
    tp = 0
    errs = []
    for p in pw:
        j = int(np.argmin(np.abs(pr - p)))
        if not used[j] and abs(pr[j] - p) <= tol:
            used[j] = True
            tp += 1
            errs.append(abs(pr[j] - p) / fs * 1000.0)
    fp = pw.size - tp
    fn = pr.size - tp
    sens = tp / (tp + fn) if (tp + fn) else float("nan")
    ppv = tp / (tp + fp) if (tp + fp) else float("nan")
    f1 = (2 * sens * ppv / (sens + ppv)) if (sens + ppv) else float("nan")
    return {
        "beat_sensitivity": sens,
        "beat_ppv": ppv,
        "beat_f1": f1,
        "beat_abs_err_ms": float(np.mean(errs)) if errs else float("nan"),
        "n_beats_ref": int(pr.size),
        "n_beats_wear": int(pw.size),
    }


def _hrv_metrics(peaks_idx: np.ndarray, fs: float) -> Dict[str, float]:
    keys = ("rmssd", "sdnn", "pnn50", "mean_hr")
    if not _HAVE_NK or peaks_idx.size < 4:
        return _nan(keys)
    try:
        hrv = nk.hrv_time({"ECG_R_Peaks": peaks_idx}, sampling_rate=fs)
        rr = np.diff(peaks_idx) / fs
        return {
            "rmssd": float(hrv.get("HRV_RMSSD", [np.nan])[0]),
            "sdnn": float(hrv.get("HRV_SDNN", [np.nan])[0]),
            "pnn50": float(hrv.get("HRV_pNN50", [np.nan])[0]),
            "mean_hr": float(60.0 / np.mean(rr)) if rr.size else float("nan"),
        }
    except Exception:
        return _nan(keys)


def _rpeaks(sig: np.ndarray, fs: float) -> np.ndarray:
    if not _HAVE_NK:
        return np.array([], dtype=int)
    try:
        _, info = nk.ecg_peaks(nk.ecg_clean(sig, sampling_rate=fs), sampling_rate=fs)
        return np.asarray(info["ECG_R_Peaks"], dtype=int)
    except Exception:
        return np.array([], dtype=int)


def ecg_agreement(w: np.ndarray, r: np.ndarray, fs: float,
                  out_dir: Optional[str] = None, name: str = "ECG") -> Tuple[Dict, Dict]:
    """ECG-specific agreement. Returns (metrics, plots)."""
    metrics: Dict[str, float] = {}
    plots: Dict[str, str] = {}
    if not _HAVE_NK:
        metrics["note"] = "neurokit2 not installed"
        return metrics, plots

    pw, pr = _rpeaks(w, fs), _rpeaks(r, fs)
    metrics.update(_beat_match(pw, pr, fs))

    # HRV per device + paired difference
    hw, hr_ = _hrv_metrics(pw, fs), _hrv_metrics(pr, fs)
    for k in ("rmssd", "sdnn", "pnn50", "mean_hr"):
        metrics[f"hrv_{k}_wear"] = hw[k]
        metrics[f"hrv_{k}_ref"] = hr_[k]
        metrics[f"hrv_{k}_absdiff"] = abs(hw[k] - hr_[k])

    # Instantaneous HR time-series agreement
    tw, hrw = _instantaneous_hr(pw, fs, len(w))
    tr, hrr = _instantaneous_hr(pr, fs, len(r))
    if hrw.size and hrr.size:
        n = min(hrw.size, hrr.size)
        hr_panel = ag.compute_all(hrw[:n], hrr[:n], fs=None)
        for k in ("pearson_r", "ccc", "icc_2_1", "rmse", "mae", "bias"):
            metrics[f"hr_{k}"] = hr_panel[k]
        if out_dir is not None:
            import matplotlib.pyplot as plt
            fig, axh = plt.subplots(figsize=(9, 3.0))
            axh.plot(tw[:n], hrw[:n], lw=1.0, label="wearable HR")
            axh.plot(tr[:n], hrr[:n], lw=1.0, alpha=0.8, label="reference HR")
            axh.set_title(f"{name} — instantaneous HR "
                          f"(r={hr_panel['pearson_r']:.3f}, bias={hr_panel['bias']:.2f} bpm)")
            axh.set_xlabel("time (s)"); axh.set_ylabel("HR (bpm)"); axh.legend(loc="upper right")
            import os
            fig.tight_layout(); p = os.path.join(out_dir, f"{name}_hr.png")
            fig.savefig(p, dpi=110); plt.close(fig); plots["hr"] = os.path.basename(p)
    return metrics, plots


def _eda_decompose(sig: np.ndarray, fs: float):
    """Return (tonic, phasic, scr_peaks_idx, scr_amplitude) or (None,...)."""
    if not _HAVE_NK:
        return None, None, np.array([]), np.array([])
    try:
        comp = nk.eda_phasic(nk.eda_clean(sig, sampling_rate=fs), sampling_rate=fs)
        tonic = comp["EDA_Tonic"].values
        phasic = comp["EDA_Phasic"].values
        _, info = nk.eda_peaks(phasic, sampling_rate=fs)
        peaks = np.asarray(info.get("SCR_Peaks", []), dtype=int)
        amp = np.asarray(info.get("SCR_Amplitude", []), dtype=float)
        return tonic, phasic, peaks, amp
    except Exception:
        return None, None, np.array([]), np.array([])


def _event_match(pw: np.ndarray, pr: np.ndarray, fs: float,
                 tol_s: float = 2.0) -> Dict[str, float]:
    """SCR event matching (looser tolerance than beats)."""
    m = _beat_match(pw, pr, fs, tol_s=tol_s)
    return {
        "scr_sensitivity": m["beat_sensitivity"],
        "scr_ppv": m["beat_ppv"],
        "scr_f1": m["beat_f1"],
        "scr_count_wear": m["n_beats_wear"],
        "scr_count_ref": m["n_beats_ref"],
    }


def eda_agreement(w: np.ndarray, r: np.ndarray, fs: float,
                  out_dir: Optional[str] = None, name: str = "EDA") -> Tuple[Dict, Dict]:
    """EDA-specific agreement (tonic SCL + phasic SCR). Returns (metrics, plots)."""
    metrics: Dict[str, float] = {}
    plots: Dict[str, str] = {}
    if not _HAVE_NK:
        metrics["note"] = "neurokit2 not installed"
        return metrics, plots

    tw, phw, pkw, ampw = _eda_decompose(w, fs)
    tr, phr, pkr, ampr = _eda_decompose(r, fs)
    if tw is not None and tr is not None:
        n = min(len(tw), len(tr))
        tonic_panel = ag.compute_all(tw[:n], tr[:n], fs=None)
        for k in ("pearson_r", "ccc", "icc_2_1", "rmse", "bias"):
            metrics[f"scl_{k}"] = tonic_panel[k]
    metrics.update(_event_match(pkw, pkr, fs))
    # phasic SCR amplitude agreement (mean per device + absolute difference)
    if ampw.size:
        metrics["scr_amp_mean_wear"] = float(np.nanmean(ampw))
    if ampr.size:
        metrics["scr_amp_mean_ref"] = float(np.nanmean(ampr))
    if ampw.size and ampr.size:
        metrics["scr_amp_absdiff"] = abs(metrics["scr_amp_mean_wear"]
                                         - metrics["scr_amp_mean_ref"])

    if out_dir is not None and tw is not None and tr is not None:
        import os
        import matplotlib.pyplot as plt
        n = min(len(tw), len(tr))
        t = np.arange(n) / fs
        fig, (a1, a2) = plt.subplots(2, 1, figsize=(9, 4.4), sharex=True)
        a1.plot(t, tw[:n], lw=1.0, label="wearable SCL")
        a1.plot(t, tr[:n], lw=1.0, alpha=0.8, label="reference SCL")
        a1.set_ylabel("tonic (uS)"); a1.legend(loc="upper right")
        a1.set_title(f"{name} — tonic/phasic decomposition")
        a2.plot(t, phw[:n], lw=0.8, label="wearable phasic")
        a2.plot(t, phr[:n], lw=0.8, alpha=0.8, label="reference phasic")
        a2.set_xlabel("time (s)"); a2.set_ylabel("phasic (uS)"); a2.legend(loc="upper right")
        fig.tight_layout(); p = os.path.join(out_dir, f"{name}_eda_decomp.png")
        fig.savefig(p, dpi=110); plt.close(fig); plots["eda_decomp"] = os.path.basename(p)
    return metrics, plots


def run_for_kind(kind: str, w: np.ndarray, r: np.ndarray, fs: float,
                 out_dir: Optional[str], name: str) -> Tuple[Dict, Dict]:
    """Dispatch to the right signal-specific analysis for a pair 'kind'."""
    kind = (kind or "").lower()
    if kind == "ecg":
        return ecg_agreement(w, r, fs, out_dir, name)
    if kind == "eda":
        return eda_agreement(w, r, fs, out_dir, name)
    return {}, {}
