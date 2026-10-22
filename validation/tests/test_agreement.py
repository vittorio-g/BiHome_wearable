"""
Unit tests for validation/agreement.py against synthetic inputs with a known
ground truth. Runnable two ways:
    .venv\\Scripts\\python.exe -m pytest validation/tests/test_agreement.py
    .venv\\Scripts\\python.exe validation/tests/test_agreement.py   (prints PASS/FAIL)
"""

import os
import sys

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import agreement as ag  # noqa: E402


def test_identical_signals():
    rng = np.random.default_rng(1)
    x = rng.normal(0, 1, 5000)
    assert abs(ag.pearson_r(x, x) - 1.0) < 1e-9
    assert abs(ag.ccc(x, x) - 1.0) < 1e-9
    assert abs(ag.icc_2_1(x, x) - 1.0) < 1e-6
    assert ag.rmse(x, x) < 1e-12
    assert abs(ag.coverage(x, x) - 1.0) < 1e-12
    ba = ag.bland_altman(x, x)
    assert abs(ba["bias"]) < 1e-12


def test_scale_offset_penalised_by_ccc_not_pearson():
    rng = np.random.default_rng(2)
    x = rng.normal(10, 3, 5000)
    y = 2.0 * x + 5.0  # perfect linear but different scale/offset
    assert abs(ag.pearson_r(x, y) - 1.0) < 1e-9   # pearson invariant
    assert ag.ccc(x, y) < 0.7                      # CCC strongly penalised


def test_known_bias():
    x = np.linspace(0, 100, 1000)
    y = x - 2.0           # x is 2.0 above y everywhere
    ba = ag.bland_altman(x, y)
    assert abs(ba["bias"] - 2.0) < 1e-9
    assert ba["sd_diff"] < 1e-9
    assert abs(ag.mae(x, y) - 2.0) < 1e-9


def test_spearman_monotonic_nonlinear():
    x = np.linspace(0.1, 5, 1000)
    y = x ** 3            # monotonic but nonlinear
    assert abs(ag.spearman_r(x, y) - 1.0) < 1e-9
    assert ag.pearson_r(x, y) < 0.98


def test_coverage_with_nans():
    x = np.arange(100, dtype=float)
    y = x.copy()
    y[::4] = np.nan       # 25% missing
    assert abs(ag.coverage(x, y) - 0.75) < 1e-9
    # metrics still computed on the clean 75%
    assert abs(ag.pearson_r(x, y) - 1.0) < 1e-9


def test_xcorr_lag_recovers_shift():
    fs = 100.0
    t = np.arange(0, 20, 1 / fs)
    base = np.sin(2 * np.pi * 1.0 * t) + np.sin(2 * np.pi * 2.3 * t)
    shift = 15  # samples; x delayed by 15 samples relative to y
    x = base.copy()
    y = np.roll(base, -shift)
    lag = ag.xcorr_lag(x, y, fs)
    assert abs(lag - shift / fs) < 1.5 / fs   # within ~1 sample


def test_compute_all_keys():
    rng = np.random.default_rng(3)
    x = rng.normal(0, 1, 2000)
    y = x + rng.normal(0, 0.1, 2000)
    res = ag.compute_all(x, y, fs=100.0)
    for k in ("n", "coverage", "pearson_r", "spearman_r", "ccc", "icc_2_1",
              "rmse", "mae", "mape_pct", "nrmse_pct", "bias", "sd_diff",
              "loa_lower", "loa_upper", "prop_bias_slope", "lag_s"):
        assert k in res
    assert res["pearson_r"] > 0.98
    assert abs(res["bias"]) < 0.05


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
