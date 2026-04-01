"""Diagnostic: read raw serial from Arduino Polar bridge for 30 seconds.
Counts samples, measures timestamp gaps, identifies where data loss occurs.
No LSL, no imputer, no viewer — just raw serial analysis."""

import serial
import time
import sys
import re

PORT = "COM5"
BAUD = 921600
DURATION = 30  # seconds

ECG_RATE = 130  # Hz
EXPECTED_PERIOD_US = 1_000_000 / ECG_RATE  # ~7692 us

def main():
    print(f"Opening {PORT} @ {BAUD}...")
    ser = serial.Serial(port=PORT, baudrate=BAUD, timeout=0.0)
    try:
        ser.set_buffer_size(rx_size=131072, tx_size=16384)
    except Exception:
        pass

    # drain any stale data
    time.sleep(0.1)
    ser.reset_input_buffer()

    print(f"Collecting data for {DURATION}s...")
    t0 = time.time()
    buf = ""

    sens_count = 0
    other_count = 0
    timestamps_us = []  # list of reconstructed us64 timestamps
    seq_numbers = []    # sequence numbers from firmware
    ecg_nan_count = 0
    parse_errors = 0

    while (time.time() - t0) < DURATION:
        n = ser.in_waiting
        if n <= 0:
            time.sleep(0.0002)
            continue
        chunk = ser.read(min(n, 65536))
        if not chunk:
            continue
        buf += chunk.decode("utf-8", errors="replace")
        parts = buf.split("\n")
        buf = parts[-1]

        for line in parts[:-1]:
            line = line.strip()
            if not line:
                continue

            if line.startswith("Sens:"):
                sens_count += 1
                # parse wrap and us32 for timestamp reconstruction
                try:
                    fields = {}
                    for kv in line[5:].split(","):
                        k, v = kv.split(":", 1)
                        fields[k] = v

                    wrap = int(fields["wrap"])
                    us32 = int(fields["us32"])
                    us64 = (wrap << 32) | us32
                    timestamps_us.append(us64)

                    if "seq" in fields:
                        seq_numbers.append(int(fields["seq"]))

                    if fields.get("ecg", "") == "nan":
                        ecg_nan_count += 1
                except Exception:
                    parse_errors += 1
            else:
                other_count += 1
                # print non-Sens lines for debugging
                if any(line.startswith(p) for p in ("INFO:", "WARN:", "ERR:", "HELLO:")):
                    elapsed = time.time() - t0
                    print(f"  [{elapsed:.1f}s] {line}")

    elapsed = time.time() - t0
    ser.close()

    print(f"\n{'='*60}")
    print(f"RESULTS ({elapsed:.1f}s collection)")
    print(f"{'='*60}")
    print(f"Sens lines received:   {sens_count}")
    print(f"Expected @ {ECG_RATE}Hz:      {int(ECG_RATE * elapsed)}")
    print(f"Loss rate:             {100*(1 - sens_count/(ECG_RATE*elapsed)):.1f}%")
    print(f"ECG=nan samples:       {ecg_nan_count} ({100*ecg_nan_count/max(sens_count,1):.1f}%)")
    print(f"Parse errors:          {parse_errors}")
    print(f"Other lines:           {other_count}")

    # Sequence number analysis
    if seq_numbers:
        seq_gaps = 0
        seq_missing_total = 0
        for i in range(1, len(seq_numbers)):
            diff = seq_numbers[i] - seq_numbers[i-1]
            if diff != 1:
                seq_gaps += 1
                seq_missing_total += diff - 1
        print(f"\nSEQUENCE NUMBER ANALYSIS:")
        print(f"  First seq: {seq_numbers[0]}, Last seq: {seq_numbers[-1]}")
        print(f"  Seq gaps:            {seq_gaps}")
        print(f"  Seq missing total:   {seq_missing_total}")
        print(f"  -> Arduino emitted {seq_numbers[-1] - seq_numbers[0] + 1} samples, we received {len(seq_numbers)}")
        print(f"  -> Serial loss:      {seq_missing_total} ({100*seq_missing_total/max(seq_numbers[-1]-seq_numbers[0]+1,1):.1f}%)")

    if len(timestamps_us) < 2:
        print("Not enough timestamps for gap analysis.")
        return

    # Gap analysis
    ts = timestamps_us
    diffs_us = [ts[i+1] - ts[i] for i in range(len(ts)-1)]

    normal_min = EXPECTED_PERIOD_US * 0.5
    normal_max = EXPECTED_PERIOD_US * 1.5

    gaps = [(i, d) for i, d in enumerate(diffs_us) if d > normal_max]
    negative = [(i, d) for i, d in enumerate(diffs_us) if d < 0]

    avg_diff = sum(diffs_us) / len(diffs_us)

    print(f"\nTIMESTAMP ANALYSIS:")
    print(f"  Avg interval:        {avg_diff:.0f} us (expected {EXPECTED_PERIOD_US:.0f})")
    print(f"  Total gaps (>1.5x):  {len(gaps)}")
    print(f"  Negative jumps:      {len(negative)}")

    if gaps:
        # Estimate total missing samples from gaps
        total_missing = 0
        print(f"\n  Top 20 gaps:")
        sorted_gaps = sorted(gaps, key=lambda x: -x[1])
        for i, (idx, d) in enumerate(sorted_gaps[:20]):
            missing = round(d / EXPECTED_PERIOD_US) - 1
            total_missing += missing
            t_sec = (ts[idx] - ts[0]) / 1_000_000
            print(f"    [{t_sec:.2f}s] gap={d/1000:.1f}ms -> ~{missing} missing samples")

        if len(sorted_gaps) > 20:
            for _, d in sorted_gaps[20:]:
                total_missing += round(d / EXPECTED_PERIOD_US) - 1

        print(f"\n  TOTAL estimated missing: {total_missing} samples "
              f"({100*total_missing/(sens_count+total_missing):.1f}% of expected)")

    # Check for consistent duplicate timestamps (interleaved data)
    zero_gaps = sum(1 for d in diffs_us if d == 0)
    micro_gaps = sum(1 for d in diffs_us if 0 < d < normal_min)
    print(f"\n  Zero-diff pairs:     {zero_gaps}")
    print(f"  Micro-gaps (<{normal_min:.0f}us): {micro_gaps}")

if __name__ == "__main__":
    main()
