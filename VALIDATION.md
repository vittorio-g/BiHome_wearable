# BiHome Wearable — validation pipeline

Validate the BiHome wearables (Polar H10, EmotiBit) against a **BIOPAC** gold
standard by recording both into one synchronized XDF and computing agreement
metrics per signal.

Everything for this lives in **`validation/`** and is kept separate from the
acquisition app so its (heavier) analysis dependencies never enter the
PyInstaller build.

---

## 1. How it fits together

```
  BIOPAC (AcqKnowledge) ──LSL──┐
                               ├──> one XDF (shared LSL clock) ──> analyze_xdf.py ──> report.html
  BiHome wearables ──LSL───────┘         (LabRecorder)                                + metrics.csv/json + figures
```

- The BIOPAC must publish an **LSL outlet** on the network. BiHome's viewer
  already records it (see §3) — no acquisition-app change is needed.
- All streams share the LSL clock, so the XDF gives a common, alignable
  timeline. Analysis resamples each channel pair to a common grid, removes a
  constant timing offset, and computes the agreement panel.

> **BIOPAC → LSL dependency.** AcqKnowledge streams over its Network Data
> Transfer (TCP), which is *not* LSL out of the box. You need a BIOPAC LSL
> outlet (a vendor/community LSL app, or a small NDT→LSL bridge). Confirm one is
> visible with `inspect_streams.py` (§2) before relying on live capture.

---

## 2. Install

```powershell
cd C:\Users\vitto\Downloads\BiHome_wearable
.\.venv\Scripts\python.exe -m pip install -r validation\requirements-validation.txt
```

(pylsl, numpy, scipy, pandas, matplotlib, pyxdf, neurokit2.)

---

## 3. Recording a real validation session

1. Start the **BIOPAC LSL outlet** and the BiHome app; let both stream.
2. Capture the live stream inventory to learn the BIOPAC stream's real name,
   channels and units:
   ```powershell
   .\.venv\Scripts\python.exe validation\inspect_streams.py --resolve 5 --sample 5
   ```
   Non-BiHome streams are flagged as *candidate reference (BIOPAC?)*.
3. Edit **`validation/channel_map.json`** so each pair points at the real
   wearable and BIOPAC stream/channel names and units (see §5).
4. In the BiHome viewer, the BIOPAC stream appears under the **"_other"** group
   with its own **REC** checkbox (the viewer resolves *all* LSL streams, not
   only `P0N_…`). Leave it checked, press **REC**, run the protocol, **STOP**.
   The XDF lands in `%APPDATA%\BiHome\recordings\`.
5. Analyse it (§6).

---

## 4. Developing/Testing without hardware (mocks)

```powershell
# stream synthetic wearable + BIOPAC outlets (Ctrl-C to stop)
.\.venv\Scripts\python.exe validation\mock_streams.py

# or record a ready-made test XDF (no GUI), then analyse it
.\.venv\Scripts\python.exe validation\make_test_recording.py --duration 60
.\.venv\Scripts\python.exe validation\analyze_xdf.py validation\test_recordings\<file>.xdf
```

The mock derives wearable and BIOPAC channels from a shared "truth" with a
**known** unit scale (1000× ECG), a **+0.1 °C** temperature bias, a **40 ms**
reference lag and measurement noise — so the metrics have a ground truth.
`tests/test_agreement.py` unit-tests the metric functions directly.

---

## 5. channel_map.json

```jsonc
{ "pairs": [
  { "name": "ECG", "kind": "ecg",
    "wearable":  { "stream": "P01_PolarECG", "channel": "ECG", "unit": "uV" },
    "reference": { "stream": "BIOPAC",       "channel": "ECG", "unit": "mV" },
    "scale_reference_to_wearable": 1000.0 }
] }
```

- `channel` is a label (matched against the stream's XML channel labels) or an
  integer index.
- `scale_reference_to_wearable` multiplies the reference so its units match the
  wearable before scale-sensitive metrics (CCC/ICC/Bland-Altman). Use `1.0`
  when units already match.
- `kind` (`ecg`, `eda`, else generic) selects the signal-specific analysis.
- Only pairs whose **both** streams exist in the XDF are analysed; a
  BIOPAC-only channel (e.g. RSP without a wearable counterpart) is simply not
  paired.

---

## 6. Analyse

```powershell
.\.venv\Scripts\python.exe validation\analyze_xdf.py path\to\recording.xdf
# options: --map <file>   --out <dir>   --no-lag-correct
```

Output (in `validation/reports/<recording>/`): `report.html`, `metrics.csv`,
`metrics.json`, and per-pair figures (time series, scatter, Bland-Altman; plus
HR-over-time for ECG and tonic/phasic for EDA).

---

## 7. Reading the metrics

**Generic (every pair)**
- **pearson_r / spearman_r** — linear / rank correlation. Scale- and
  offset-invariant: high `r` with low CCC ⇒ a systematic scale/offset error.
- **ccc** — Lin's Concordance: agreement *and* calibration; penalised by any
  bias or scale mismatch.
- **icc_2_1** — absolute-agreement intraclass correlation (two-way random).
- **bias / loa_lower / loa_upper** — Bland-Altman mean difference and 95% limits
  of agreement (wearable − reference). `prop_bias_slope` ≠ 0 ⇒ bias depends on
  signal level.
- **rmse / mae / mape_pct / nrmse_pct** — error magnitude (NRMSE normalised by
  reference range).
- **coverage** — fraction of usable (non-NaN) sample pairs.
- **lag_s** — estimated wearable-vs-reference timing offset (removed before
  amplitude metrics unless `--no-lag-correct`).

**ECG (`kind: ecg`)**
- **hr_\*** — agreement of the instantaneous heart-rate time series.
- **hrv_{rmssd,sdnn,pnn50,mean_hr}_{wear,ref,absdiff}** — HRV per device + abs
  difference.
- **beat_{sensitivity,ppv,f1,abs_err_ms}** — R-peak detection agreement
  (reference peaks = ground truth) and mean beat-timing error.

**EDA (`kind: eda`)**
- **scl_\*** — tonic (skin-conductance-level) agreement.
- **scr_{sensitivity,ppv,f1,count_wear,count_ref}** — phasic SCR event agreement.

---

## 8. Caveats / open items

- **Units & scale** matter for CCC/ICC/Bland-Altman (not for Pearson). Set
  `scale_reference_to_wearable` from the real units in the inventory.
- **Sampling rates** differ (Polar ECG 130 Hz vs BIOPAC ~1–2 kHz); analysis
  resamples to the wearable rate. Raw high-frequency ECG morphology is therefore
  not the target — derived HR/HRV and R-peak agreement are.
- **Lag**: a constant offset is estimated and removed; the value is reported. A
  *drifting* offset is not corrected (LSL clock sync should keep it small).
- **Live agreement monitor**: `live_monitor.py` (standalone console) prints a
  rolling-window readout per pair — latest values, rolling Pearson r (with an
  in-window timing-offset correction so ECG is meaningful) and bias:
  ```powershell
  .\.venv\Scripts\python.exe validation\live_monitor.py --window 10
  ```
  It is a sanity monitor ("are the devices tracking now?"), not a replacement
  for the offline report. It is intentionally separate from the BiHome viewer
  (no acquisition-app change). An *embedded* monitor inside the viewer is a
  possible future addition.
