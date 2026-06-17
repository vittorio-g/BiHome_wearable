"""
sync_marker.py — fire synchronized sync events for the two-PC BIOPAC validation.

Run this on the BiHome PC during a recording. Each "fire" does two things at the
same instant:
  1. pushes an LSL **marker** (captured in the BiHome XDF by the viewer/LabRecorder), and
  2. sends a **TTL pulse** over the USB-TTL -> STP100D -> MP160 (recorded in AcqKnowledge .acq).

The same event therefore lands in BOTH recordings, which is exactly what
analyze_xdf.py --acq needs to fit the two PCs' clocks (offset + drift). Fire at
least 2 pulses (e.g. start and end of the session).

Examples:
    # 3 sync pulses, 5 s apart, real TTL on COM3 + LSL marker
    .venv\\Scripts\\python.exe validation\\sync_marker.py --port COM3 --pulses 3 --interval 5

    # marker-only dry run (no serial hardware), to test the LSL side
    .venv\\Scripts\\python.exe validation\\sync_marker.py --no-ttl --pulses 3 --interval 1
"""

import argparse
import os
import sys
import time

import pylsl

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import ttl_trigger  # noqa: E402


def _make_marker_outlet(name: str):
    info = pylsl.StreamInfo(name, "Markers", 1, pylsl.IRREGULAR_RATE,
                            pylsl.cf_int32, "bihome_ttl_sync")
    info.desc().append_child("channels").append_child("channel") \
        .append_child_value("label", "TTLsync")
    return pylsl.StreamOutlet(info)


def main():
    ap = argparse.ArgumentParser(description="Fire synchronized TTL + LSL marker sync events.")
    ap.add_argument("--port", default=None, help="USB-TTL COM port (e.g. COM3). Omit for marker-only.")
    ap.add_argument("--baud", type=int, default=115200)
    ap.add_argument("--value", default="01", help="trigger value: int 0-255 or hex (e.g. 01, FF)")
    ap.add_argument("--width-ms", type=float, default=50.0, help="TTL pulse width")
    ap.add_argument("--pulses", type=int, default=2, help="number of sync pulses to fire")
    ap.add_argument("--interval", type=float, default=5.0, help="seconds between pulses")
    ap.add_argument("--marker-name", default="BiHomeSync", help="LSL marker stream name")
    ap.add_argument("--no-ttl", action="store_true", help="marker-only (no serial hardware)")
    ap.add_argument("--no-init", action="store_true", help='skip the "RR" init on the TTL module')
    args = ap.parse_args()

    value = ttl_trigger.parse_value(args.value)

    outlet = _make_marker_outlet(args.marker_name)
    print(f"LSL marker outlet '{args.marker_name}' (int32) up. Value={value} (0x{value:02X}).")
    time.sleep(1.0)  # let consumers (BiHome viewer / LabRecorder) subscribe

    ttl = None
    if not args.no_ttl:
        if not args.port:
            print("ERROR: --port is required unless --no-ttl is given", file=sys.stderr)
            return 1
        try:
            ttl = ttl_trigger.TtlTrigger.open(args.port, args.baud)
            if not args.no_init:
                ttl.init()
                print(f'TTL module on {args.port} @ {args.baud}: "RR" init sent.')
        except Exception as e:
            print(f"ERROR opening TTL port {args.port}: {type(e).__name__}: {e}", file=sys.stderr)
            return 1

    try:
        for i in range(args.pulses):
            t = pylsl.local_clock()
            outlet.push_sample([value], timestamp=t)   # LSL marker
            if ttl is not None:
                ttl.pulse(value, args.width_ms)         # TTL pulse to BIOPAC
            print(f"  pulse {i + 1}/{args.pulses}  value={value} (0x{value:02X})  "
                  f"lsl_t={t:.3f}{'  [marker-only]' if ttl is None else ''}")
            if i < args.pulses - 1:
                time.sleep(args.interval)
    except KeyboardInterrupt:
        print("\ninterrupted")
    finally:
        if ttl is not None:
            ttl.close()
            print("TTL port reset + closed.")
    print("Done. Make sure the BiHome recording captured the marker stream and "
          "the .acq captured the TTL pulses.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
