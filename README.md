# BiHome Wearable — Multi-Device Physiological Data Acquisition

A multi-device physiological and motion data acquisition system for experimental research. Integrates heterogeneous wearable sensors, synchronizes them with sub-millisecond precision, and publishes real-time data streams via **Lab Streaming Layer (LSL)** for live monitoring and lossless recording in `.xdf` format.

---

## Scientific Purpose

The system is designed for experimental sessions involving:

- **Polar H10 validation**: compare Polar H10 ECG/HR against a reference analog ECG (Arduino MKR + electrodes) to assess reliability before experimental use.
- **Full physiological acquisition**: EmotiBit as primary multimodal sensor (PPG, EDA, temperature, 9-axis IMU), active throughout sessions.
- **UWB position tracking** *(in development)*: subject localization via triangulation with 3 fixed UWB anchors + 1 UWB tag on the subject.

---

## Project Structure

```
BiHome_wearable/
├── BiHome_wearable.py          # Main acquisition engine (~3000 lines)
├── build_exe.py                # PyInstaller packaging script
├── README.md                   # This file
│
├── Viewer/
│   ├── lsl_viewer.py           # Real-time LSL viewer (PyQt5 + pyqtgraph)
│   ├── fonts/                  # Bundled Montserrat TTF font family
│   ├── recordings/             # XDF + CSV output (gitignored)
│   ├── diag/                   # Auto-generated diagnostic CSVs
│   └── viewer_settings.json    # Persistent UI state
│
├── all_sensors/                # Arduino firmware
│   ├── ECG/ECG.ino             # Arduino MKR WiFi — analog ECG → TCP → PC
│   ├── POLAR_2/POLAR_2.ino     # Arduino MKR — BLE bridge Polar H10 → USB Serial (legacy)
│   ├── UWB_Ping/UWB_Ping.ino   # DW3000 Initiator (DS-TWR)
│   └── UWB_Pong/UWB_Pong.ino  # DW3000 Responder (DS-TWR)
│
├── data/
│   ├── import pyxdf.py         # XDF → CSV + JSON metadata exporter
│   ├── Data_Exploration.R      # Exploratory R analysis
│   ├── exp001/                 # Example session recordings
│   ├── csv_export/             # CSV output from pyxdf script
│   └── Comparison_ecg/         # ECG vs Polar validation recordings
│
├── LabRecorder/                # XDF recording engine (binaries gitignored)
│   ├── LabRecorderCLI.exe      # CLI binary called from Python
│   └── LabRecorder.cfg         # Stream selection configuration
│
├── diag_bleak.py               # BLE diagnostics (Polar H10 MAC discovery, data loss test)
├── diag_serial.py              # Serial diagnostics (Arduino data loss measurement)
├── diag_emotibit_scan.py       # UDP broadcast scan for EmotiBit discovery
└── diag_emotibit_brainflow.py  # BrainFlow board initialization test
```

---

## Hardware

| Device | Role | Connection |
|---|---|---|
| **Polar H10** | ECG + accelerometer (BLE PMD) | Direct BLE → PC (`bleak`) — **recommended, ~0% loss** |
| **Arduino MKR WiFi 1010** | Analog ECG (electrodes, 12-bit ADC) | WiFi TCP → PC (IP `192.168.50.174:5000`) |
| **Arduino MKR WiFi 1010** | BLE → USB bridge for Polar H10 *(legacy)* | USB Serial `COM5` @ 921600 baud |
| **EmotiBit** | PPG, EDA, temperature, 9-axis IMU | WiFi/UDP → BrainFlow → PC (auto-discovery) |
| **DW3000 (UWB)** *(paused)* | 2-way ranging for localization | SPI on Arduino |

**Hardware notes:**
- Arduino MKR ECG uses **pin A0** for 12-bit analog acquisition, `VREF = 3.3V`.
- Arduino MKR Polar bridge uses **pin 1** as BLE connection LED indicator.
- EmotiBit requires no manual IP configuration — BrainFlow discovers it via UDP broadcast.
- UWB modules are pending hardware purchase. Planned architecture: 1 mobile tag on subject + 3 fixed anchors for 3D triangulation.

---

## Data Flow

```
Polar H10 ──── BLE (bleak) ──────────────────────────────────────►
Arduino MKR (ECG.ino) ──── WiFi TCP ─────────────────────────────► BiHome_wearable.py ──► LSL Outlets ──► LabRecorder / lsl_viewer
EmotiBit ──── WiFi/UDP ──── BrainFlow ───────────────────────────►

(Legacy) Polar H10 ──BLE──► Arduino MKR (POLAR_2.ino) ──USB Serial──► BiHome_wearable.py
```

---

## LSL Streams Published

| Stream Name | Type | Channels | Sample Rate | Source |
|---|---|---|---|---|
| `ArduinoWiFi_ECG` | BIO | `ecg` | 250 Hz | Arduino MKR WiFi |
| `PolarH10_Sens` | BIO | `ecg`, `ax`, `ay`, `az`, `beat` | 130 Hz | Polar H10 (direct BLE) |
| `EmotiBit_IMU` | BIO | `acc_x/y/z`, `gyro_x/y/z`, `mag_x/y/z` | variable | EmotiBit |
| `EmotiBit_PPG` | BIO | `ppg_0`, `ppg_1`, `ppg_2` | variable | EmotiBit |
| `EmotiBit_EDA_TEMP` | BIO | `eda`, `temp` | variable | EmotiBit |
| `Clock_ArduinoWiFi_ECG` | CLOCK | 10 sync fields | irregular | internal |
| `Clock_ArduinoUSB_Polar` | CLOCK | 10 sync fields | irregular | internal |
| `Clock_EmotiBit` | CLOCK | 4 sync fields | irregular | internal |

The `beat` channel in `PolarH10_Sens` is **1.0** at the sample where an R-peak is confirmed, **0.0** otherwise. It is computed entirely in Python (not from firmware). See [R-Peak Detection](#r-peak-detection-and-beat-channel).

---

## Installation

### Python Dependencies

```bash
pip install pylsl pyserial brainflow numpy bleak pyxdf pyqtgraph PyQt5
```

### Standalone Windows Executable

```bash
pip install pyinstaller
python build_exe.py
# Output: dist/BiHome Wearable/BiHome Wearable.exe
```

The executable bundles all dependencies (pylsl, bleak, brainflow, numpy, pyqtgraph, PyQt5), fonts, icons, and the LabRecorder CLI. No Python installation required on the target machine.

---

## Usage

### 1. Configure feature flags

Edit the top of `BiHome_wearable.py`:

```python
ENABLE_ARDUINO_WIFI_ECG   = False   # Analog ECG over WiFi
ENABLE_BLEAK_POLAR        = True    # Direct BLE to Polar H10 (recommended, ~0% loss)
ENABLE_ARDUINO_USB_POLAR  = False   # Arduino BLE bridge (legacy, ~20% loss)
ENABLE_EMOTIBIT           = False   # EmotiBit multimodal sensor
ENABLE_SIGNAL_FILTER      = True    # Weighted moving average on ECG, ACC, PPG
ENABLE_ECG_IMPUTATION     = False   # Synthetic beat filling during BLE gaps (not needed with direct BLE)
```

Also set the Polar H10 MAC address:

```python
POLAR_BLE_ADDRESS = "24:AC:AC:04:96:A3"  # find yours with: python diag_bleak.py
```

### 2. Start acquisition

```bash
python BiHome_wearable.py
```

### 3. Monitor and record (separate terminal)

```bash
python Viewer/lsl_viewer.py
```

The viewer auto-discovers all active LSL streams. To record:
- Check the **REC** checkbox next to each stream to include
- Enter an optional filename (defaults to current date/time)
- Click **REC** to start, **STOP** to finish
- The `.xdf` file is saved in `Viewer/recordings/` via LabRecorderCLI
- A per-stream CSV is exported automatically at the end of recording

---

## Real-Time Viewer (`Viewer/lsl_viewer.py`)

PyQt5 + pyqtgraph viewer with BiHome branding (Montserrat font, teal/dark palette).

**Features:**
- Auto-discovery of all active LSL streams
- One plot per channel with smooth scrolling (20 FPS)
- Per-channel toggle buttons (colored = active, grey = hidden)
- Drag-and-drop channel reordering
- Auto or manual Y-axis scaling per channel
- **BPM and HRV metrics**: heart rate (30 s window), SDNN, RMSSD — computed from the `beat` channel
- **Beat markers**: red dots positioned on R-peaks via derivative analysis
- Integrated XDF recording (REC/STOP) with automatic per-stream CSV export
- Gap detection: inserts NaN to avoid spurious horizontal lines on packet loss
- Persistent settings: channel visibility, Y-axis ranges, window size

---

## Time Synchronization

Temporal alignment is critical when combining signals from devices with independent clocks.

### Arduino (ECG WiFi + Polar bridge)

Uses a manual **NTP-like protocol**:

1. PC sends `SYNC:<seq>` to the device.
2. Device replies immediately with `T:<seq>,w2,t2_32,w3,t3_32` (Arduino `micros64()` timestamps).
3. PC computes RTT, one-way delay, and clock offset against `pylsl.local_clock()`.
4. Offset is smoothed with an EMA filter (`α = 0.15`).
5. Each sample timestamp is converted: `t_host = t_dev − offset`.

**Polar H10 sensor-level timestamps:** PMD packets embed a 64-bit nanosecond timestamp (bytes 1–8) representing the last sample in each batch. The firmware back-calculates timestamps for all previous samples in the batch. These sensor timestamps are then mapped to the Arduino clock domain via a continuously estimated EMA offset, keeping them compatible with the NTP sync chain toward the PC.

> Using `micros()` at BLE notification arrival would introduce variable latency (~7.5–30 ms per connection interval), desynchronizing Polar from the Arduino ECG (which timestamps each sample at the exact `analogRead()` moment).

### EmotiBit (BrainFlow)

BrainFlow timestamps are correlated to `pylsl.local_clock()` via an EMA offset (`α = 0.05`), updated on every incoming data batch.

### 64-bit Microsecond Timestamps (`micros64`)

Arduino's 32-bit `micros()` wraps every ~71 minutes. The firmware extends it to 64 bits:

```c
uint64_t readMicros64() {
  uint32_t now = micros();
  if (now < last_us32) wrap32++;
  last_us32 = now;
  return (((uint64_t)wrap32) << 32) | (uint64_t)now;
}
```

The `(wrap, us32)` pair is transmitted over serial/TCP and reconstructed on the PC side.

---

## R-Peak Detection and `beat` Channel

The `beat` channel in `PolarH10_Sens` is computed in real time in Python by the `PolarECGImputer` class. It is **1.0** at the confirmed R-peak sample, **0.0** otherwise.

Detection is delayed by `POST_S = 0.40 s`: when `beat=1.0` appears at timestamp `T`, the actual R-peak occurred at approximately `T − 0.40 s`. This delay is required for the post-peak confirmation window.

### Detection Pipeline (per sample)

1. **Local max/min check** (±`LOCAL_WIN = 5` samples, ~38 ms at 130 Hz)
   Candidate must be the absolute maximum (or minimum for inverted ECG) in its narrow local window — not the full PRE+POST window, which at normal heart rates contains two R-peaks.

2. **Sharpness discriminator** (`SHARP_FRAC = 0.50`)
   Signal must change by at least 50% of local amplitude in the `LOCAL_WIN` samples preceding the candidate. R-peaks (upstroke ~20–40 ms) pass; T-waves (upstroke ~80–150 ms) are rejected.

3. **Max-derivative check** (`MAX_DERIV_WIN = 3`)
   The maximum sample-to-sample derivative within ±3 samples of the candidate must exceed 20% of amplitude. R-peaks have 5–10× steeper derivatives than T-waves.

4. **Adaptive amplitude threshold** (`AMP_FRAC = 0.55`)
   Candidate amplitude must reach at least 55% of the EMA (α = 0.2) of previously accepted peaks. Before EMA initialization, a fixed threshold of `MIN_AMP_UV = 60 µV` applies.

5. **Refractory period** (`REFRACT_S = 0.40 s = 52 samples`)
   Detection disabled for 400 ms after each confirmed R-peak, preventing double detections on the downstroke, S-wave, or T-wave.

6. **MIN_RR guard**
   Before confirming a peak: `peak_ts − last_peak_ts ≥ MIN_RR_S = 0.35 s`. Also covers cases where `last_peak_ts` was advanced by gap prediction.

### Gap Prediction

If no R-peak is detected for more than `1.8 × mean_RR` (with at least 2 RR intervals in history), the system predicts a missing beat:

```
_last_peak_ts += mean_RR
is_beat = True
```

This signals "a beat should have occurred here" without modifying the ECG signal.

### Anti-Double Guard

Gap prediction and real detection can fire within a few samples of each other. A second guard in the output suppresses the second event if `current_ts − last_beat_ts < MIN_RR_S`.

### ECG Imputation During BLE Gaps

When BLE drops packets (samples arrive as `NaN`), with `ENABLE_ECG_IMPUTATION = True`:

1. Computes phase within the RR cycle at the gap start.
2. Places beat templates (mean of last `MAX_BEATS = 8` beats, baseline-subtracted) at each predicted beat position within the gap.
3. Returns synthetic samples with correct timestamps and `beat=1.0` on imputed peaks.

```
Real signal:     ____/\____/\____[  NaN gap  ]____/\____
Imputed signal:  ____/\____/\____[/\__synth__]____/\____
```

### Reference Parameters

| Parameter | Value | Meaning |
|---|---|---|
| `FS` | 130 Hz | Sampling frequency |
| `PRE_S` / `POST_S` | 0.25 s / 0.40 s | Window around peak |
| `LOCAL_WIN` | 5 samples (~38 ms) | Half-width for local-max and sharpness checks |
| `MIN_RR_S` / `MAX_RR_S` | 0.35 s / 1.60 s | Physiological RR range (170–37 bpm) |
| `REFRACT_S` | 0.40 s | Post-peak refractory period |
| `AMP_FRAC` | 0.55 | Adaptive threshold (55% of EMA) |
| `MIN_AMP_UV` | 60 µV | Fixed threshold for EMA bootstrap |
| `SHARP_FRAC` | 0.50 | Sharpness threshold (upstroke / amplitude) |
| `MAX_DERIV_WIN` | 3 samples (~23 ms) | Half-width for max-derivative check |
| `MAX_BEATS` | 8 | Beats in imputation template |
| `IMPUTER_MAX_GAP_S` | 4.0 s | Gaps longer than this are not imputed |

---

## Signal Filtering

ECG (WiFi and Polar H10) and PPG (EmotiBit) signals are optionally filtered in Python before being pushed to LSL, using a **causal weighted moving average** (no external dependencies).

### Filter Kernels

```python
ECG_FILTER_WEIGHTS = [1, 2, 3, 2, 1]        # 5-tap triangular (ECG WiFi, Polar ECG)
PPG_FILTER_WEIGHTS = [1, 2, 3, 2, 1]        # 5-tap triangular (EmotiBit PPG)
ACC_FILTER_WEIGHTS = [1, 2, 3, 4, 3, 2, 1]  # 7-tap (more aggressive, for 50 Hz ACC)
```

The triangular kernel assigns maximum weight to the current sample (center tap) and decreasing weights to prior samples, attenuating high frequencies while preserving peak shape.

**Group delay:** ~2 samples (causal).

| Signal | Sample Rate | Delay Introduced |
|---|---|---|
| ECG WiFi | 250 Hz | ~8 ms |
| ECG Polar H10 | ~130 Hz | ~15 ms |
| PPG EmotiBit | ~25 Hz | ~80 ms |

The delay is identical across all samples in the same stream, so **it does not affect temporal alignment between streams** — sample timestamps are unchanged, only the numeric values are filtered.

**Channels filtered:**
- `ArduinoWiFi_ECG`: `ecg` — 5-tap triangular
- `PolarH10_Sens`: `ecg` (5-tap) + `ax`, `ay`, `az` (7-tap)
- `EmotiBit_PPG`: `ppg_0`, `ppg_1`, `ppg_2` — 5-tap
- `EmotiBit_IMU`, `EmotiBit_EDA_TEMP`: not filtered

**Toggle:**
```python
ENABLE_SIGNAL_FILTER = True   # True = filter, False = raw signal
```

**Custom kernels:** any list of positive weights works — `SignalFilter` normalizes automatically.

---

## Direct BLE Connection to Polar H10

The default mode (`ENABLE_BLEAK_POLAR = True`) connects the PC directly to the Polar H10 via Bluetooth, bypassing the Arduino MKR bridge.

### Why not the Arduino bridge?

The NINA-W102 (ESP32) module on the Arduino MKR WiFi 1010 introduces **periodic ~570 ms gaps every ~2.8 s** in BLE notification reception, causing ~20% data loss. Diagnostics confirmed the loss is 100% within the NINA module (zero serial loss: `seq_gaps = 0`), gaps are regular and independent of Python load, and `WiFi.end()` does not help. Direct BLE via `bleak` achieves **~0% data loss**.

### Requirements

- Bluetooth enabled on the PC
- Polar H10 **NOT paired** in Windows — pairing reduces MTU to 23, causing disconnections
- Arduino bridge Polar disconnected (cannot share the BLE connection)

### Configuration

```python
ENABLE_BLEAK_POLAR   = True
POLAR_BLE_ADDRESS    = "24:AC:AC:04:96:A3"  # find yours: python diag_bleak.py
```

Automatic reconnection handles transient BLE disconnections. Sample timestamps use the Polar sensor clock (nanoseconds), continuously calibrated to `pylsl.local_clock()` via EMA.

---

## Serial/TCP Protocol Reference

### Commands (PC → Arduino)

| Command | Response | Description |
|---|---|---|
| `SYNC:<seq>` | `T:<seq>,w2,t2_32,w3,t3_32` | Round-trip for clock offset estimation |
| `led_on` | `ACK:led_on` | Turn on built-in LED |
| `led_off` | `ACK:led_off` | Turn off built-in LED |

### Data Messages (Arduino → PC)

| Label | Format | Description |
|---|---|---|
| `ECG:` | `wrap:<v>,us32:<v>,ecg_mV:<v>` | Arduino WiFi ECG sample |
| `Sens:` | `wrap:<v>,us32:<v>,ecg:<v>,ax:<v>,ay:<v>,az:<v>` | Polar H10 batch (ECG + ACC) |
| `T:` | see above | Sync response |
| `INFO:*` | string | Diagnostic messages |
| `WARN:*` | string | Warnings |
| `ERR:*` | string | Errors |

---

## Post-Session Analysis

```bash
cd data
python "import pyxdf.py"
# Output: data/csv_export/
```

Exports from each `.xdf` recording:
- One **CSV per stream** with `time_stamps` column + one column per channel
- A **JSON metadata file** per stream (name, type, sample rate, LSL info)
- The **global XDF header** as JSON

---

## UWB Localization *(in development)*

Uses **DS-TWR (Double-Sided Two-Way Ranging)** with **DW3000** chips for centimeter-level distance estimation between node pairs.

### Planned Architecture

```
[UWB tag on subject]
        │
        ├──► Anchor A (DW3000 PONG)
        ├──► Anchor B (DW3000 PONG)
        └──► Anchor C (DW3000 PONG)
```

3D position estimated via trilateration from 3 measured distances.

### Current State

- `UWB_Ping.ino`: Initiator firmware (DS-TWR) with 7-point FIR filter (3-sample delay, 7-measurement warmup).
- `UWB_Pong.ino`: Responder firmware (DS-TWR).
- **Pending**: purchase of additional UWB modules.

Planned future LSL streams: `UWB_Ranging` (`dist_A`, `dist_B`, `dist_C` in cm) and `UWB_Position` (`x`, `y`, `z`).

---

## Serial Reader Architecture

The Arduino serial bridge uses a **producer-consumer** design to minimize data loss:

```
[Serial RX 128 KB buffer]
         │
         ▼
  _serial_reader()  ── dedicated thread, serial read only
         │
         ▼
  Queue(maxsize=50000)  ── text lines
         │
         ▼
  run()  ── worker thread: parse, impute, push to LSL
```

- **Producer** reads up to 64 KB per cycle, enqueues lines
- **Consumer** drains up to 500 lines per batch, parses data, runs ECG imputer, pushes to LSL
- **Stats every 10 s**: `lines` read, `pushed` to LSL, `q_depth`, `expected` — useful for diagnosing where loss occurs
- Serial RX buffer: **128 KB** (Windows default is 4 KB)

---

## Troubleshooting

| Problem | Likely Cause | Solution |
|---|---|---|
| No data from Polar | Arduino IDE Serial Monitor open | Close the Serial Monitor (COM port is exclusive) |
| `WARN:NO_ECG_DATA_YET` | Polar H10 slow to respond to ECG command | Expected: firmware retries automatically |
| EmotiBit not found | Device not visible on network | Verify EmotiBit is on the same WiFi network; BrainFlow auto-discovers via UDP broadcast |
| Streams not visible in LabRecorder | Script not running or firewall | Verify the script is running and all devices are on the same network segment |
| UWB: `checkSPI KO` | SPI wiring or reset issue | Check CS/MOSI/MISO/SCK wiring and RSTn on pin D9 |
| Polar disconnects after pairing | Windows reduced MTU to 23 | Unpair the Polar H10 from Windows Bluetooth settings |
