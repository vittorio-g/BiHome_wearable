"""
acq_sync.py — bring a BIOPAC AcqKnowledge .acq recording onto the BiHome LSL
timeline using a shared TTL trigger, so the existing agreement analysis applies.

Setup assumed (chosen for this study): the TTL trigger is recorded
  - on the BIOPAC side as pulses on a digital/analog channel in the .acq, and
  - on the BiHome side as events in an LSL **marker stream** (a LabJack / trigger
    box turns each TTL edge into an LSL marker), captured in the XDF.

Pipeline:
  1. read .acq (bioread) -> per-channel samples + rates
  2. detect TTL pulse times on the trigger channel (sync.detect_pulses)
  3. take the XDF marker timestamps (LSL clock) as the same events
  4. fit the clock map LSL ≈ slope*acq + intercept (sync.fit_clock_map)
  5. resample the BIOPAC physiology channels onto a common grid, map that grid
     to LSL time, and expose them as a pyxdf-shaped pseudo-stream named "BIOPAC"
  6. inject it into analyze_xdf's stream dict -> pairs analyse as usual

The numeric path (steps 2-6, minus the bioread file read) is unit-tested in
tests/test_acq_sync.py with synthetic two-clock data.
"""

from typing import Dict, List, Optional, Tuple

import numpy as np

import sync


# ── pyxdf-shaped pseudo-stream (so analyze_xdf can consume it unchanged) ──────

def make_pseudo_stream(name: str, time_stamps: np.ndarray, time_series: np.ndarray,
                       labels: List[str], fs: float) -> dict:
    """Build a dict matching the subset of the pyxdf stream structure that
    analyze_xdf uses (name, channel_count, nominal_srate, desc/channels/label,
    time_stamps, time_series)."""
    return {
        "info": {
            "name": [name],
            "channel_count": [str(len(labels))],
            "nominal_srate": [str(fs)],
            "desc": [{"channels": [{"channel": [{"label": [l]} for l in labels]}]}],
        },
        "time_stamps": np.asarray(time_stamps, dtype=float),
        "time_series": np.asarray(time_series, dtype=float),
    }


# ── .acq reading (bioread) ───────────────────────────────────────────────────

def read_acq(path: str) -> Dict[str, dict]:
    """Read a .acq file into {channel_name: {'fs': float, 'samples': np.ndarray}}."""
    import bioread  # imported lazily so the module loads without a real .acq
    data = bioread.read_file(path)
    chans = {}
    for ch in data.channels:
        if ch.data is None or getattr(ch, "samples_per_second", 0) in (None, 0):
            continue  # skip empty / rate-less channels rather than crashing later
        name = ch.name
        if name in chans:
            # AcqKnowledge often repeats names (e.g. several "Digital input");
            # disambiguate so channels don't silently overwrite each other.
            k = 2
            while f"{name}_{k}" in chans:
                k += 1
            name = f"{name}_{k}"
        chans[name] = {
            "fs": float(ch.samples_per_second),
            "samples": np.asarray(ch.data, dtype=float),
        }
    return chans


def trigger_times(chans: Dict[str, dict], trigger_channel, threshold: float = None,
                  min_interval_s: float = 0.5, polarity: str = "rising") -> np.ndarray:
    """Detect TTL pulse times (s from acq start) on the named/indexed channel."""
    if isinstance(trigger_channel, int):
        name = list(chans.keys())[trigger_channel]
    else:
        name = trigger_channel
    if name not in chans:
        raise KeyError(f"trigger channel {trigger_channel!r} not in .acq "
                       f"(channels: {list(chans.keys())})")
    ch = chans[name]
    return sync.detect_pulses(ch["samples"], ch["fs"], threshold,
                              min_interval_s, polarity)


# ── build & inject the BIOPAC pseudo-stream ──────────────────────────────────

def build_biopac_stream(physio: Dict[str, dict], slope: float, intercept: float,
                        name: str = "BIOPAC") -> dict:
    """Resample all physiology channels onto a common grid (the highest channel
    rate) over their shared duration, map that grid from acq -> LSL time, and
    package as one pyxdf-shaped pseudo-stream. Resampling to a common grid keeps
    the uniform stream model; analyze_xdf resamples again to the wearable rate."""
    if not physio:
        raise ValueError("no physiology channels to build a BIOPAC stream")
    target_fs = max(c["fs"] for c in physio.values())
    dur = min(len(c["samples"]) / c["fs"] for c in physio.values())
    grid_acq = np.arange(0.0, dur, 1.0 / target_fs)
    labels, cols = [], []
    for chname, c in physio.items():
        t_ch = np.arange(len(c["samples"])) / c["fs"]
        cols.append(np.interp(grid_acq, t_ch, c["samples"]))
        labels.append(chname)
    data = np.column_stack(cols)
    ts_lsl = sync.map_times(grid_acq, slope, intercept)
    return make_pseudo_stream(name, ts_lsl, data, labels, target_fs)


def inject_biopac_from_acq(streams: Dict[str, dict], xdf_marker_times: np.ndarray,
                           acq_path: str, trigger_channel, stream_name: str = "BIOPAC",
                           threshold: float = None, min_interval_s: float = 0.5,
                           polarity: str = "rising", keep_channels=None) -> dict:
    """Read the .acq, align it to the XDF marker times via the shared TTL, add a
    'BIOPAC' pseudo-stream to `streams`, and return the alignment diagnostics.
    keep_channels: if given, only these .acq channels are kept (the ones the
    channel_map actually pairs), so unrelated channels don't bloat the stream."""
    chans = read_acq(acq_path)
    pulses = trigger_times(chans, trigger_channel, threshold, min_interval_s, polarity)
    return _align_and_inject(streams, xdf_marker_times, chans, pulses,
                             trigger_channel, stream_name, keep_channels)


def _align_and_inject(streams, xdf_marker_times, chans, pulses, trigger_channel,
                      stream_name, keep_channels=None) -> dict:
    """Shared core (separated from file I/O so tests can pass synthetic chans)."""
    a, b = sync.match_pulses(np.asarray(xdf_marker_times, dtype=float), pulses)
    slope, intercept, max_resid, n = sync.fit_clock_map(a, b)
    trig_name = (list(chans.keys())[trigger_channel]
                 if isinstance(trigger_channel, int) else trigger_channel)
    physio = {k: v for k, v in chans.items() if k != trig_name}
    if keep_channels:
        keep = set(keep_channels)
        filtered = {k: v for k, v in physio.items() if k in keep}
        if filtered:  # only narrow when at least one requested channel exists
            physio = filtered
    streams[stream_name] = build_biopac_stream(physio, slope, intercept, stream_name)
    return {"slope": slope, "intercept": intercept, "max_resid_s": max_resid,
            "n_pulses": n, "channels": list(physio.keys())}
