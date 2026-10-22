"""
inspect_streams.py — LSL stream inventory for the BiHome validation work.

Sprint 0 tool. Run this with EVERYTHING streaming (BiHome wearables AND the
BIOPAC LSL outlet) so we can capture the *real* metadata of every stream on
the network: name, type, channel count, nominal rate, per-channel labels and
units, plus a short live sample to sanity-check scale/units and the effective
(measured) sampling rate.

The output drives the channel-pairing config (validation/channel_map.json):
we only design BIOPAC <-> wearable pairings once we have seen the actual
BIOPAC stream identity and units.

This tool is READ-ONLY: it resolves and briefly subscribes to streams, it
never records or writes to %APPDATA%/BiHome. It writes a JSON inventory next
to itself for later reference.

Usage (from the repo root, using the venv interpreter):
    .venv\\Scripts\\python.exe validation\\inspect_streams.py
    .venv\\Scripts\\python.exe validation\\inspect_streams.py --resolve 5 --sample 4
"""

import argparse
import json
import os
import sys
import time

import numpy as np

try:
    from pylsl import resolve_streams, StreamInlet, proc_clocksync
except Exception as e:  # pragma: no cover - import guard
    print(f"ERROR: pylsl not available: {e}", file=sys.stderr)
    sys.exit(1)


# BiHome's own streams are prefixed P0N_ (e.g. P01_PolarECG). Anything that is
# NOT a BiHome stream and NOT the marker stream is a candidate reference
# (BIOPAC) stream worth pairing.
_BIHOME_HINTS = ("PolarECG", "PolarACC", "EmoPPG", "EmoEDA", "EmoTemp",
                 "Emo", "Polar", "_Marker", "Battery")


def _looks_like_bihome(name: str) -> bool:
    if name[:1] == "P" and "_" in name[:5]:
        return True
    return any(h in name for h in _BIHOME_HINTS)


def _parse_channels(info) -> list:
    """Extract per-channel (label, unit, type) from the stream's XML desc.
    Returns [] when the producer did not advertise channel metadata."""
    channels = []
    try:
        desc = info.desc()
        if desc.empty():
            return channels
        chans = desc.child("channels")
        if chans.empty():
            return channels
        ch = chans.child("channel")
        while not ch.empty():
            channels.append({
                "label": ch.child_value("label"),
                "unit": ch.child_value("unit"),
                "type": ch.child_value("type"),
            })
            ch = ch.next_sibling()
    except Exception:
        pass
    return channels


def _sample_stream(info, seconds: float) -> dict:
    """Briefly subscribe and report effective rate + per-channel value ranges.
    Helps verify units/scale (e.g. Polar ECG in uV vs BIOPAC ECG in mV)."""
    out = {"measured_srate": None, "n_samples": 0, "per_channel": None,
           "error": None}
    if seconds <= 0:
        return out
    try:
        inlet = StreamInlet(info, max_buflen=60, processing_flags=proc_clocksync)
        nch = info.channel_count()
        buf = []
        t_end = time.time() + seconds
        # pull_chunk in a tight loop for the sampling window
        while time.time() < t_end:
            chunk, ts = inlet.pull_chunk(timeout=0.2, max_samples=1024)
            if chunk:
                buf.extend(chunk)
        inlet.close_stream()
        n = len(buf)
        out["n_samples"] = n
        if n > 0:
            out["measured_srate"] = round(n / seconds, 2)
            try:
                arr = np.asarray(buf, dtype=float)
                if arr.ndim == 1:
                    arr = arr.reshape(-1, 1)
                out["per_channel"] = [
                    {
                        "min": float(np.nanmin(arr[:, c])),
                        "max": float(np.nanmax(arr[:, c])),
                        "mean": float(np.nanmean(arr[:, c])),
                    }
                    for c in range(min(nch, arr.shape[1]))
                ]
            except Exception as e:
                out["error"] = f"stats failed: {type(e).__name__}: {e}"
    except Exception as e:
        out["error"] = f"{type(e).__name__}: {e}"
    return out


def main():
    ap = argparse.ArgumentParser(description="Inventory all LSL streams on the network.")
    ap.add_argument("--resolve", type=float, default=4.0,
                    help="seconds to wait while resolving streams (default 4)")
    ap.add_argument("--sample", type=float, default=3.0,
                    help="seconds to sample each stream for rate/range (0 to skip)")
    ap.add_argument("--out", default=None,
                    help="JSON output path (default: validation/stream_inventory.json)")
    args = ap.parse_args()

    print(f"Resolving LSL streams ({args.resolve:.0f}s)...")
    infos = resolve_streams(wait_time=args.resolve)
    if not infos:
        print("\nNo LSL streams found.")
        print("Checklist: BiHome running? BIOPAC LSL outlet started? Same subnet/firewall open?")
        return

    print(f"Found {len(infos)} stream(s).\n")
    inventory = []
    for info in infos:
        name = info.name()
        entry = {
            "name": name,
            "type": info.type(),
            "channel_count": info.channel_count(),
            "nominal_srate": info.nominal_srate(),
            "channel_format": info.channel_format(),
            "source_id": info.source_id(),
            "hostname": info.hostname(),
            "uid": info.uid(),
            "is_bihome": _looks_like_bihome(name),
            "channels": _parse_channels(info),
        }
        entry["live"] = _sample_stream(info, args.sample)
        inventory.append(entry)

        tag = "BiHome" if entry["is_bihome"] else "*** CANDIDATE REFERENCE (BIOPAC?) ***"
        print(f"-- {name}  [{tag}]")
        print(f"     type={entry['type']!r}  channels={entry['channel_count']}  "
              f"nominal_srate={entry['nominal_srate']}  fmt={entry['channel_format']}")
        print(f"     source_id={entry['source_id']!r}  host={entry['hostname']!r}")
        if entry["channels"]:
            for i, ch in enumerate(entry["channels"]):
                print(f"       ch{i}: label={ch['label']!r} unit={ch['unit']!r} type={ch['type']!r}")
        else:
            print("       (no per-channel metadata advertised)")
        live = entry["live"]
        if live["error"]:
            print(f"     live: ERROR {live['error']}")
        elif live["n_samples"]:
            print(f"     live: {live['n_samples']} samples, measured_srate~{live['measured_srate']} Hz")
            if live["per_channel"]:
                for i, st in enumerate(live["per_channel"]):
                    print(f"       ch{i}: min={st['min']:.4g} max={st['max']:.4g} mean={st['mean']:.4g}")
        else:
            print("     live: no samples received in sampling window")
        print()

    out_path = args.out or os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                         "stream_inventory.json")
    try:
        with open(out_path, "w", encoding="utf-8") as f:
            json.dump(inventory, f, indent=2)
        print(f"Inventory written to: {out_path}")
    except Exception as e:
        print(f"Could not write inventory JSON: {e}", file=sys.stderr)

    bihome = [e for e in inventory if e["is_bihome"]]
    refs = [e for e in inventory if not e["is_bihome"]]
    print(f"\nSummary: {len(bihome)} BiHome stream(s), {len(refs)} candidate reference stream(s).")
    if not refs:
        print("No non-BiHome stream detected → the BIOPAC LSL outlet is not visible yet.")


if __name__ == "__main__":
    main()
