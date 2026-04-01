"""Diagnostic: connect directly to Polar H10 via PC Bluetooth (bleak).
Measures BLE notification delivery rate — no Arduino in the loop."""

import asyncio
import struct
import time
from bleak import BleakClient

POLAR_ADDR = "24:AC:AC:04:96:A3"
PMD_CONTROL = "FB005C81-02E7-F387-1CAD-8ACD2D8DF0C8"
PMD_DATA    = "FB005C82-02E7-F387-1CAD-8ACD2D8DF0C8"
HR_MEASUREMENT = "00002a37-0000-1000-8000-00805f9b34fb"  # Heart Rate Measurement

# ECG start: 130 Hz, 14-bit resolution
ENABLE_ECG = bytes([0x02, 0x00, 0x00, 0x01, 0x82, 0x00, 0x01, 0x01, 0x0E, 0x00])
# ACC start: 50 Hz, 16-bit, 8G
ENABLE_ACC = bytes([0x02, 0x02, 0x00, 0x01, 0x32, 0x00, 0x01, 0x01, 0x10, 0x00, 0x02, 0x01, 0x08, 0x00])

DURATION = 30  # seconds
ECG_RATE = 130

ecg_samples = 0
ecg_notifications = 0
acc_notifications = 0
timestamps_ns = []  # polar sensor timestamps
first_data_at = None

def handle_pmd(sender, data: bytearray):
    global ecg_samples, ecg_notifications, acc_notifications, first_data_at

    if len(data) < 10:
        return

    meas_type = data[0]

    if meas_type == 0x00:  # ECG
        ecg_notifications += 1
        if first_data_at is None:
            first_data_at = time.time()

        # bytes 1-8: timestamp (ns, little-endian)
        ts_ns = struct.unpack_from('<Q', data, 1)[0]
        timestamps_ns.append(ts_ns)

        # ECG payload starts at byte 10, 3 bytes per sample (24-bit signed)
        payload = data[10:]
        n_samp = len(payload) // 3
        ecg_samples += n_samp

    elif meas_type == 0x02:  # ACC
        acc_notifications += 1


async def main():
    global first_data_at

    disconn_reason = [None]

    def on_disconnect(client):
        disconn_reason[0] = "callback"
        print("  [!] Disconnect callback fired")

    print(f"Connecting to Polar H10 at {POLAR_ADDR}...")
    async with BleakClient(POLAR_ADDR, disconnected_callback=on_disconnect) as client:
        mtu = getattr(client, 'mtu_size', None)
        print(f"Connected! MTU={mtu}")

        # Request larger MTU if possible
        if hasattr(client, '_backend') and hasattr(client._backend, '_session'):
            try:
                req_mtu = await client._backend._session.request_mtu_async(512)
                print(f"MTU after request: {req_mtu}")
            except Exception as e:
                print(f"MTU request failed (non-critical): {e}")

        # Subscribe to HR measurement — keeps BLE connection alive
        try:
            await client.start_notify(HR_MEASUREMENT, lambda s, d: None)
            print("Subscribed to HR")
        except Exception as e:
            print(f"HR subscribe failed: {e}")

        # Subscribe to PMD control indications (Polar expects us to read responses)
        try:
            await client.start_notify(PMD_CONTROL, lambda s, d: print(f"  [PMD_CTRL] {d.hex()}"))
            print("Subscribed to PMD control indications")
        except Exception as e:
            print(f"PMD control subscribe failed: {e}")

        # Subscribe to PMD data notifications
        await client.start_notify(PMD_DATA, handle_pmd)
        print("Subscribed to PMD data")

        await asyncio.sleep(0.5)

        # Start ECG stream
        await client.write_gatt_char(PMD_CONTROL, ENABLE_ECG, response=True)
        print("ECG start sent")

        await asyncio.sleep(1.0)

        # Start ACC stream
        try:
            await client.write_gatt_char(PMD_CONTROL, ENABLE_ACC, response=True)
            print("ACC start sent")
        except Exception as e:
            print(f"ACC start failed (non-critical): {e}")

        print(f"Collecting data for {DURATION}s...")
        # Poll connection status instead of blind sleep
        for _ in range(DURATION * 10):
            if not client.is_connected:
                print("  [!] Disconnected from Polar!")
                break
            await asyncio.sleep(0.1)

        if client.is_connected:
            try:
                await client.stop_notify(PMD_DATA)
            except Exception:
                pass

    elapsed = time.time() - first_data_at if first_data_at else DURATION
    expected = int(ECG_RATE * elapsed)

    print(f"\n{'='*60}")
    print(f"RESULTS ({elapsed:.1f}s of ECG data)")
    print(f"{'='*60}")
    print(f"ECG samples received:  {ecg_samples}")
    print(f"Expected @ {ECG_RATE}Hz:      {expected}")
    print(f"Loss rate:             {100*(1 - ecg_samples/max(expected,1)):.1f}%")
    print(f"ECG notifications:     {ecg_notifications}")
    print(f"ACC notifications:     {acc_notifications}")
    if ecg_notifications:
        print(f"Avg samples/notif:     {ecg_samples/ecg_notifications:.1f}")

    # Timestamp gap analysis
    if len(timestamps_ns) >= 2:
        diffs_ms = [(timestamps_ns[i+1] - timestamps_ns[i]) / 1e6
                    for i in range(len(timestamps_ns)-1)]
        # Each notification covers ~53ms of data (7 samples at 130Hz)
        normal_max = 80  # ms - allow some jitter
        gaps = [(i, d) for i, d in enumerate(diffs_ms) if d > normal_max]

        print(f"\nNOTIFICATION TIMING (Polar sensor timestamps):")
        print(f"  Avg interval:        {sum(diffs_ms)/len(diffs_ms):.1f} ms")
        print(f"  Min interval:        {min(diffs_ms):.1f} ms")
        print(f"  Max interval:        {max(diffs_ms):.1f} ms")
        print(f"  Gaps (>80ms):        {len(gaps)}")

        if gaps:
            print(f"\n  Top 10 gaps:")
            sorted_gaps = sorted(gaps, key=lambda x: -x[1])
            for _, d in sorted_gaps[:10]:
                print(f"    {d:.1f} ms")


asyncio.run(main())
