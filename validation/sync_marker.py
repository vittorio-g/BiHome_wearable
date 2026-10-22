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


def _make_trigger_outlet(name: str, rate: float):
    # Continuous regular-rate 0/1 digital channel: visible as a square wave in
    # the viewer (0 at rest, 1 during a pulse) and edge-detectable offline,
    # symmetric with the BIOPAC .acq TTL channel.
    info = pylsl.StreamInfo(name, "Digital", 1, rate, pylsl.cf_int32, "bihome_ttl_sync")
    info.desc().append_child("channels").append_child("channel") \
        .append_child_value("label", "TTL")
    return pylsl.StreamOutlet(info)


def main():
    ap = argparse.ArgumentParser(description="Fire synchronized TTL + LSL marker sync events.")
    ap.add_argument("--port", default=None, help="USB-TTL COM port (e.g. COM3). Omit for marker-only.")
    ap.add_argument("--baud", type=int, default=115200)
    ap.add_argument("--value", default="01", help="trigger value: int 0-255 or hex (e.g. 01, FF)")
    ap.add_argument("--width-ms", type=float, default=50.0, help="TTL pulse width")
    ap.add_argument("--pulses", type=int, default=3,
                    help="number of sync pulses to fire (>=3 so the clock-fit residual "
                         "is meaningful; 0 = run forever until stopped)")
    ap.add_argument("--interval", type=float, default=5.0, help="seconds between pulses")
    ap.add_argument("--rate", type=float, default=100.0, help="continuous stream sample rate (Hz)")
    ap.add_argument("--marker-name", default="BiHomeSync", help="LSL trigger stream name")
    ap.add_argument("--no-ttl", action="store_true", help="marker-only (no serial hardware)")
    ap.add_argument("--no-init", action="store_true", help='skip the "RR" init on the TTL module')
    args = ap.parse_args()

    value = ttl_trigger.parse_value(args.value)
    width_s = max(0.0, args.width_ms) / 1000.0

    outlet = _make_trigger_outlet(args.marker_name, args.rate)
    print(f"LSL continuous trigger '{args.marker_name}' @ {args.rate:.0f} Hz up. "
          f"TTL value={value} (0x{value:02X}).")
    # Wait for a consumer so the first pulses are not emitted into the void. The
    # BiHome viewer records only streams it already knows at REC time, so this
    # stream must be up (and ideally subscribed) before REC.
    if outlet.wait_for_consumers(8.0):
        print("  consumer attached (a recorder is subscribed).")
    else:
        print("  WARNING: no LSL consumer yet — make sure BiHome shows BiHomeSync and "
              "you press REC; it records only streams known at REC.", file=sys.stderr)
    time.sleep(1.0)

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

    # Continuous push: 0 at rest, 1 during each pulse window. The TTL line is
    # driven HIGH/LOW on the same transitions, so the LSL square wave and the
    # BIOPAC TTL are the same signal.
    period = 1.0 / args.rate
    t0 = pylsl.local_clock() + 1.0
    forever = args.pulses <= 0
    last_k = None if forever else args.pulses - 1
    end_t = None if forever else t0 + last_k * args.interval + width_s + 0.5
    next_t = pylsl.local_clock()
    prev = 0
    fired = 0
    if forever:
        print(f"Running until stopped (Ctrl-C): a pulse every {args.interval:.0f}s.")
    try:
        while end_t is None or pylsl.local_clock() < end_t:
            now = pylsl.local_clock()
            if now >= next_t:
                val = 0
                if now >= t0:
                    k = int((now - t0) // args.interval)   # current pulse index
                    if forever or k <= last_k:
                        ps = t0 + k * args.interval
                        if ps <= now < ps + width_s:
                            val = 1
                outlet.push_sample([val], timestamp=now)
                if val != prev:
                    if ttl is not None:
                        try:
                            ttl.send_value(value) if val else ttl.reset()
                        except Exception as e:
                            print(f"  !! TTL write failed ({type(e).__name__}: {e}) — "
                                  f"continuing MARKER-ONLY so BiHomeSync keeps recording.",
                                  file=sys.stderr)
                            try:
                                ttl.close()
                            except Exception:
                                pass
                            ttl = None
                    if val:
                        fired += 1
                        tag = "" if forever else f"/{args.pulses}"
                        print(f"  pulse {fired}{tag}  lsl_t={now:.3f}"
                              f"{'  [marker-only]' if ttl is None else ''}")
                    prev = val
                next_t += period
                if next_t < now:           # fell behind: resync
                    next_t = now + period
            time.sleep(period / 4.0)
    except KeyboardInterrupt:
        print("\ninterrupted")
    finally:
        if ttl is not None:
            ttl.close()
            print("TTL port reset + closed.")
    print("Done. The continuous 0/1 trigger stream and the TTL pulses are aligned; "
          "make sure BiHome recorded the stream and the .acq captured the pulses.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
