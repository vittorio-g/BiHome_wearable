"""
live_monitor.py — real-time agreement monitor (standalone, console).

Subscribes directly to the LSL streams named in channel_map.json and prints,
once per second, a rolling-window readout per pair: latest wearable & reference
values, the rolling Pearson r and the rolling bias (wearable - reference). It
is a SANITY MONITOR — "are the two devices tracking each other right now?" — not
a substitute for the rigorous offline report (analyze_xdf.py).

It is deliberately standalone (it does NOT modify the BiHome viewer): lower risk
and runnable next to a live session or the mock streams.

Usage:
    .venv\\Scripts\\python.exe validation\\live_monitor.py            # until Ctrl-C
    .venv\\Scripts\\python.exe validation\\live_monitor.py --duration 15 --window 10
"""

import argparse
import json
import os
import sys
import time
from collections import deque

import numpy as np
import pylsl

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import agreement as ag  # noqa: E402

_HERE = os.path.dirname(os.path.abspath(__file__))


def _channel_index(info, spec):
    if isinstance(spec, int):
        return spec
    ch = info.desc().child("channels").child("channel")
    for i in range(info.channel_count()):
        if ch.child_value("label") == spec:
            return i
        ch = ch.next_sibling()
    raise KeyError(f"channel {spec!r} not in {info.name()}")


class _Sub:
    """One stream subscription with a rolling (timestamp, value) buffer per
    channel of interest."""

    def __init__(self, inlet, window_s):
        self.inlet = inlet
        self.window_s = window_s
        self.ts = deque()
        self.data = deque()  # rows: full sample (list of floats)

    def pump(self):
        while True:
            chunk, stamps = self.inlet.pull_chunk(timeout=0.0, max_samples=2048)
            if not chunk:
                break
            for s, t in zip(chunk, stamps):
                self.ts.append(t)
                self.data.append(s)
        # trim to window
        if self.ts:
            tmax = self.ts[-1]
            while self.ts and (tmax - self.ts[0]) > self.window_s:
                self.ts.popleft()
                self.data.popleft()

    def channel(self, idx):
        if not self.ts:
            return np.array([]), np.array([])
        ts = np.fromiter(self.ts, dtype=float)
        vals = np.array(self.data, dtype=float)
        if vals.ndim == 1:
            vals = vals.reshape(-1, 1)
        return ts, vals[:, idx]


def main():
    ap = argparse.ArgumentParser(description="Rolling-window agreement monitor.")
    ap.add_argument("--map", default=os.path.join(_HERE, "channel_map.json"))
    ap.add_argument("--window", type=float, default=10.0, help="rolling window (s)")
    ap.add_argument("--duration", type=float, default=0.0, help="0 = until Ctrl-C")
    ap.add_argument("--grid-fs", type=float, default=25.0, help="resample rate for r/bias")
    args = ap.parse_args()

    with open(args.map, encoding="utf-8") as f:
        cmap = json.load(f)

    needed = set()
    for p in cmap["pairs"]:
        needed.add(p["wearable"]["stream"])
        needed.add(p["reference"]["stream"])

    print(f"Resolving {len(needed)} streams: {', '.join(sorted(needed))} ...")
    infos = {i.name(): i for i in pylsl.resolve_streams(wait_time=3.0)}
    subs, idx, full = {}, {}, {}
    for name in needed:
        if name not in infos:
            print(f"  (missing) {name}")
            continue
        inlet = pylsl.StreamInlet(infos[name], max_buflen=int(args.window) + 2,
                                  processing_flags=pylsl.proc_clocksync)
        # resolve_streams() returns a lightweight StreamInfo with no channel
        # description; the full <desc> (channel labels) is only available from
        # an opened inlet.
        try:
            full[name] = inlet.info(timeout=5.0)
        except Exception:
            full[name] = infos[name]
        subs[name] = _Sub(inlet, args.window)

    pairs = [p for p in cmap["pairs"]
             if p["wearable"]["stream"] in subs and p["reference"]["stream"] in subs]
    if not pairs:
        print("No pair has both streams available — nothing to monitor.")
        return 1
    for p in pairs:
        idx[p["name"]] = (
            _channel_index(full[p["wearable"]["stream"]], p["wearable"]["channel"]),
            _channel_index(full[p["reference"]["stream"]], p["reference"]["channel"]),
        )

    print(f"Monitoring {len(pairs)} pair(s); window={args.window:.0f}s. Ctrl-C to stop.\n")
    t_end = (time.time() + args.duration) if args.duration > 0 else None
    try:
        while True:
            if t_end and time.time() >= t_end:
                break
            for s in subs.values():
                s.pump()
            rows = []
            for p in pairs:
                name = p["name"]
                wi, ri = idx[name]
                tw, vw = subs[p["wearable"]["stream"]].channel(wi)
                tr, vr = subs[p["reference"]["stream"]].channel(ri)
                scale = float(p.get("scale_reference_to_wearable", 1.0))
                if tw.size < 5 or tr.size < 5:
                    rows.append(f"  {name:5s}  (warming up)")
                    continue
                t0 = max(tw[0], tr[0]); t1 = min(tw[-1], tr[-1])
                if t1 - t0 < 1.0:
                    rows.append(f"  {name:5s}  (no overlap yet)")
                    continue
                grid = np.arange(t0, t1, 1.0 / args.grid_fs)
                w = np.interp(grid, tw, vw)
                r = np.interp(grid, tr, vr) * scale
                # remove a constant in-window timing offset (matters for ECG)
                lag = ag.xcorr_lag(w, r, args.grid_fs, max_lag_s=0.5)
                if np.isfinite(lag):
                    r = np.interp(grid - lag, tr, vr) * scale
                rr = ag.pearson_r(w, r)
                bias = float(np.mean(w - r))
                rows.append(f"  {name:5s}  wear={vw[-1]:9.3g}  ref={vr[-1] * scale:9.3g}  "
                            f"r={rr:5.2f}  bias={bias:9.3g}")
            stamp = time.strftime("%H:%M:%S")
            print(f"[{stamp}] rolling {args.window:.0f}s\n" + "\n".join(rows) + "\n")
            time.sleep(1.0)
    except KeyboardInterrupt:
        pass
    print("Monitor stopped.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
