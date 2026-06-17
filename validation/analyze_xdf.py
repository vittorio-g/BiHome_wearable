"""
analyze_xdf.py — offline agreement analysis of a BiHome validation recording.

Reads an XDF that contains both wearable streams and the reference (BIOPAC)
stream, time-aligns each configured channel pair on the shared LSL clock,
computes the agreement panel (validation/agreement.py), and writes a report
(per-pair metrics + time-series / scatter / Bland-Altman figures + HTML).

Usage:
    .venv\\Scripts\\python.exe validation\\analyze_xdf.py path\\to\\recording.xdf
    .venv\\Scripts\\python.exe validation\\analyze_xdf.py rec.xdf --map validation/channel_map.json
"""

import argparse
import json
import os
import sys

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import agreement as ag  # noqa: E402
import signal_specific as ss  # noqa: E402
import acq_sync as acqs  # noqa: E402
import sync  # noqa: E402

import matplotlib
matplotlib.use("Agg")  # headless: save PNGs, never open a window
import matplotlib.pyplot as plt  # noqa: E402
import pyxdf  # noqa: E402

_HERE = os.path.dirname(os.path.abspath(__file__))


# ── XDF helpers ────────────────────────────────────────────────────────────

def _channel_labels(stream) -> list:
    """Best-effort extraction of channel labels from a pyxdf stream info."""
    try:
        desc = stream["info"]["desc"][0]
        chans = desc["channels"][0]["channel"]
        return [c["label"][0] for c in chans]
    except Exception:
        n = int(stream["info"]["channel_count"][0])
        return [f"ch{i}" for i in range(n)]


def _resolve_channel(stream, spec) -> int:
    """Map a channel spec (int index or string label) to a column index."""
    if isinstance(spec, int):
        return spec
    labels = _channel_labels(stream)
    if spec in labels:
        return labels.index(spec)
    raise KeyError(f"channel {spec!r} not found in {stream['info']['name'][0]} "
                   f"(labels: {labels})")


def _load_streams_by_name(path):
    streams, _ = pyxdf.load_xdf(path, dejitter_timestamps=True)
    by_name = {}
    for s in streams:
        by_name[s["info"]["name"][0]] = s
    return by_name


# ── Alignment ──────────────────────────────────────────────────────────────

def _series(stream, ch_idx):
    ts = np.asarray(stream["time_stamps"], dtype=float)
    data = np.asarray(stream["time_series"], dtype=float)
    if data.ndim == 1:
        data = data.reshape(-1, 1)
    return ts, data[:, ch_idx]


def _align_pair(w_stream, w_idx, r_stream, r_idx, scale_ref,
                lag_correct=True, max_lag_s=2.0):
    """Resample wearable and reference channels onto a common time grid over
    their overlapping window. Grid rate = wearable nominal rate (fallback:
    measured). A constant timing offset between the two devices is estimated by
    cross-correlation and (optionally) removed before amplitude agreement is
    assessed — the estimated lag is reported separately. Returns
    (grid_t, w, r_scaled, fs, lag_s)."""
    tw, vw = _series(w_stream, w_idx)
    tr, vr = _series(r_stream, r_idx)
    t0 = max(tw[0], tr[0])
    t1 = min(tw[-1], tr[-1])
    if t1 <= t0:
        raise ValueError("no temporal overlap between wearable and reference")
    try:
        fs = float(w_stream["info"]["nominal_srate"][0])
    except Exception:
        fs = 0.0
    if fs <= 0:
        fs = (len(tw) - 1) / (tw[-1] - tw[0])
    grid = np.arange(t0, t1, 1.0 / fs)
    w = np.interp(grid, tw, vw)
    r = np.interp(grid, tr, vr) * float(scale_ref)
    # Estimate the wearable-vs-reference timing offset and remove it so that
    # amplitude metrics are not dominated by a constant lag (critical for sharp
    # signals like ECG). The lag itself is reported as a metric.
    lag = ag.xcorr_lag(w, r, fs, max_lag_s=max_lag_s)
    if lag_correct and np.isfinite(lag):
        r = np.interp(grid - lag, tr, vr) * float(scale_ref)
    return grid, w, r, fs, lag


# ── Plotting ───────────────────────────────────────────────────────────────

def _plot_pair(name, grid, w, r, metrics, out_dir):
    t = grid - grid[0]
    paths = {}

    # Time-series overlay (first ~20 s for readability)
    fig, axp = plt.subplots(figsize=(9, 3.2))
    sel = t <= min(20.0, t[-1])
    axp.plot(t[sel], w[sel], lw=0.8, label="wearable")
    axp.plot(t[sel], r[sel], lw=0.8, alpha=0.8, label="reference (scaled)")
    axp.set_title(f"{name} — time series (first {t[sel][-1]:.0f}s)")
    axp.set_xlabel("time (s)"); axp.set_ylabel(name); axp.legend(loc="upper right")
    fig.tight_layout(); p = os.path.join(out_dir, f"{name}_timeseries.png")
    fig.savefig(p, dpi=110); plt.close(fig); paths["timeseries"] = os.path.basename(p)

    # Scatter with identity line
    fig, axs = plt.subplots(figsize=(4.2, 4.2))
    step = max(1, len(w) // 4000)
    axs.scatter(r[::step], w[::step], s=4, alpha=0.3)
    lo = min(np.nanmin(w), np.nanmin(r)); hi = max(np.nanmax(w), np.nanmax(r))
    axs.plot([lo, hi], [lo, hi], "k--", lw=1, label="identity")
    axs.set_title(f"{name} — scatter (r={metrics['pearson_r']:.3f}, CCC={metrics['ccc']:.3f})")
    axs.set_xlabel("reference"); axs.set_ylabel("wearable"); axs.legend(loc="upper left")
    fig.tight_layout(); p = os.path.join(out_dir, f"{name}_scatter.png")
    fig.savefig(p, dpi=110); plt.close(fig); paths["scatter"] = os.path.basename(p)

    # Bland-Altman
    diff = w - r; mean = (w + r) / 2.0
    fig, axb = plt.subplots(figsize=(5.0, 3.6))
    axb.scatter(mean[::step], diff[::step], s=4, alpha=0.3)
    for y, ls, lab in ((metrics["bias"], "-", "bias"),
                       (metrics["loa_upper"], "--", "+1.96 SD"),
                       (metrics["loa_lower"], "--", "-1.96 SD")):
        axb.axhline(y, ls=ls, color="r" if ls == "-" else "gray", lw=1)
    axb.set_title(f"{name} — Bland-Altman (bias={metrics['bias']:.4g})")
    axb.set_xlabel("mean of wearable & reference"); axb.set_ylabel("wearable - reference")
    fig.tight_layout(); p = os.path.join(out_dir, f"{name}_bland_altman.png")
    fig.savefig(p, dpi=110); plt.close(fig); paths["bland_altman"] = os.path.basename(p)
    return paths


# ── Report ─────────────────────────────────────────────────────────────────

_METRIC_ORDER = ["n", "coverage", "pearson_r", "spearman_r", "ccc", "icc_2_1",
                 "rmse", "mae", "mape_pct", "nrmse_pct", "bias", "sd_diff",
                 "loa_lower", "loa_upper", "prop_bias_slope", "lag_s"]


def _write_html(results, xdf_path, out_dir):
    rows = ""
    head = "".join(f"<th>{m}</th>" for m in _METRIC_ORDER)
    for name, r in results.items():
        m = r["metrics"]
        cells = "".join(
            f"<td>{m[k]:.4g}</td>" if isinstance(m.get(k), float) else f"<td>{m.get(k,'')}</td>"
            for k in _METRIC_ORDER)
        rows += f"<tr><th>{name}</th>{cells}</tr>\n"
    figs = ""
    for name, res in results.items():
        p = res["plots"]
        figs += f"<h2>{name}</h2>\n<img src='{p['timeseries']}'><br>" \
                f"<img src='{p['scatter']}'><img src='{p['bland_altman']}'>\n"
        for fn in (res.get("extra_plots") or {}).values():
            figs += f"<br><img src='{fn}'>\n"
        ex = res.get("extra") or {}
        if ex:
            er = "".join(
                f"<tr><th>{k}</th><td>{(f'{v:.4g}' if isinstance(v, float) else v)}</td></tr>"
                for k, v in ex.items())
            figs += f"<h3>{name} — signal-specific</h3><table>{er}</table>\n"
    html = f"""<!doctype html><html><head><meta charset="utf-8">
<title>BiHome validation report</title>
<style>body{{font-family:system-ui,Arial;margin:24px;color:#1c1c1c}}
table{{border-collapse:collapse;font-size:12px;margin:12px 0}}
th,td{{border:1px solid #ccc;padding:4px 8px;text-align:right}}
th{{background:#f0f0f0}} img{{max-width:760px;margin:6px 0}}
h2{{margin-top:28px}}</style></head><body>
<h1>BiHome wearable — validation report</h1>
<p>Recording: <code>{os.path.basename(xdf_path)}</code></p>
<h2>Agreement metrics</h2>
<table><tr><th>pair</th>{head}</tr>{rows}</table>
{figs}
</body></html>"""
    p = os.path.join(out_dir, "report.html")
    with open(p, "w", encoding="utf-8") as f:
        f.write(html)
    return p


def main():
    ap = argparse.ArgumentParser(description="Agreement analysis of a validation XDF.")
    ap.add_argument("xdf", help="path to the .xdf recording")
    ap.add_argument("--map", default=os.path.join(_HERE, "channel_map.json"))
    ap.add_argument("--out", default=None, help="output dir (default: validation/reports/<stem>)")
    ap.add_argument("--no-lag-correct", dest="lag_correct", action="store_false",
                    help="do NOT remove the estimated timing offset before amplitude metrics")
    # TTL-sync path: pull the reference (BIOPAC) signals from a separate .acq
    # recording, aligned to this XDF via a shared trigger.
    ap.add_argument("--acq", default=None, help="BIOPAC .acq file to align via TTL")
    ap.add_argument("--acq-trigger", default=None,
                    help="trigger channel in the .acq (name or integer index)")
    ap.add_argument("--xdf-trigger", default=None,
                    help="name of the LSL marker stream in the XDF carrying the TTL events")
    ap.add_argument("--acq-threshold", type=float, default=None,
                    help="TTL high/low threshold for pulse detection (default: auto)")
    args = ap.parse_args()

    if not os.path.isfile(args.xdf):
        print(f"ERROR: no such file: {args.xdf}", file=sys.stderr); return 1
    with open(args.map, encoding="utf-8") as f:
        cmap = json.load(f)

    stem = os.path.splitext(os.path.basename(args.xdf))[0]
    out_dir = args.out or os.path.join(_HERE, "reports", stem)
    os.makedirs(out_dir, exist_ok=True)

    streams = _load_streams_by_name(args.xdf)
    print(f"Loaded {len(streams)} streams: {', '.join(streams)}")

    # Optional: align a BIOPAC .acq recording onto this XDF's LSL timeline via a
    # shared TTL trigger, then treat its channels as a normal "BIOPAC" stream.
    if args.acq:
        if not args.acq_trigger or not args.xdf_trigger:
            print("ERROR: --acq requires --acq-trigger and --xdf-trigger", file=sys.stderr)
            return 1
        if args.xdf_trigger not in streams:
            print(f"ERROR: XDF marker stream {args.xdf_trigger!r} not found "
                  f"(streams: {', '.join(streams)})", file=sys.stderr)
            return 1
        trig_stream = streams[args.xdf_trigger]
        trig_ts = np.asarray(trig_stream["time_stamps"], dtype=float)
        trig_vals = np.asarray(trig_stream["time_series"], dtype=float)
        if trig_vals.ndim > 1:
            trig_vals = trig_vals[:, 0]
        marker_times = sync.rising_edge_times(trig_ts, trig_vals)
        print(f"XDF trigger '{args.xdf_trigger}': {marker_times.size} rising edge(s)")
        trig = args.acq_trigger
        try:
            trig = int(trig)
        except ValueError:
            pass
        try:
            diag = acqs.inject_biopac_from_acq(
                streams, marker_times, args.acq, trig,
                threshold=args.acq_threshold)
        except Exception as e:
            print(f"ERROR aligning .acq: {type(e).__name__}: {e}", file=sys.stderr)
            return 1
        print(f"TTL-aligned BIOPAC .acq: {diag['n_pulses']} pulse(s), "
              f"slope={diag['slope']:.6f}, offset={diag['intercept']:.3f}s, "
              f"max_residual={diag['max_resid_s'] * 1000:.1f}ms, "
              f"channels={diag['channels']}")

    results = {}
    for pair in cmap["pairs"]:
        name = pair["name"]
        ws, rs = pair["wearable"]["stream"], pair["reference"]["stream"]
        if ws not in streams or rs not in streams:
            print(f"SKIP {name}: missing stream ({ws} or {rs}) in recording")
            continue
        try:
            w_idx = _resolve_channel(streams[ws], pair["wearable"]["channel"])
            r_idx = _resolve_channel(streams[rs], pair["reference"]["channel"])
            grid, w, r, fs, lag = _align_pair(
                streams[ws], w_idx, streams[rs], r_idx,
                pair.get("scale_reference_to_wearable", 1.0),
                lag_correct=args.lag_correct)
        except Exception as e:
            print(f"SKIP {name}: {type(e).__name__}: {e}")
            continue
        metrics = ag.compute_all(w, r, fs=None)
        metrics["lag_s"] = lag  # estimated offset (removed before amplitude metrics)
        plots = _plot_pair(name, grid, w, r, metrics, out_dir)
        extra, extra_plots = ss.run_for_kind(pair.get("kind"), w, r, fs, out_dir, name)
        results[name] = {"metrics": metrics, "plots": plots,
                         "extra": extra, "extra_plots": extra_plots}
        print(f"  {name:5s}  n={metrics['n']:6d}  r={metrics['pearson_r']:.3f}  "
              f"CCC={metrics['ccc']:.3f}  ICC={metrics['icc_2_1']:.3f}  "
              f"bias={metrics['bias']:.4g}  lag={metrics.get('lag_s', float('nan')):.3f}s")
        sk = [k for k in ("hr_pearson_r", "hr_bias", "beat_f1", "beat_abs_err_ms",
                          "scl_pearson_r", "scl_ccc", "scr_f1",
                          "scr_count_wear", "scr_count_ref") if k in extra]
        if sk:
            print("         " + "  ".join(
                (f"{k}={extra[k]:.4g}" if isinstance(extra[k], float) else f"{k}={extra[k]}")
                for k in sk))

    if not results:
        print("No pairs analysed (check channel_map vs streams in the XDF).")
        return 1

    with open(os.path.join(out_dir, "metrics.json"), "w", encoding="utf-8") as f:
        json.dump({k: {**v["metrics"], **v.get("extra", {})} for k, v in results.items()},
                  f, indent=2)
    # CSV
    with open(os.path.join(out_dir, "metrics.csv"), "w", encoding="utf-8") as f:
        f.write("pair," + ",".join(_METRIC_ORDER) + "\n")
        for name, r in results.items():
            m = r["metrics"]
            f.write(name + "," + ",".join(str(m.get(k, "")) for k in _METRIC_ORDER) + "\n")
    html = _write_html(results, args.xdf, out_dir)
    print(f"\nReport: {html}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
