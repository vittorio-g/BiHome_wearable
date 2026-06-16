"""
make_test_recording.py — produce a real .xdf recording from the synthetic mock
streams, with NO GUI and NO hardware, so the offline analysis pipeline can be
developed and verified end-to-end.

It (1) starts the mock wearable + BIOPAC LSL outlets in a background thread,
(2) launches the bundled LabRecorderCLI.exe to record all of them, (3) records
for --duration seconds, then (4) stops the recorder gracefully (CTRL_BREAK, so
the XDF footer is written) and the mock outlets.

Usage:
    .venv\\Scripts\\python.exe validation\\make_test_recording.py --duration 30
"""

import argparse
import os
import signal
import subprocess
import sys
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import mock_streams  # noqa: E402

_HERE = os.path.dirname(os.path.abspath(__file__))
_REPO = os.path.dirname(_HERE)
LABRECORDER_CLI = os.path.join(_REPO, "LabRecorder", "LabRecorderCLI.exe")

# All mock stream names (must match mock_streams._make_streams)
STREAM_NAMES = ["P01_PolarECG", "P01_EmoEDA", "P01_EmoPPG", "P01_EmoTemp", "BIOPAC"]


def main():
    ap = argparse.ArgumentParser(description="Record mock streams to an XDF (no GUI).")
    ap.add_argument("--duration", type=float, default=30.0, help="recording seconds")
    ap.add_argument("--out", default=None, help="output .xdf path")
    ap.add_argument("--seed", type=int, default=0)
    args = ap.parse_args()

    if not os.path.isfile(LABRECORDER_CLI):
        print(f"ERROR: LabRecorderCLI not found at {LABRECORDER_CLI}", file=sys.stderr)
        return 1

    out_dir = os.path.join(_HERE, "test_recordings")
    os.makedirs(out_dir, exist_ok=True)
    out_path = args.out or os.path.join(out_dir, f"mock_{time.strftime('%Y%m%d_%H%M%S')}.xdf")

    # Stream a bit longer than the recording so the recorder never starves at
    # the edges (record window sits inside the streaming window).
    stop_event = threading.Event()
    stream_dur = args.duration + 4.0
    t = threading.Thread(target=mock_streams.run_outlets,
                         kwargs=dict(duration=stream_dur, stop_event=stop_event,
                                     seed=args.seed, verbose=True),
                         daemon=True)
    t.start()
    time.sleep(2.0)  # let outlets come up + the 1s discover wait inside run_outlets

    preds = [f"name='{n}'" for n in STREAM_NAMES]
    cmd = [LABRECORDER_CLI, out_path] + preds
    print(f"[REC] {' '.join(cmd)}")
    proc = subprocess.Popen(cmd, creationflags=subprocess.CREATE_NEW_PROCESS_GROUP,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE)

    print(f"Recording {args.duration:.0f}s ...")
    time.sleep(args.duration)

    # Graceful stop so the XDF footer is written (same as the viewer does).
    try:
        proc.send_signal(signal.CTRL_BREAK_EVENT)
        try:
            proc.wait(timeout=8)
        except subprocess.TimeoutExpired:
            proc.kill()
    except Exception as e:
        print(f"stop error: {e}")
    stop_event.set()
    t.join(timeout=5)

    if os.path.isfile(out_path) and os.path.getsize(out_path) > 0:
        print(f"\nOK: wrote {out_path} ({os.path.getsize(out_path)} bytes)")
        return 0
    print("\nERROR: XDF not written or empty.", file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())
