#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Multi-device acquisition + time-sync + LSL streaming

FIXES:
- no dropping of repeated ECG/ACC values
- SYNC uses sequence IDs: SYNC:<seq>  ->  T:<seq>,w2,t2_32,w3,t3_32
- Arduino WiFi ECG expected label = ECG
- Polar bridge expected labels = PECG / PACC
- nominal_srate set where known
"""

import os
import sys
import time
import struct
import asyncio
import socket
import select
import subprocess
import threading
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

from pylsl import StreamInfo, StreamOutlet, local_clock

# =====================================================
# Feature flags
# =====================================================

ENABLE_ARDUINO_WIFI_ECG = False
ENABLE_ARDUINO_USB_POLAR = False   # Arduino NINA bridge (legacy, ~20% data loss)
ENABLE_BLEAK_POLAR = True          # Direct PC BLE via bleak (~0% data loss)
ENABLE_EMOTIBIT = False

# Weighted moving average on ECG (WiFi + Polar) and EmotiBit PPG.
# Set to False to stream raw unfiltered values.
ENABLE_SIGNAL_FILTER = True

# R-peak template imputation for Polar H10 ECG gaps (BLE packet loss).
# When enabled, gaps in the Polar ECG stream are filled with a synthetic signal
# built from the average beat template + estimated RR interval.
# Only gaps ≤ IMPUTER_MAX_GAP_S are imputed; longer gaps pass through as NaN.
ENABLE_ECG_IMPUTATION  = False   # Not needed with direct BLE (0% loss)
IMPUTER_MAX_GAP_S      = 4.0   # seconds — skip imputation for longer dropouts

# =====================================================
# USER CONFIG
# =====================================================

# --- Arduino MKR over WiFi/TCP (ECG-only) ---
ARD_WIFI_IP = "192.168.50.174"
ARD_WIFI_PORT = 5000
ARD_WIFI_DATA_LABEL = "ECG"
ARD_WIFI_NCH = 1
ARD_WIFI_SRATE = 250.0

# --- Arduino MKR over USB/Serial (Polar BLE PMD) — legacy ---
POLAR_SERIAL_PORT = "COM5"
POLAR_SERIAL_BAUD = 921600

# --- Direct BLE to Polar H10 via bleak (PC Bluetooth) ---
POLAR_BLE_ADDRESS = "24:AC:AC:04:96:A3"  # default for single-participant mode

POLAR_LABEL_MAP = {
    "Sens": ("PolarH10_Sens", ["ecg","ax","ay","az","beat"], 130.0),
}

# =====================================================
# Known devices registry — friendly name → address/serial
# =====================================================

# Short friendly name → (address/serial, long description)
# Short names are used in LSL stream names and in the viewer UI.
KNOWN_POLAR = {
    "Polar 1":    ("24:AC:AC:04:96:A3", "0496A33F"),
    "Polar 2":    ("24:AC:AC:04:93:ED", "0493ED32"),
}

KNOWN_EMOTIBIT = {
    "EmotiBit 1": ("MD-V6-0000089", ""),
    "EmotiBit 2": ("MD-V6-0000482", ""),
}

# =====================================================
# Multi-participant helpers
# =====================================================

def _safe(s: str) -> str:
    """Make a string safe for LSL identifiers (remove spaces)."""
    return s.replace(" ", "")

def make_polar_label_map(participant_id: str, device_name: str = "Polar") -> dict:
    """Generate Polar label_map with participant + device name in stream names.
    E.g. participant_id='P01', device_name='Polar 1' → 'P01_Polar1'."""
    safe_dev = _safe(device_name)
    prefix = f"{participant_id}_{safe_dev}"
    return {
        "Sens": (prefix, ["ecg", "ax", "ay", "az", "beat"], 130.0),
    }

def make_emotibit_stream_names(participant_id: str, device_name: str = "EmotiBit") -> dict:
    """Generate EmotiBit stream/source names with participant + device prefix.
    E.g. participant_id='P01', device_name='EmotiBit 1' → 'P01_EmotiBit1_IMU' etc."""
    safe_dev = _safe(device_name)
    prefix = f"{participant_id}_{safe_dev}"
    pid = participant_id.lower()
    sid_suffix = f"{pid}_{safe_dev.lower()}"
    return {
        "imu_name": f"{prefix}_IMU",
        "ppg_name": f"{prefix}_PPG",
        "eda_temp_name": f"{prefix}_EDA_TEMP",
        "clock_name": f"Clock_{prefix}",
        "battery_name": f"{prefix}_Battery",
        "imu_sid": f"emotibit_imu_{sid_suffix}",
        "ppg_sid": f"emotibit_ppg_{sid_suffix}",
        "eda_temp_sid": f"emotibit_eda_temp_{sid_suffix}",
        "clock_sid": f"clock_emotibit_{sid_suffix}",
        "battery_sid": f"emotibit_battery_{sid_suffix}",
    }

def make_polar_battery_stream(participant_id: str, device_name: str = "Polar") -> dict:
    safe_dev = _safe(device_name)
    return {
        "name": f"{participant_id}_{safe_dev}_Battery",
        "sid": f"polar_battery_{participant_id.lower()}_{safe_dev.lower()}",
    }

# --- Sync cadence (seconds) ---
SYNC_INTERVAL_S = 2.0

# --- NTP-like offset smoothing ---
SYNC_ALPHA = 0.15

# Outlier rejection thresholds (seconds)
MAX_RTT_S = 0.250
MAX_ABS_DELAY_S = 0.200
MAX_ABS_OFFSET_JUMP_S = 0.100

# --- EmotiBit (BrainFlow) ---
EMOTIBIT_IP = ""  # empty → BrainFlow auto-discovers via UDP broadcast
EMOTIBIT_TIMEOUT = 5
EMOTIBIT_POLL_INTERVAL = 0.05
EMOTIBIT_SERIAL_NUMBER = ""

EMOTIBIT_CLOCK_STREAM = True
EMOTIBIT_CLOCK_ALPHA = 0.02

# =====================================================
# Status / diagnostics prints
# =====================================================

STATUS_HEARTBEAT_S = 5.0
NO_DATA_WARN_S = 5.0
NO_DATA_REPEAT_S = 10.0

def log(tag: str, msg: str) -> None:
    print(f"{time.strftime('%H:%M:%S')} {tag} {msg}", flush=True)

# =====================================================
# Global stop
# =====================================================

stop_event  = threading.Event()
send_lock   = threading.Lock()
ready_event = threading.Event()  # set quando tutti i dispositivi abilitati sono connessi

# =====================================================
# Device/system health
# =====================================================

@dataclass
class DeviceHealth:
    name: str
    enabled: bool
    lock: threading.Lock = field(default_factory=threading.Lock)

    state: str = "INIT"  # INIT, CONNECTING, ACTIVE, ERROR, STOPPED
    detail: str = ""
    connected_at: Optional[float] = None
    last_data_at: Optional[float] = None
    first_data: bool = False
    fatal_error: Optional[str] = None

    def set(
        self,
        *,
        state: Optional[str] = None,
        detail: Optional[str] = None,
        connected_at: Optional[float] = None,
        last_data_at: Optional[float] = None,
        first_data: Optional[bool] = None,
        fatal_error: Optional[str] = None,
    ) -> None:
        with self.lock:
            if state is not None:
                self.state = state
            if detail is not None:
                self.detail = detail
            if connected_at is not None:
                self.connected_at = connected_at
            if last_data_at is not None:
                self.last_data_at = last_data_at
            if first_data is not None:
                self.first_data = first_data
            if fatal_error is not None:
                self.fatal_error = fatal_error

    def snapshot(self) -> Tuple[str, str, Optional[float], Optional[float], bool, Optional[str]]:
        with self.lock:
            return (
                self.state,
                self.detail,
                self.connected_at,
                self.last_data_at,
                self.first_data,
                self.fatal_error,
            )

@dataclass
class MonitoredDevice:
    health: DeviceHealth
    thread: Optional[threading.Thread]


class SystemMonitorThread(threading.Thread):
    def __init__(self, devices: List[MonitoredDevice]):
        super().__init__(daemon=True)
        self.devices = devices
        self._last_sig: Optional[str] = None
        self._last_all_ok: Optional[bool] = None
        self._printed_once = False

    @staticmethod
    def _evaluate(dev: MonitoredDevice) -> Tuple[bool, str, str]:
        h = dev.health

        if not h.enabled:
            return True, "DISABLED", "disabled"

        state, detail, connected_at, last_data_at, _, fatal_error = h.snapshot()

        if dev.thread is None:
            return False, "NO_THREAD", "thread not created"
        if (not dev.thread.is_alive()) and (not stop_event.is_set()):
            return False, "THREAD_DEAD", "thread not alive"

        if fatal_error:
            return False, "ERROR", fatal_error
        if state == "ERROR":
            return False, "ERROR", (detail or "error")

        if state in ("INIT", "CONNECTING"):
            return False, "CONNECTING", (detail or state)

        if state == "STOPPED":
            return False, "STOPPED", "stopped"

        if last_data_at is None:
            if connected_at is None:
                return False, "WAIT_CONNECT", (detail or "not connected")
            age = float(local_clock()) - float(connected_at)
            return False, "WAIT_DATA", f"connected, waiting first data ({age:.1f}s)"

        age = float(local_clock()) - float(last_data_at)
        if age >= NO_DATA_WARN_S:
            return False, "STALL", f"stalled (no data {age:.1f}s)"

        return True, "OK", "streaming"

    def run(self):
        _logged_active: set = set()
        _logged_error:  set = set()

        while not stop_event.is_set():
            for d in self.devices:
                if not d.health.enabled:
                    continue
                name = d.health.name
                state, detail, _, _, _, fatal_error = d.health.snapshot()

                # Messaggio una-tantum quando il dispositivo si connette
                if state == "ACTIVE" and name not in _logged_active:
                    _logged_active.add(name)
                    log("[SETUP]", f"[OK] {name} collegato")

                # Messaggio una-tantum in caso di errore irreversibile
                err_msg = fatal_error or (detail if state == "ERROR" else None)
                if err_msg and name not in _logged_error:
                    _logged_error.add(name)
                    log("[SETUP]", f"[ERR] {name} errore: {err_msg}")

                # Thread morto inaspettatamente
                if (d.thread is not None and not d.thread.is_alive()
                        and not stop_event.is_set()
                        and name not in _logged_error):
                    _logged_error.add(name)
                    log("[SETUP]", f"[ERR] {name} thread terminato inaspettatamente")

            # Sblocca gli stream quando tutti i dispositivi abilitati sono connessi
            if not ready_event.is_set():
                enabled = [d for d in self.devices if d.health.enabled]
                if enabled and all(d.health.name in _logged_active for d in enabled):
                    ready_event.set()
                    log("[SETUP]", "Tutti i dispositivi collegati -- avvio degli stream LSL")

            time.sleep(0.2)

# =====================================================
# Arduino micros wrap helper
# =====================================================

US_WRAP = 2 ** 32

def arduino_us64(wrap: int, us32: int) -> int:
    return int(wrap) * US_WRAP + int(us32)

# =====================================================
# Clock sync state + LSL clock stream
# =====================================================

@dataclass
class SyncSnapshot:
    seq: int
    t1: float
    t2: float
    t3: float
    t4: float
    rtt: float
    delay: float
    offset: float

class ClockSync:
    def __init__(self, name: str, source_id: str):
        self.name = name
        self.lock = threading.Lock()

        self.pending: Dict[int, float] = {}
        self.next_seq: int = 1

        self.offset: Optional[float] = None
        self.delay: Optional[float] = None
        self._prev_offset: Optional[float] = None

        info = StreamInfo(
            name=f"Clock_{name}",
            type="CLOCK",
            channel_count=10,
            nominal_srate=0.0,
            channel_format="float32",
            source_id=source_id,
        )
        try:
            chns = info.desc().append_child("channels")
            for lbl in [
                "seq", "t1_host", "t2_dev", "t3_dev", "t4_host",
                "rtt", "delay", "offset", "offset_smooth", "delay_smooth"
            ]:
                ch = chns.append_child("channel")
                ch.append_child_value("label", lbl)
                ch.append_child_value("type", "clock")
        except Exception:
            pass

        for _attempt in range(5):
            try:
                self.outlet = StreamOutlet(info)
                break
            except RuntimeError:
                if _attempt == 4:
                    raise RuntimeError(
                        "could not create stream outlet after 5 attempts. "
                        "Check if another LSL application or previous instance is running."
                    )
                time.sleep(1.0)

    def mark_request(self) -> int:
        t1 = float(local_clock())
        with self.lock:
            seq = int(self.next_seq)
            self.next_seq += 1
            if self.next_seq > 2_000_000_000:
                self.next_seq = 1
            self.pending[seq] = t1
            while len(self.pending) > 32:
                oldest_key = next(iter(self.pending))
                self.pending.pop(oldest_key, None)
        return seq

    def update_from_reply(self, seq: int, t2: float, t3: float, *, alpha: float = SYNC_ALPHA) -> Tuple[Optional[SyncSnapshot], Optional[str]]:
        t4 = float(local_clock())

        with self.lock:
            t1 = self.pending.pop(int(seq), None)

        if t1 is None:
            return None, f"unknown_or_stale_seq:{seq}"

        rtt = (t4 - t1)
        delay = ((t4 - t1) - (t3 - t2)) / 2.0
        offset = (t2 - (t1 + delay))

        if (rtt < 0) or (rtt > MAX_RTT_S):
            return None, f"reject_rtt:{rtt:.6f}"
        if abs(delay) > MAX_ABS_DELAY_S:
            return None, f"reject_delay:{delay:.6f}"

        with self.lock:
            if self._prev_offset is not None and abs(offset - self._prev_offset) > MAX_ABS_OFFSET_JUMP_S:
                return None, f"reject_offset_jump:{offset - self._prev_offset:.6f}"

            if self.offset is None:
                self.offset = float(offset)
            else:
                self.offset = float((1.0 - alpha) * self.offset + alpha * offset)

            if self.delay is None:
                self.delay = float(delay)
            else:
                self.delay = float((1.0 - alpha) * self.delay + alpha * delay)

            self._prev_offset = float(offset)

            offset_s = float(self.offset)
            delay_s = float(self.delay)

        try:
            self.outlet.push_sample(
                [
                    float(seq), float(t1), float(t2), float(t3), float(t4),
                    float(rtt), float(delay), float(offset), float(offset_s), float(delay_s)
                ],
                timestamp=float(t4),
            )
        except Exception:
            pass

        return SyncSnapshot(
            seq=int(seq),
            t1=t1,
            t2=t2,
            t3=t3,
            t4=t4,
            rtt=rtt,
            delay=delay,
            offset=offset,
        ), None

    def estimate_host_time(self, t_dev: float) -> float:
        with self.lock:
            off = self.offset
        if off is None:
            return float(local_clock())
        return float(t_dev - off)

# =====================================================
# LSL outlet helper
# =====================================================

def make_lsl_outlet(
    stream_name: str,
    stream_type: str,
    channel_labels: List[str],
    nominal_srate: float,
    source_id: str
) -> StreamOutlet:
    info = StreamInfo(
        name=stream_name,
        type=stream_type,
        channel_count=len(channel_labels),
        nominal_srate=float(nominal_srate),
        channel_format="float32",
        source_id=source_id,
    )
    try:
        chns = info.desc().append_child("channels")
        for lbl in channel_labels:
            ch = chns.append_child("channel")
            ch.append_child_value("label", str(lbl))
            ch.append_child_value("type", str(stream_type).lower())
    except Exception:
        pass
    for _attempt in range(5):
        try:
            return StreamOutlet(info)
        except RuntimeError:
            if _attempt == 4:
                raise RuntimeError(
                    "could not create stream outlet after 5 attempts. "
                    "Check if another LSL application or previous instance is running."
                )
            time.sleep(1.0)

# =====================================================
# Signal filter — weighted moving average (pure Python)
# =====================================================

# 5-tap symmetric triangular weights: centre sample has weight 3, neighbours 2 and 1.
# Group delay: ~2 samples (causal). At 250 Hz ECG → ~8 ms; at 25 Hz PPG → ~80 ms.
# Change these lists to tune the filter (any length, any positive weights).
ECG_FILTER_WEIGHTS = [1, 2, 3, 2, 1]
PPG_FILTER_WEIGHTS = [1, 2, 3, 2, 1]
ACC_FILTER_WEIGHTS = [1, 2, 3, 4, 3, 2, 1]  # 7-tap triangular — smooths 50Hz ACC

class SignalFilter:
    """Causal weighted moving average, pure Python, no dependencies.

    During warm-up (fewer samples than the window length) the filter uses only
    the available samples with the right-aligned tail of the weight vector, so
    there is no startup bias from implicit zeros.
    A NaN input flushes the buffer (gap in data → restart smoothly).
    """

    def __init__(self, weights: list):
        self._w = [float(x) for x in weights]
        self._n = len(self._w)
        self._buf: list = []

    def apply(self, x: float) -> float:
        import math
        if math.isnan(x):
            self._buf = []
            return x
        self._buf.append(x)
        if len(self._buf) > self._n:
            del self._buf[0]
        k = len(self._buf)
        ws = self._w[self._n - k:]
        total = sum(ws[i] * self._buf[i] for i in range(k))
        return total / sum(ws)

    def reset(self):
        self._buf = []


# =====================================================
# Polar ECG imputer — R-peak template + RR prediction
# =====================================================

class PolarECGImputer:
    """Fills BLE dropout gaps in Polar H10 ECG with synthetic beats.

    Algorithm
    ---------
    Feed every ECG sample (including NaN gaps) through push().
    - Real samples update an R-peak detector, a beat template (average of
      the last MAX_BEATS beats), and an RR-interval history.
    - When a NaN run ends and the gap is ≤ max_gap_s, _fill() generates
      synthetic samples by placing the template at phase-continuous beat
      positions and returns them together with the resuming real sample.

    Timestamps are in Arduino device-seconds (same domain as t_dev_s),
    so the caller can convert them to LSL time with the usual sync object.

    Peak detection
    --------------
    - Delayed by POST_S seconds so we can inspect the full post-R window.
    - Candidate must be the global maximum of its [PRE_S … POST_S] window.
    - Window amplitude must exceed MIN_AMP_UV (guards against flat noise).
    - Enforces a refractory period of REFRACT_S after every accepted peak.

    Notes
    -----
    - The template is baseline-subtracted (DC-removed per beat), so the
      synthetic signal is centred around 0.  This is intentional: it makes
      the imputed segment visually distinguishable from real ECG while
      still showing correct beat morphology and timing.
    - ACC channels are set to NaN for imputed samples (no ACC data for gaps).
    """

    FS          = 130.0   # Hz  — must match POLAR_2.ino ECG output rate
    PRE_S       = 0.25    # window before R-peak (seconds)
    POST_S      = 0.40    # window after  R-peak (seconds)
    LOCAL_WIN   = 5       # half-width for local-max check (samples, ~38 ms at 130 Hz)
    MIN_RR_S    = 0.35    # fastest plausible heart rate (~170 bpm)
    MAX_RR_S    = 1.60    # slowest plausible heart rate (~37 bpm)
    REFRACT_S   = 0.40    # min time between two accepted peaks (was 0.35)
    AMP_FRAC    = 0.55    # beat must reach ≥ 55 % of the adaptive peak-amplitude EMA
    MIN_AMP_UV  = 60.0    # µV — fallback threshold before EMA is initialised
    SHARP_FRAC  = 0.50    # sharpness: signal must change ≥ 50 % of amplitude in LOCAL_WIN
                          # (was 0.40 — tightened to reject borderline T-waves)
    MAX_DERIV_WIN = 3     # half-width for max-derivative check (samples, ~23ms)
    MAX_BEATS   = 8       # beats kept in template average
    MAX_RR_HIST = 10      # RR intervals kept for heart-rate estimate

    def __init__(self, max_gap_s: float = 4.0):
        self._max_gap   = max_gap_s
        self._pre       = int(self.PRE_S   * self.FS)
        self._post      = int(self.POST_S  * self.FS)
        self._refract   = int(self.REFRACT_S * self.FS)

        self._buf_max   = int(3.0 * self.FS)
        self._buf_v: List[float] = []
        self._buf_t: List[float] = []

        self._beats:    List[List[float]] = []
        self._template: Optional[List[float]] = None

        self._rr_hist:      List[float]  = []
        self._last_peak_ts: Optional[float] = None
        self._since_peak    = self._refract   # start ready
        self._peak_amp_ema: Optional[float]  = None  # adaptive amplitude estimate

        self._in_gap        = False
        self._gap_start_ts: Optional[float] = None
        self._last_beat_output_ts: Optional[float] = None  # wall-clock guard for output

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def push(self, ts: float, val: float) -> List[Tuple[float, float, bool]]:
        """Feed one sample (ts in device-seconds, val in µV or NaN).

        Returns a list of (ts, ecg_val, is_beat) triples:
          - Normal:    [(ts, val, is_beat)]      is_beat=True when R-peak confirmed
          - Gap start: []                        NaN samples silently consumed
          - Gap end:   [(imp_ts, imp_val, is_beat), ..., (ts, val, is_beat)]

        NOTE: R-peak detection is delayed by POST_S (~0.4 s) because it waits for
        the post-peak window to complete before confirming.  When is_beat=True on
        a real sample at time T, the actual peak occurred at approximately T-POST_S.
        Imputed beats (from gap filling) are marked at their exact predicted position.
        """
        import math
        if math.isnan(val):
            if not self._in_gap:
                self._in_gap       = True
                self._gap_start_ts = ts
            return []

        imputed: List[Tuple[float, float, bool]] = []
        if self._in_gap and self._gap_start_ts is not None:
            gap_dur = ts - self._gap_start_ts
            if 0.0 < gap_dur <= self._max_gap:
                imputed = self._fill(self._gap_start_ts, ts)
            self._in_gap       = False
            self._gap_start_ts = None

        prev_peak = self._last_peak_ts
        self._ingest(ts, val)
        is_beat = (self._last_peak_ts is not None and self._last_peak_ts != prev_peak)

        # Gap prediction: if no beat detected for > 1.8 × mean RR, advance the
        # expected beat position and mark this sample as a predicted beat.
        if (not is_beat
                and self._last_peak_ts is not None
                and len(self._rr_hist) >= 2):
            rr = self._rr_estimate()
            elapsed = (ts - self.POST_S) - self._last_peak_ts
            if elapsed > 1.8 * rr:
                self._last_peak_ts = self._last_peak_ts + rr
                is_beat = True

        # Output guard: never emit two is_beat=True events within MIN_RR_S
        results = imputed + [(ts, val, is_beat)]
        filtered = []
        for (evt_ts, evt_val, evt_beat) in results:
            if evt_beat:
                if (self._last_beat_output_ts is None
                        or evt_ts - self._last_beat_output_ts >= self.MIN_RR_S):
                    self._last_beat_output_ts = evt_ts
                    filtered.append((evt_ts, evt_val, True))
                else:
                    filtered.append((evt_ts, evt_val, False))
            else:
                filtered.append((evt_ts, evt_val, False))
        return filtered

    def has_template(self) -> bool:
        return self._template is not None and len(self._rr_hist) >= 2

    # ------------------------------------------------------------------
    # Internal: live sample ingestion
    # ------------------------------------------------------------------

    def _ingest(self, ts: float, val: float) -> None:
        self._buf_v.append(val)
        self._buf_t.append(ts)
        if len(self._buf_v) > self._buf_max:
            del self._buf_v[0]
            del self._buf_t[0]

        self._since_peak += 1

        # R-peak detection: candidate is at position ci = buf_len - _post - 1.
        # We wait _post samples after it to confirm it is a local extremum.
        n  = len(self._buf_v)
        ci = n - self._post - 1
        if ci < self._pre:
            return
        if self._since_peak < self._refract:
            return

        cval = self._buf_v[ci]
        win  = self._buf_v[ci - self._pre : ci + self._post + 1]  # used for template

        # Local-max/min check over ±LOCAL_WIN samples.
        # Amplitude and polarity are also computed on the LOCAL window — NOT on the
        # full PRE+POST window (wmin/wmax). At normal heart rates the full window
        # spans two R-peaks, so wmax is the taller of the two, making
        # neg_amp = wmax-cval large even for a genuine positive peak, which caused
        # the polarity branch to flip and the local-ext check to fail on most beats.
        lo = max(0, ci - self.LOCAL_WIN)
        hi = min(n - 1, ci + self.LOCAL_WIN)
        local     = self._buf_v[lo : hi + 1]
        local_max = max(local)
        local_min = min(local)

        pos_amp = cval - local_min   # amplitude if positive peak
        neg_amp = local_max - cval   # amplitude if negative peak (inverted ECG)

        if pos_amp >= neg_amp:
            amp          = pos_amp
            is_local_ext = (cval == local_max)
        else:
            amp          = neg_amp
            is_local_ext = (cval == local_min)

        if not is_local_ext:
            return

        # Sharpness check 1: upslope over LOCAL_WIN samples
        slope_start = max(0, ci - self.LOCAL_WIN)
        upslope = abs(cval - self._buf_v[slope_start])
        if amp > 0 and upslope < amp * self.SHARP_FRAC:
            return

        # Sharpness check 2: max sample-to-sample derivative near the peak.
        # R-peaks have derivatives 5–10x steeper than T-waves.  We require
        # the max |diff| in ±MAX_DERIV_WIN around the candidate to exceed
        # 20% of the candidate's amplitude PER SAMPLE.  At 130 Hz:
        #   R-peak: rises ~800 µV in 2–3 samples → |diff| ≈ 300 µV/sample
        #   T-wave: rises ~200 µV in 10+ samples → |diff| ≈ 20 µV/sample
        dlo = max(1, ci - self.MAX_DERIV_WIN)
        dhi = min(n - 1, ci + self.MAX_DERIV_WIN)
        max_diff = 0.0
        for di in range(dlo, dhi + 1):
            d = abs(self._buf_v[di] - self._buf_v[di - 1])
            if d > max_diff:
                max_diff = d
        if amp > 0 and max_diff < amp * 0.20:
            return

        # Adaptive amplitude threshold
        threshold = (self._peak_amp_ema * self.AMP_FRAC
                     if self._peak_amp_ema is not None
                     else self.MIN_AMP_UV)
        if amp < threshold:
            return

        # ---- Valid R-peak ----
        peak_ts = self._buf_t[ci]
        # Enforce minimum RR even if gap prediction already advanced _last_peak_ts.
        if (self._last_peak_ts is not None
                and peak_ts - self._last_peak_ts < self.MIN_RR_S):
            return
        self._since_peak = 0
        self._peak_amp_ema = (amp if self._peak_amp_ema is None
                              else 0.8 * self._peak_amp_ema + 0.2 * amp)

        if self._last_peak_ts is not None:
            rr = peak_ts - self._last_peak_ts
            if self.MIN_RR_S <= rr <= self.MAX_RR_S:
                self._rr_hist.append(rr)
                if len(self._rr_hist) > self.MAX_RR_HIST:
                    del self._rr_hist[0]
        self._last_peak_ts = peak_ts

        # Baseline-subtract and store beat
        edge = max(1, len(win) // 5)
        bl   = (sum(win[:edge]) + sum(win[-edge:])) / (2 * edge)
        beat = [v - bl for v in win]

        self._beats.append(beat)
        if len(self._beats) > self.MAX_BEATS:
            del self._beats[0]

        # Recompute template as arithmetic mean of stored beats
        tlen  = len(self._beats[0])
        valid = [b for b in self._beats if len(b) == tlen]
        if len(valid) >= 2:
            tmpl = [0.0] * tlen
            for b in valid:
                for i in range(tlen):
                    tmpl[i] += b[i]
            self._template = [v / len(valid) for v in tmpl]

    # ------------------------------------------------------------------
    # Internal: gap filling
    # ------------------------------------------------------------------

    def _rr_estimate(self) -> Optional[float]:
        if not self._rr_hist:
            return None
        return sum(self._rr_hist) / len(self._rr_hist)

    def _fill(self, gap_start: float, gap_end: float) -> List[Tuple[float, float, bool]]:
        if self._template is None or self._last_peak_ts is None:
            return []
        rr = self._rr_estimate()
        if rr is None:
            return []

        dt     = 1.0 / self.FS
        n      = max(0, int((gap_end - gap_start) * self.FS) - 1)
        if n == 0:
            return []

        ts_out  = [gap_start + i * dt for i in range(n)]
        vals    = [0.0] * n
        is_beat = [False] * n

        # Phase-continuous beat placement: compute where in the RR cycle
        # we are at gap_start, then count forward by rr until gap_end.
        phase       = (gap_start - self._last_peak_ts) % rr
        beat_offset = rr - phase     # seconds to first beat inside gap

        while beat_offset < (gap_end - gap_start):
            ci = int(beat_offset * self.FS)
            for j, v in enumerate(self._template):
                idx = ci - self._pre + j
                if 0 <= idx < n:
                    vals[idx] += v
            # Mark the R-peak position (centre of the template window).
            if 0 <= ci < n:
                is_beat[ci] = True
            beat_offset += rr

        return [(ts_out[i], vals[i], is_beat[i]) for i in range(n)]


# =====================================================
# TCP helpers
# =====================================================

def tcp_connect(ip: str, port: int) -> socket.socket:
    soc = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    soc.connect((ip, port))
    soc.setblocking(False)
    log("[TCP]", f"Connected to {ip}:{port}")
    return soc

def tcp_send(sock: socket.socket, line: str) -> None:
    try:
        data = (line + "\n").encode("utf-8")
        with send_lock:
            sock.sendall(data)
    except OSError as e:
        log("[TCP]", f"Send error ({line!r}): {e}")

def tcp_read_lines(sock: socket.socket, buffer: str, timeout: float = 0.1) -> Tuple[str, List[str], bool]:
    try:
        r, _, _ = select.select([sock], [], [], timeout)
        if not r:
            return buffer, [], False

        chunk = sock.recv(4096)
        if not chunk:
            return buffer, [], True

        buffer += chunk.decode("utf-8", errors="replace")
        parts = buffer.split("\n")
        buffer = parts[-1]
        lines = [p.strip() for p in parts[:-1] if p.strip()]
        return buffer, lines, False

    except OSError as e:
        log("[TCP]", f"Read error: {e}")
        return buffer, [], True

# =====================================================
# Serial helpers
# =====================================================

def serial_open(port: str, baud: int):
    try:
        import serial
    except ImportError:
        log("[SERIAL]", "pyserial not installed. pip install pyserial")
        return None

    try:
        ser = serial.Serial(port=port, baudrate=int(baud), timeout=0.0)
        # Ingrandisci i buffer seriali OS (default Windows = 4KB, troppo poco
        # per 130Hz × ~60 byte/linea = 7.8 KB/s — se il thread è occupato
        # per >0.5s il buffer overflow causa perdita dati).
        try:
            ser.set_buffer_size(rx_size=131072, tx_size=16384)  # 128KB RX
        except Exception:
            pass  # non tutti gli OS/driver supportano set_buffer_size
        time.sleep(1.5)
        ser.reset_input_buffer()
        log("[SERIAL]", f"Opened {port} @ {baud}")
        return ser
    except Exception as e:
        log("[SERIAL]", f"Cannot open {port}: {e}")
        try:
            from serial.tools import list_ports
            available = [p.device for p in list_ports.comports()]
            log("[SERIAL]", f"Porte seriali disponibili: {available if available else '(nessuna)'}")
        except Exception:
            pass
        return None

def serial_send(ser, line: str) -> None:
    try:
        data = (line + "\n").encode("utf-8")
        with send_lock:
            ser.write(data)
    except Exception as e:
        log("[SERIAL]", f"Send error ({line!r}): {e}")

def serial_read_lines(ser, buffer: str, max_bytes: int = 65536) -> Tuple[str, List[str]]:
    try:
        n = ser.in_waiting
        if n <= 0:
            return buffer, []
        chunk = ser.read(min(n, max_bytes))
        if not chunk:
            return buffer, []

        buffer += chunk.decode("utf-8", errors="replace")
        parts = buffer.split("\n")
        buffer = parts[-1]
        lines = [p.strip() for p in parts[:-1] if p.strip()]
        return buffer, lines
    except Exception as e:
        log("[SERIAL]", f"Read error: {e}")
        return buffer, []

# =====================================================
# Parsing helpers
# =====================================================

def parse_T_payload(value: str) -> Optional[Tuple[int, float, float]]:
    """
    Supported:
      - "seq,t2_us,t3_us"
      - "seq,w2,t2_32,w3,t3_32"
    Returns:
      (seq, t2_s, t3_s)
    """
    try:
        parts = [p.strip() for p in value.split(",") if p.strip()]

        if len(parts) == 3:
            seq = int(parts[0])
            t2_s = float(parts[1]) / 1e6
            t3_s = float(parts[2]) / 1e6
            return seq, t2_s, t3_s

        if len(parts) == 5:
            seq = int(parts[0])
            w2 = int(parts[1]); t2_32 = int(parts[2])
            w3 = int(parts[3]); t3_32 = int(parts[4])
            t2_s = arduino_us64(w2, t2_32) / 1e6
            t3_s = arduino_us64(w3, t3_32) / 1e6
            return seq, t2_s, t3_s

        return None
    except Exception:
        return None

def parse_wrapped_sample(value: str, n_ch: int) -> Optional[Tuple[float, List[float]]]:
    """
    Formato nominale (nuovo): "wrap:123,us32:456789,nome1:v1,nome2:v2,..."
    Formato posizionale (legacy): "wrap,us32,val1,val2,...,valN"
    Ritorna (t_dev_s, [val1, val2, ...]).
    """
    try:
        parts = [p.strip() for p in value.split(",") if p.strip()]

        # Formato nominale: ogni campo contiene ":"
        if all(":" in p for p in parts):
            kv: Dict[str, str] = {}
            ordered: List[Tuple[str, str]] = []
            for p in parts:
                k, v = p.split(":", 1)
                kv[k.strip()] = v.strip()
                ordered.append((k.strip(), v.strip()))
            wrap = int(kv["wrap"])
            us32 = int(kv["us32"])
            vals = [float(v) for k, v in ordered if k not in ("wrap", "us32", "seq")]
            if len(vals) != n_ch:
                return None
            t_us64 = arduino_us64(wrap, us32)
            return float(t_us64 / 1e6), vals

        # Formato posizionale legacy: "wrap,us32,val1,...,valN"
        if len(parts) != (2 + n_ch):
            return None
        wrap = int(parts[0])
        us32 = int(parts[1])
        vals = [float(x) for x in parts[2:]]
        t_us64 = arduino_us64(wrap, us32)
        return float(t_us64 / 1e6), vals

    except Exception:
        return None

def split_messages(line: str) -> List[Tuple[str, str]]:
    out = []
    for msg in line.strip().split("\t"):
        msg = msg.strip()
        if not msg:
            continue
        if ":" not in msg:
            continue
        label, payload = msg.split(":", 1)
        out.append((label.strip(), payload.strip()))
    return out

# =====================================================
# Arduino WiFi ECG thread
# =====================================================

class ArduinoWiFiECGThread(threading.Thread):
    def __init__(self, ip: str, port: int, data_label: str, n_ch: int, health: DeviceHealth):
        super().__init__(daemon=True)
        self.ip = ip
        self.port = int(port)
        self.data_label = data_label
        self.n_ch = int(n_ch)
        self.health = health

        self.sock: Optional[socket.socket] = None
        self.buf = ""
        self.sync = ClockSync(name="ArduinoWiFi_ECG", source_id="clock_arduino_wifi_ecg")

        self.outlet = make_lsl_outlet(
            stream_name="ArduinoWiFi_ECG",
            stream_type="BIO",
            channel_labels=["ecg"] if self.n_ch == 1 else [f"ch{i}" for i in range(self.n_ch)],
            nominal_srate=ARD_WIFI_SRATE,
            source_id="arduino_wifi_ecg",
        )

        self.connected_at: Optional[float] = None
        self.last_data_at: Optional[float] = None
        self.last_heartbeat_at: float = 0.0
        self.last_warn_at: float = 0.0
        self.last_sync_warn_at: float = 0.0
        self._printed_first_data: bool = False

        self._ecg_filter = SignalFilter(ECG_FILTER_WEIGHTS) if ENABLE_SIGNAL_FILTER else None

    def connect(self) -> bool:
        self.health.set(state="CONNECTING", detail=f"{self.ip}:{self.port}")
        try:
            self.sock = tcp_connect(self.ip, self.port)
            self.connected_at = float(local_clock())
            self.health.set(state="ACTIVE", connected_at=self.connected_at, detail="connected (waiting data)")
            log("[ArduinoWiFiECG]", f"Connection ACTIVE ({self.ip}:{self.port}). Waiting label='{self.data_label}'.")
            return True
        except Exception as e:
            self.health.set(state="ERROR", fatal_error=f"connect failed: {e}")
            log("[ArduinoWiFiECG]", f"Connect error: {e}")
            return False

    def send_sync(self):
        if not self.sock:
            return
        seq = self.sync.mark_request()
        tcp_send(self.sock, f"SYNC:{seq}")

    def run(self):
        if not ENABLE_ARDUINO_WIFI_ECG:
            return
        if not self.connect():
            return

        next_sync = 0.0
        while not stop_event.is_set():
            now = float(local_clock())

            if now >= next_sync:
                self.send_sync()
                next_sync = now + float(SYNC_INTERVAL_S)

            self.buf, lines, closed = tcp_read_lines(self.sock, self.buf, timeout=0.1)
            if closed:
                self.health.set(state="ERROR", fatal_error="connection closed/EOF")
                log("[ArduinoWiFiECG]", "Connection closed / EOF from device.")
                break

            for line in lines:
                for label, payload in split_messages(line):
                    if label == "T":
                        tt = parse_T_payload(payload)
                        if tt is None:
                            continue
                        seq, t2, t3 = tt
                        _, err = self.sync.update_from_reply(seq, t2, t3)
                        now_log = float(local_clock())
                        if err is not None and (now_log - self.last_sync_warn_at) >= 5.0:
                            log("[ArduinoWiFiECG]", f"SYNC rejected: {err}")
                            self.last_sync_warn_at = now_log
                        continue

                    if label == self.data_label:
                        parsed = parse_wrapped_sample(payload, self.n_ch)
                        if parsed is None:
                            continue
                        t_dev_s, vals = parsed
                        if self._ecg_filter is not None:
                            vals = [self._ecg_filter.apply(vals[0])]
                        ts_host = self.sync.estimate_host_time(t_dev_s)

                        now2 = float(local_clock())
                        self.last_data_at = now2
                        self.health.set(last_data_at=now2, first_data=True, detail="streaming")

                        if ready_event.is_set():
                            try:
                                self.outlet.push_sample([float(v) for v in vals], timestamp=float(ts_host))
                                if not self._printed_first_data:
                                    log("[ArduinoWiFiECG]", "Primo campione LSL inviato.")
                                    self._printed_first_data = True
                            except Exception as e:
                                log("[ArduinoWiFiECG]", f"LSL push error: {e}")

            now3 = float(local_clock())

            if self.connected_at is not None and self.last_data_at is None:
                if (now3 - self.connected_at) >= NO_DATA_WARN_S and (now3 - self.last_warn_at) >= NO_DATA_REPEAT_S:
                    log("[ArduinoWiFiECG]", f"WARNING: connected but NO SENSOR DATA yet (>{now3 - self.connected_at:.1f}s).")
                    self.last_warn_at = now3

            if self.last_data_at is not None:
                age = now3 - self.last_data_at

                if (now3 - self.last_heartbeat_at) >= STATUS_HEARTBEAT_S:
                    log("[ArduinoWiFiECG]", f"Data flowing (last sample {age*1000:.0f} ms ago).")
                    self.last_heartbeat_at = now3

                if age >= NO_DATA_WARN_S and (now3 - self.last_warn_at) >= NO_DATA_REPEAT_S:
                    log("[ArduinoWiFiECG]", f"WARNING: NO SENSOR DATA for {age:.1f}s (stream stalled?).")
                    self.last_warn_at = now3

        try:
            if self.sock:
                self.sock.close()
        except Exception:
            pass
        self.health.set(state="STOPPED", detail="stopped")
        log("[ArduinoWiFiECG]", "Thread stopped.")

# =====================================================
# Arduino USB (Polar bridge) thread
# =====================================================

class ArduinoUSBPolarThread(threading.Thread):
    def __init__(self, port: str, baud: int, label_map: Dict[str, Tuple[str, List[str], float]], health: DeviceHealth):
        super().__init__(daemon=True)
        self.port = port
        self.baud = int(baud)
        self.label_map = label_map
        self.health = health

        self.ser = None
        self.buf = ""
        self.sync = ClockSync(name="ArduinoUSB_Polar", source_id="clock_arduino_usb_polar")

        self.outlets: Dict[str, StreamOutlet] = {}
        for lbl, (sname, ch_labels, srate) in self.label_map.items():
            self.outlets[lbl] = make_lsl_outlet(
                stream_name=sname,
                stream_type="BIO",
                channel_labels=ch_labels,
                nominal_srate=float(srate),
                source_id=f"polar_{lbl.lower()}",
            )

        self.connected_at: Optional[float] = None
        self.last_line_at: Optional[float] = None
        self.last_data_any_at: Optional[float] = None
        self.last_data_by_label: Dict[str, float] = {}
        self.last_heartbeat_at: float = 0.0
        self.last_warn_at: float = 0.0
        self.last_sync_warn_at: float = 0.0
        self._printed_first_line: bool = False
        self._printed_first_by_label: Dict[str, bool] = {lbl: False for lbl in self.label_map.keys()}

        # Filter only the ECG channel (index 0) of each Polar label; ACC is not filtered.
        self._polar_ecg_filters: Dict[str, SignalFilter] = (
            {lbl: SignalFilter(ECG_FILTER_WEIGHTS) for lbl in self.label_map}
            if ENABLE_SIGNAL_FILTER else {}
        )

        # ECG imputer: always created for beat detection.
        # max_gap_s=0 disables gap filling but keeps R-peak tracking for the beat channel.
        _gap_s = float(IMPUTER_MAX_GAP_S) if ENABLE_ECG_IMPUTATION else 0.0
        self._ecg_imputers: Dict[str, PolarECGImputer] = {
            lbl: PolarECGImputer(max_gap_s=_gap_s) for lbl in self.label_map
        }

    def connect(self) -> bool:
        self.health.set(state="CONNECTING", detail=f"{self.port}@{self.baud}")
        self.ser = serial_open(self.port, self.baud)
        if self.ser is not None:
            self.connected_at = float(local_clock())
            self.health.set(state="ACTIVE", connected_at=self.connected_at, detail="connected (waiting data)")
            log("[ArduinoUSBPolar]", f"Connection ACTIVE ({self.port} @ {self.baud}).")
            log("[ArduinoUSBPolar]", "Expected labels: T, PECG, PACC")
            return True

        self.health.set(state="ERROR", fatal_error="cannot open serial port")
        return False

    def send_sync(self):
        if not self.ser:
            return
        seq = self.sync.mark_request()
        serial_send(self.ser, f"SYNC:{seq}")

    def _push_label(self, lbl: str, values: List[float], t_dev_s: float) -> None:
        # values = firmware channels only (ecg, ax, ay, az) — 'beat' appended here.
        imputer = self._ecg_imputers.get(lbl)
        n_fw    = len(values)   # firmware channel count (without beat)

        if imputer is not None and n_fw > 0:
            raw = imputer.push(t_dev_s, values[0])
            # raw = [(ts, ecg, is_beat), ...]  last entry is always the real sample.
            pairs: List[Tuple[float, List[float]]] = []
            for i, (imp_ts, imp_ecg, is_beat) in enumerate(raw):
                beat_val  = 1.0 if is_beat else 0.0
                is_real   = (i == len(raw) - 1)
                if is_real:
                    # Real sample: keep original ACC channels.
                    imp_vals = [imp_ecg] + list(values[1:]) + [beat_val]
                else:
                    # Imputed sample: no ACC data for the gap.
                    imp_vals = [imp_ecg] + [float('nan')] * (n_fw - 1) + [beat_val]
                pairs.append((imp_ts, imp_vals))
        else:
            pairs = [(t_dev_s, list(values) + [0.0])]

        # --- Health bookkeeping ---
        now = float(local_clock())
        self.last_data_any_at        = now
        self.last_data_by_label[lbl] = now
        self.health.set(last_data_at=now, first_data=True, detail="streaming")

        if not ready_event.is_set():
            return

        # --- Filter ECG channel, then push ---
        filt = self._polar_ecg_filters.get(lbl)
        try:
            for ts_dev, vals in pairs:
                if filt is not None and len(vals) > 0:
                    vals = [filt.apply(vals[0])] + list(vals[1:])
                ts_host = self.sync.estimate_host_time(ts_dev)
                self.outlets[lbl].push_sample([float(v) for v in vals], timestamp=float(ts_host))
            if not self._printed_first_by_label.get(lbl, False):
                sname = self.label_map.get(lbl, ("(unknown)", [], 0.0))[0]
                log("[ArduinoUSBPolar]", f"Primo campione LSL inviato (label='{lbl}' -> '{sname}').")
                self._printed_first_by_label[lbl] = True
        except Exception as e:
            log("[ArduinoUSBPolar]", f"LSL push error ({lbl}): {e}")

    # ── producer-consumer architecture ──────────────────────────────
    # Thread 1 (_serial_reader): ONLY reads serial bytes into a Queue.
    #   Runs in a tight loop with NO processing — never misses data.
    # Thread 2 (run / main worker): takes lines from Queue, parses,
    #   runs imputer, pushes to LSL.  Can be as slow as it needs.
    # ─────────────────────────────────────────────────────────────────

    def _serial_reader(self):
        """Dedicated serial reader — runs in its own daemon thread.
        Only reads bytes and splits into lines → self._line_q.
        Never blocks on anything except serial I/O."""
        import queue
        buf = ""
        ser = self.ser
        while not stop_event.is_set():
            try:
                n = ser.in_waiting
                if n <= 0:
                    time.sleep(0.0002)  # 0.2ms idle poll
                    continue
                chunk = ser.read(min(n, 65536))
                if not chunk:
                    continue
                buf += chunk.decode("utf-8", errors="replace")
                parts = buf.split("\n")
                buf = parts[-1]
                for p in parts[:-1]:
                    p = p.strip()
                    if p:
                        self._line_q.put(p)
            except Exception as e:
                log("[SerialReader]", f"err: {e}")
                time.sleep(0.01)

    def run(self):
        import queue
        if not ENABLE_ARDUINO_USB_POLAR:
            return
        if not self.connect():
            return

        # line queue: serial reader → worker
        self._line_q: queue.Queue = queue.Queue(maxsize=50000)

        # start dedicated serial reader thread
        reader_t = threading.Thread(target=self._serial_reader, daemon=True,
                                    name="PolarSerialReader")
        reader_t.start()
        log("[ArduinoUSBPolar]", "Serial reader thread started (producer-consumer mode).")

        next_sync = 0.0
        _lines_read = 0
        _samples_pushed = 0
        _last_stats = float(local_clock())

        try:
          while not stop_event.is_set():
            now = float(local_clock())

            # SYNC: still on this thread (serial writes are fast and non-blocking)
            if now >= next_sync:
                self.send_sync()
                next_sync = now + float(SYNC_INTERVAL_S)

            # Drain the line queue (all available lines, up to 500 per batch)
            batch = []
            for _ in range(500):
                try:
                    batch.append(self._line_q.get_nowait())
                except Exception:
                    break

            if not batch:
                time.sleep(0.002)  # 2ms — nothing to process
                continue

            for line in batch:
                _lines_read += 1
                self.last_line_at = float(local_clock())
                self._printed_first_line = True

                for label, payload in split_messages(line):
                    if label in ("INFO", "WARN", "ERR", "HELLO"):
                        log("[PolarArduino]", f"{label}: {payload}")
                        continue

                    if label == "T":
                        tt = parse_T_payload(payload)
                        if tt is None:
                            continue
                        seq, t2, t3 = tt
                        _, err = self.sync.update_from_reply(seq, t2, t3)
                        now_log = float(local_clock())
                        if err is not None and (now_log - self.last_sync_warn_at) >= 5.0:
                            log("[ArduinoUSBPolar]", f"SYNC rejected: {err}")
                            self.last_sync_warn_at = now_log
                        continue

                    if label in self.label_map and label in self.outlets:
                        _, ch_labels, _ = self.label_map[label]
                        n_fw_ch = len(ch_labels) - 1
                        parsed = parse_wrapped_sample(payload, n_fw_ch)
                        if parsed is None:
                            continue
                        t_dev_s, vals = parsed
                        self._push_label(label, vals, t_dev_s)
                        _samples_pushed += 1

            # periodic stats (every 10s)
            now3 = float(local_clock())
            if (now3 - _last_stats) >= 10.0:
                qsz = self._line_q.qsize()
                expected = 130 * (now3 - _last_stats)
                log("[ArduinoUSBPolar]",
                    f"STATS: lines={_lines_read}, pushed={_samples_pushed}, "
                    f"q_depth={qsz}, expected~{expected:.0f}")
                _lines_read = 0
                _samples_pushed = 0
                _last_stats = now3

            if self.connected_at is not None and self.last_line_at is None:
                if (now3 - self.connected_at) >= NO_DATA_WARN_S and (now3 - self.last_warn_at) >= NO_DATA_REPEAT_S:
                    log("[ArduinoUSBPolar]", f"WARNING: connected but NO SERIAL LINES yet (>{now3 - self.connected_at:.1f}s).")
                    log("[ArduinoUSBPolar]", "Tip: close Arduino IDE Serial Monitor (COM port is exclusive).")
                    self.last_warn_at = now3

            if self.last_line_at is not None and self.last_data_any_at is None:
                age_line = now3 - self.last_line_at
                if age_line >= NO_DATA_WARN_S and (now3 - self.last_warn_at) >= NO_DATA_REPEAT_S:
                    log("[ArduinoUSBPolar]", f"WARNING: got serial lines, but none matched parsing/push for {age_line:.1f}s.")
                    log("[ArduinoUSBPolar]", "Expected labels: PECG / PACC / T")
                    self.last_warn_at = now3

            if self.last_data_any_at is not None:
                age_any = now3 - self.last_data_any_at

                if (now3 - self.last_heartbeat_at) >= STATUS_HEARTBEAT_S:
                    parts = []
                    for lbl in self.label_map.keys():
                        if lbl in self.last_data_by_label:
                            age = now3 - self.last_data_by_label[lbl]
                            parts.append(f"{lbl}:{age*1000:.0f}ms")
                        else:
                            parts.append(f"{lbl}:no-data")
                    log("[ArduinoUSBPolar]", "Data flowing (" + ", ".join(parts) + ")")
                    self.last_heartbeat_at = now3

                if age_any >= NO_DATA_WARN_S and (now3 - self.last_warn_at) >= NO_DATA_REPEAT_S:
                    log("[ArduinoUSBPolar]", f"WARNING: NO PUSHED SENSOR DATA for {age_any:.1f}s (stream stalled?).")
                    self.last_warn_at = now3

        except Exception as e:
            self.health.set(state="ERROR", fatal_error=f"eccezione non gestita: {e}")
            log("[ArduinoUSBPolar]", f"ERRORE fatale nel thread: {e}")

        try:
            if self.ser:
                self.ser.close()
        except Exception:
            pass
        self.health.set(state="STOPPED", detail="stopped")
        log("[ArduinoUSBPolar]", "Thread stopped.")

# =====================================================
# Direct BLE Polar H10 thread (bleak — bypasses Arduino bridge)
# =====================================================

class BleakPolarThread(threading.Thread):
    """Connects directly to Polar H10 via PC Bluetooth using bleak.
    Replaces ArduinoUSBPolarThread — eliminates NINA-W102 data loss."""

    PMD_CONTROL = "FB005C81-02E7-F387-1CAD-8ACD2D8DF0C8"
    PMD_DATA    = "FB005C82-02E7-F387-1CAD-8ACD2D8DF0C8"
    HR_MEAS     = "00002a37-0000-1000-8000-00805f9b34fb"
    BATTERY     = "00002a19-0000-1000-8000-00805f9b34fb"  # Battery Level char

    ENABLE_ECG = bytes([0x02, 0x00, 0x00, 0x01, 0x82, 0x00, 0x01, 0x01, 0x0E, 0x00])
    ENABLE_ACC = bytes([0x02, 0x02, 0x00, 0x01, 0x32, 0x00, 0x01, 0x01, 0x10, 0x00, 0x02, 0x01, 0x08, 0x00])

    ECG_RATE = 130
    ACC_RATE = 50
    ECG_PERIOD_S = 1.0 / ECG_RATE
    ACC_PERIOD_S = 1.0 / ACC_RATE

    def __init__(self, address: str, label_map: Dict[str, Tuple[str, List[str], float]],
                 health: DeviceHealth, participant_id: str = "", device_name: str = "Polar"):
        super().__init__(daemon=True)
        self.address = address
        self.label_map = label_map
        self.health = health
        self.participant_id = participant_id
        self.device_name = device_name

        sid_suffix = f"{participant_id.lower()}_{_safe(device_name).lower()}" if participant_id else ""

        # LSL outlets — ECG/ACC/beat
        self.outlets: Dict[str, StreamOutlet] = {}
        for lbl, (sname, ch_labels, srate) in self.label_map.items():
            sid = f"polar_{lbl.lower()}_{sid_suffix}" if sid_suffix else f"polar_{lbl.lower()}"
            self.outlets[lbl] = make_lsl_outlet(
                stream_name=sname,
                stream_type="BIO",
                channel_labels=ch_labels,
                nominal_srate=float(srate),
                source_id=sid,
            )

        # Battery stream — irregular rate, single channel (percentage 0-100)
        batt_info = make_polar_battery_stream(participant_id or "", device_name)
        self.battery_outlet = make_lsl_outlet(
            stream_name=batt_info["name"],
            stream_type="Battery",
            channel_labels=["battery_pct"],
            nominal_srate=0.0,
            source_id=batt_info["sid"],
        )
        self._last_battery_pct: Optional[int] = None

        # ECG filter + imputer — same as ArduinoUSBPolarThread
        self._polar_ecg_filters: Dict[str, SignalFilter] = (
            {lbl: SignalFilter(ECG_FILTER_WEIGHTS) for lbl in self.label_map}
            if ENABLE_SIGNAL_FILTER else {}
        )
        # ACC filters: one per axis (ax, ay, az) per label
        self._acc_filters: Dict[str, List[SignalFilter]] = (
            {lbl: [SignalFilter(ACC_FILTER_WEIGHTS) for _ in range(3)]
             for lbl in self.label_map}
            if ENABLE_SIGNAL_FILTER else {}
        )
        _gap_s = float(IMPUTER_MAX_GAP_S) if ENABLE_ECG_IMPUTATION else 0.0
        self._ecg_imputers: Dict[str, PolarECGImputer] = {
            lbl: PolarECGImputer(max_gap_s=_gap_s) for lbl in self.label_map
        }

        # Clock calibration: Polar sensor ns → local_clock() offset (EMA)
        self._polar_off_init = False
        self._polar_off_s = 0.0  # offset such that ts_host = ts_polar_s + offset
        self._ALPHA = 0.05  # slower EMA — Polar clock is very stable

        # ACC state: timestamped ring for linear interpolation
        # Each entry: (host_ts, ax, ay, az)
        self._acc_ring: List[Tuple[float, float, float, float]] = []
        self._ACC_RING_MAX = 200  # ~4s at 50Hz

        # Stats
        self._ecg_count = 0
        self._acc_count = 0
        self._notif_count = 0
        self._printed_first = {lbl: False for lbl in self.label_map}

    def _update_clock(self, polar_ts_ns: int):
        """Calibrate Polar sensor clock (ns since boot) → pylsl.local_clock()."""
        polar_s = polar_ts_ns / 1e9
        host_now = float(local_clock())
        sample = host_now - polar_s
        if not self._polar_off_init:
            self._polar_off_s = sample
            self._polar_off_init = True
        else:
            self._polar_off_s = (1 - self._ALPHA) * self._polar_off_s + self._ALPHA * sample

    def _polar_to_host(self, polar_ts_ns: int) -> float:
        """Convert Polar sensor timestamp (ns) to host LSL time."""
        return polar_ts_ns / 1e9 + self._polar_off_s

    def _push_ecg_batch(self, payload: bytes, polar_ts_ns: int):
        """Decode ECG samples from PMD payload and push to LSL."""
        step = 3  # 24-bit signed per sample
        nsamp = len(payload) // step
        if nsamp <= 0:
            return

        self._update_clock(polar_ts_ns)

        # polar_ts_ns is timestamp of LAST sample in batch
        first_ns = polar_ts_ns - (nsamp - 1) * int(1e9 / self.ECG_RATE)

        lbl = "Sens"
        imputer = self._ecg_imputers.get(lbl)
        filt = self._polar_ecg_filters.get(lbl)
        acc_filt = self._acc_filters.get(lbl)  # list of 3 filters (ax, ay, az)
        outlet = self.outlets.get(lbl)
        if not outlet:
            return

        for i in range(nsamp):
            # Decode 24-bit signed little-endian
            raw = payload[i*3 : i*3 + 3]
            val = int.from_bytes(raw, byteorder='little', signed=False)
            if val & 0x800000:
                val |= ~0xFFFFFF  # sign extend
            ecg_uv = float(val)

            ts_ns = first_ns + i * int(1e9 / self.ECG_RATE)
            ts_host = self._polar_to_host(ts_ns)

            # Feed imputer (same as ArduinoUSBPolarThread._push_label)
            if imputer:
                raw_out = imputer.push(ts_host, ecg_uv)
                for j, (imp_ts, imp_ecg, is_beat) in enumerate(raw_out):
                    beat_val = 1.0 if is_beat else 0.0
                    is_real = (j == len(raw_out) - 1)
                    if is_real:
                        acc = self._interp_acc(imp_ts)
                        if acc_filt:
                            acc = [acc_filt[k].apply(acc[k]) for k in range(3)]
                        vals = [imp_ecg] + acc + [beat_val]
                    else:
                        vals = [imp_ecg, float('nan'), float('nan'), float('nan'), beat_val]

                    if filt:
                        vals = [filt.apply(vals[0])] + vals[1:]

                    try:
                        outlet.push_sample([float(v) for v in vals], timestamp=float(imp_ts))
                    except Exception as e:
                        log("[BleakPolar]", f"LSL push error: {e}")
            else:
                acc = self._interp_acc(ts_host)
                if acc_filt:
                    acc = [acc_filt[k].apply(acc[k]) for k in range(3)]
                vals = [ecg_uv] + acc + [0.0]
                if filt:
                    vals = [filt.apply(vals[0])] + vals[1:]
                try:
                    outlet.push_sample([float(v) for v in vals], timestamp=float(ts_host))
                except Exception as e:
                    log("[BleakPolar]", f"LSL push error: {e}")

            self._ecg_count += 1

        # Health + first-sample logging
        now = float(local_clock())
        self.health.set(last_data_at=now, first_data=True, detail="streaming")
        if not self._printed_first.get(lbl, False):
            sname = self.label_map.get(lbl, ("?",))[0]
            log("[BleakPolar]", f"First ECG data -> LSL '{sname}' ({nsamp} samples)")
            self._printed_first[lbl] = True

    def _interp_acc(self, ts_host: float) -> List[float]:
        """Linearly interpolate ACC at an ECG timestamp from the ACC ring."""
        ring = self._acc_ring
        if not ring:
            return [float('nan')] * 3
        if len(ring) == 1 or ts_host <= ring[0][0]:
            return [ring[0][1], ring[0][2], ring[0][3]]
        if ts_host >= ring[-1][0]:
            return [ring[-1][1], ring[-1][2], ring[-1][3]]
        # Binary search for bracket
        lo, hi = 0, len(ring) - 1
        while lo < hi - 1:
            mid = (lo + hi) // 2
            if ring[mid][0] <= ts_host:
                lo = mid
            else:
                hi = mid
        t0, ax0, ay0, az0 = ring[lo]
        t1, ax1, ay1, az1 = ring[hi]
        dt = t1 - t0
        if dt < 1e-9:
            return [ax1, ay1, az1]
        f = (ts_host - t0) / dt
        return [ax0 + f * (ax1 - ax0),
                ay0 + f * (ay1 - ay0),
                az0 + f * (az1 - az0)]

    def _push_acc_batch(self, payload: bytes, polar_ts_ns: int):
        """Decode ACC samples and store with timestamps for interpolation."""
        step = 6  # 3 × int16
        nsamp = len(payload) // step
        if nsamp <= 0:
            return

        self._update_clock(polar_ts_ns)
        ACC_RATE = 50.0
        first_ns = polar_ts_ns - (nsamp - 1) * int(1e9 / ACC_RATE)

        for i in range(nsamp):
            off = i * step
            ax = int.from_bytes(payload[off:off+2], 'little', signed=True)
            ay = int.from_bytes(payload[off+2:off+4], 'little', signed=True)
            az = int.from_bytes(payload[off+4:off+6], 'little', signed=True)
            ts_ns = first_ns + i * int(1e9 / ACC_RATE)
            ts_host = self._polar_to_host(ts_ns)
            self._acc_ring.append((ts_host, float(ax), float(ay), float(az)))

        # Trim ring
        if len(self._acc_ring) > self._ACC_RING_MAX:
            self._acc_ring = self._acc_ring[-self._ACC_RING_MAX:]

        self._acc_count += nsamp

    def _handle_pmd(self, sender, data: bytearray):
        """BLE notification callback for PMD data characteristic."""
        if len(data) < 10:
            return
        self._notif_count += 1
        meas_type = data[0]
        # Bytes 1-8: Polar sensor timestamp (ns, little-endian)
        polar_ts_ns = struct.unpack_from('<Q', data, 1)[0]
        # Byte 9: frame type (for ACC delta encoding detection)
        frame_type = data[9] if len(data) > 9 else 0
        payload = data[10:]

        if meas_type == 0x00:  # ECG
            self._push_ecg_batch(bytes(payload), polar_ts_ns)
        elif meas_type == 0x02:  # ACC
            # Only raw ACC (not delta) for simplicity
            if not (frame_type & 0x80):
                self._push_acc_batch(bytes(payload), polar_ts_ns)

    def _handle_battery(self, sender, data: bytearray):
        """BLE notification callback for Battery Level characteristic."""
        if len(data) >= 1:
            self._push_battery(int(data[0]))

    def _push_battery(self, pct: int):
        """Push battery percentage to LSL."""
        if pct < 0 or pct > 100:
            return
        if self._last_battery_pct == pct:
            return
        self._last_battery_pct = pct
        try:
            self.battery_outlet.push_sample([float(pct)])
            log("[BleakPolar]", f"Battery: {pct}%")
        except Exception:
            pass

    async def _run_async(self):
        """Main async loop: connect, subscribe, stream, auto-reconnect."""
        from bleak import BleakClient, BleakScanner

        while not stop_event.is_set():
            self.health.set(state="CONNECTING", detail=f"scanning for {self.address}")
            log("[BleakPolar]", f"Connecting to Polar H10 at {self.address}...")

            try:
                async with BleakClient(self.address) as client:
                    mtu = getattr(client, 'mtu_size', '?')
                    log("[BleakPolar]", f"Connected! MTU={mtu}")
                    self.health.set(state="ACTIVE", connected_at=float(local_clock()), detail="connected")

                    # Subscribe HR (keepalive) + PMD control (indications) + PMD data
                    try:
                        await client.start_notify(self.HR_MEAS, lambda s, d: None)
                    except Exception:
                        pass
                    try:
                        await client.start_notify(self.PMD_CONTROL, lambda s, d: None)
                    except Exception:
                        pass
                    await client.start_notify(self.PMD_DATA, self._handle_pmd)

                    # Read + subscribe to battery level
                    try:
                        batt_data = await client.read_gatt_char(self.BATTERY)
                        if batt_data:
                            self._push_battery(int(batt_data[0]))
                        await client.start_notify(self.BATTERY, self._handle_battery)
                    except Exception as e:
                        log("[BleakPolar]", f"Battery read failed: {e}")

                    await asyncio.sleep(1.0)

                    # Start ECG
                    await client.write_gatt_char(self.PMD_CONTROL, self.ENABLE_ECG, response=True)
                    log("[BleakPolar]", "ECG stream started (130 Hz)")

                    # Wait for first ECG data before starting ACC
                    ecg_before = self._ecg_count
                    for _ in range(50):  # up to 5s
                        await asyncio.sleep(0.1)
                        if self._ecg_count > ecg_before or not client.is_connected:
                            break

                    if not client.is_connected:
                        continue  # reconnect

                    await asyncio.sleep(1.0)

                    # Start ACC
                    try:
                        await client.write_gatt_char(self.PMD_CONTROL, self.ENABLE_ACC, response=True)
                        log("[BleakPolar]", "ACC stream started (50 Hz)")
                    except Exception as e:
                        log("[BleakPolar]", f"ACC start failed (non-critical): {e}")

                    # Stay connected — poll until disconnect or stop
                    last_stats = time.time()
                    while not stop_event.is_set() and client.is_connected:
                        await asyncio.sleep(0.1)
                        # Stats every 10s
                        now = time.time()
                        if (now - last_stats) >= 10.0:
                            elapsed = now - last_stats
                            expected = int(self.ECG_RATE * elapsed)
                            log("[BleakPolar]",
                                f"STATS: ecg={self._ecg_count}, acc={self._acc_count}, "
                                f"notif={self._notif_count}, expected~{expected}")
                            self._ecg_count = 0
                            self._acc_count = 0
                            self._notif_count = 0
                            last_stats = now

                    if not client.is_connected:
                        log("[BleakPolar]", "Disconnected from Polar H10")

            except Exception as e:
                log("[BleakPolar]", f"BLE error: {e}")

            if stop_event.is_set():
                break

            self.health.set(state="CONNECTING", detail="reconnecting in 2s...")
            log("[BleakPolar]", "Reconnecting in 2s...")
            for _ in range(20):  # 2s in 0.1s increments
                if stop_event.is_set():
                    break
                time.sleep(0.1)

        self.health.set(state="STOPPED", detail="stopped")
        log("[BleakPolar]", "Thread stopped.")

    def run(self):
        if not ENABLE_BLEAK_POLAR:
            return
        # Run async event loop in this thread
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            loop.run_until_complete(self._run_async())
        except Exception as e:
            log("[BleakPolar]", f"Fatal error: {e}")
        finally:
            loop.close()


# =====================================================
# EmotiBit thread (unchanged timing logic)
# =====================================================

class EmotiBitThread(threading.Thread):
    def __init__(self, health: DeviceHealth, participant_id: str = "",
                 serial_number: str = "", device_name: str = "EmotiBit"):
        super().__init__(daemon=True)
        self.health = health
        self.participant_id = participant_id
        self.device_name = device_name
        self._serial_number = serial_number

        # Stream names: prefixed with participant ID + device name if provided
        if participant_id:
            sn = make_emotibit_stream_names(participant_id, device_name)
        else:
            sn = {
                "imu_name": "EmotiBit_IMU", "ppg_name": "EmotiBit_PPG",
                "eda_temp_name": "EmotiBit_EDA_TEMP", "clock_name": "Clock_EmotiBit",
                "battery_name": "EmotiBit_Battery",
                "imu_sid": "emotibit_imu", "ppg_sid": "emotibit_ppg",
                "eda_temp_sid": "emotibit_eda_temp", "clock_sid": "clock_emotibit",
                "battery_sid": "emotibit_battery",
            }

        self.out_imu = make_lsl_outlet(
            stream_name=sn["imu_name"],
            stream_type="BIO",
            channel_labels=["acc_x","acc_y","acc_z","gyro_x","gyro_y","gyro_z","mag_x","mag_y","mag_z"],
            nominal_srate=0.0,
            source_id=sn["imu_sid"],
        )
        self.out_ppg = make_lsl_outlet(
            stream_name=sn["ppg_name"],
            stream_type="BIO",
            channel_labels=["ppg_0","ppg_1","ppg_2"],
            nominal_srate=0.0,
            source_id=sn["ppg_sid"],
        )
        self.out_eda_temp = make_lsl_outlet(
            stream_name=sn["eda_temp_name"],
            stream_type="BIO",
            channel_labels=["eda","temp"],
            nominal_srate=0.0,
            source_id=sn["eda_temp_sid"],
        )

        # Battery stream — irregular, single channel (percentage 0-100)
        self.battery_outlet = make_lsl_outlet(
            stream_name=sn["battery_name"],
            stream_type="Battery",
            channel_labels=["battery_pct"],
            nominal_srate=0.0,
            source_id=sn["battery_sid"],
        )
        self._last_battery_pct: Optional[float] = None

        self.clock_outlet: Optional[StreamOutlet] = None
        if EMOTIBIT_CLOCK_STREAM:
            info = StreamInfo(
                name=sn["clock_name"],
                type="CLOCK",
                channel_count=4,
                nominal_srate=0.0,
                channel_format="float32",
                source_id=sn["clock_sid"],
            )
            try:
                chns = info.desc().append_child("channels")
                for lbl in ["t_dev", "t_host", "offset_raw", "offset_smooth"]:
                    ch = chns.append_child("channel")
                    ch.append_child_value("label", lbl)
                    ch.append_child_value("type", "clock")
            except Exception:
                pass
            self.clock_outlet = StreamOutlet(info)

        self.offset_ema: Optional[float] = None
        self.connected_at: Optional[float] = None
        self.last_data_at: Optional[float] = None
        self.last_warn_at: float = 0.0
        self.last_heartbeat_at: float = 0.0
        self._printed_first_data: bool = False

        # One filter per PPG channel (ppg_0, ppg_1, ppg_2)
        self._ppg_filters = (
            [SignalFilter(PPG_FILTER_WEIGHTS) for _ in range(3)]
            if ENABLE_SIGNAL_FILTER else None
        )

        self._board = None
        self._board_id = None

        self._imu_idx = None
        self._ppg_idx = None
        self._eda_idx = None
        self._temp_idx = None
        self._ts_default = None
        self._ts_aux = None
        self._ts_anc = None

    def _update_clock(self, t_dev_last: float, host_now: float) -> float:
        raw_offset = float(t_dev_last - host_now)
        if self.offset_ema is None:
            self.offset_ema = raw_offset
        else:
            self.offset_ema = float((1.0 - EMOTIBIT_CLOCK_ALPHA) * self.offset_ema + EMOTIBIT_CLOCK_ALPHA * raw_offset)

        if self.clock_outlet is not None:
            try:
                self.clock_outlet.push_sample(
                    [float(t_dev_last), float(host_now), float(raw_offset), float(self.offset_ema)],
                    timestamp=float(host_now),
                )
            except Exception:
                pass

        return float(self.offset_ema)

    def _dev_to_host(self, t_dev: float, host_now: float) -> float:
        if self.offset_ema is None:
            return float(host_now)
        return float(t_dev - self.offset_ema)

    def _poll_battery(self):
        """Read latest battery value from BrainFlow and push to LSL if changed."""
        if self._batt_idx is None or self._batt_preset is None:
            return
        try:
            count = int(self._board.get_board_data_count(self._batt_preset))
            if count <= 0:
                return
            data = self._board.get_current_board_data(1, self._batt_preset)
            val = float(data[self._batt_idx][-1])
        except Exception:
            return
        # EmotiBit battery is already in percentage (0-100)
        pct = max(0.0, min(100.0, val))
        if self._last_battery_pct is not None and abs(pct - self._last_battery_pct) < 1.0:
            return
        self._last_battery_pct = pct
        try:
            self.battery_outlet.push_sample([pct])
            log("[EmotiBit]", f"Battery: {pct:.0f}%")
        except Exception:
            pass

    def _drain_and_push(self, preset: int, ts_row: int, indices: List[int], outlet: StreamOutlet, label: str, filters=None) -> int:
        try:
            count = int(self._board.get_board_data_count(preset))
        except Exception:
            return 0
        if count <= 0:
            return 0

        try:
            data = self._board.get_board_data(count, preset)
        except Exception:
            return 0

        try:
            import numpy as np
            data = np.asarray(data)
        except Exception:
            pass

        try:
            ts = data[ts_row]
            sig = data[indices]
        except Exception:
            return 0

        try:
            t_dev_last = float(ts[-1])
        except Exception:
            return 0

        host_now = float(local_clock())
        self._update_clock(t_dev_last, host_now)

        pushed = 0
        try:
            n_ch = len(indices)
            n_samp = int(len(ts))
            for i in range(n_samp):
                t_dev = float(ts[i])
                ts_host = self._dev_to_host(t_dev, host_now)
                vals = [float(sig[j][i]) for j in range(n_ch)]
                if filters is not None:
                    vals = [filters[j].apply(vals[j]) for j in range(min(len(filters), n_ch))]
                if ready_event.is_set():
                    outlet.push_sample(vals, timestamp=float(ts_host))
                    pushed += 1
        except Exception:
            return pushed

        now2 = float(local_clock())
        self.last_data_at = now2
        self.health.set(last_data_at=now2, first_data=True, detail="streaming")
        if not self._printed_first_data and pushed > 0:
            log("[EmotiBit]", f"FIRST DATA pushed ({label}). Streaming to LSL.")
            self._printed_first_data = True

        return pushed

    def run(self):
        if not ENABLE_EMOTIBIT:
            return

        self.health.set(state="CONNECTING", detail="importing brainflow")
        self.connected_at = float(local_clock())
        self.health.set(connected_at=self.connected_at)

        try:
            from brainflow.board_shim import BoardShim, BrainFlowInputParams, BoardIds, BrainFlowPresets
        except Exception as e:
            self.health.set(state="ERROR", fatal_error=f"brainflow import failed: {e}")
            log("[EmotiBit]", f"ERROR: BrainFlow not available ({e}). Install with: pip install brainflow")
            return

        try:
            BoardShim.enable_dev_board_logger()
        except Exception:
            pass

        try:
            params = BrainFlowInputParams()
            if EMOTIBIT_IP:
                params.ip_address = str(EMOTIBIT_IP)
            # Prefer instance serial number, fall back to global config
            sn = self._serial_number or EMOTIBIT_SERIAL_NUMBER
            if sn:
                params.serial_number = str(sn)
            try:
                params.timeout = int(EMOTIBIT_TIMEOUT)
            except Exception:
                pass

            self._board_id = BoardIds.EMOTIBIT_BOARD
            self._board = BoardShim(self._board_id, params)

            self.health.set(state="CONNECTING", detail=f"prepare_session (ip={EMOTIBIT_IP})")
            log("[EmotiBit]", f"Preparing session via BrainFlow (ip/broadcast={EMOTIBIT_IP})...")
            self._board.prepare_session()

            self.health.set(state="CONNECTING", detail="start_stream")
            self._board.start_stream(45000, "")
            self.health.set(state="ACTIVE", detail="connected (waiting data)")
            log("[EmotiBit]", "Session started. Waiting for data...")

            bid = int(self._board_id.value) if hasattr(self._board_id, "value") else int(self._board_id)

            self._imu_idx = (
                BoardShim.get_accel_channels(bid, BrainFlowPresets.DEFAULT_PRESET) +
                BoardShim.get_gyro_channels(bid, BrainFlowPresets.DEFAULT_PRESET) +
                BoardShim.get_magnetometer_channels(bid, BrainFlowPresets.DEFAULT_PRESET)
            )
            self._ppg_idx = BoardShim.get_ppg_channels(bid, BrainFlowPresets.AUXILIARY_PRESET)
            self._eda_idx = BoardShim.get_eda_channels(bid, BrainFlowPresets.ANCILLARY_PRESET)
            self._temp_idx = BoardShim.get_temperature_channels(bid, BrainFlowPresets.ANCILLARY_PRESET)
            # Battery channel (may be on any preset — try all)
            self._batt_idx = None
            self._batt_preset = None
            for preset in (BrainFlowPresets.ANCILLARY_PRESET,
                           BrainFlowPresets.AUXILIARY_PRESET,
                           BrainFlowPresets.DEFAULT_PRESET):
                try:
                    ch = BoardShim.get_battery_channel(bid, preset)
                    if ch is not None and ch >= 0:
                        self._batt_idx = ch
                        self._batt_preset = preset
                        break
                except Exception:
                    continue

            self._ts_default = BoardShim.get_timestamp_channel(bid, BrainFlowPresets.DEFAULT_PRESET)
            self._ts_aux = BoardShim.get_timestamp_channel(bid, BrainFlowPresets.AUXILIARY_PRESET)
            self._ts_anc = BoardShim.get_timestamp_channel(bid, BrainFlowPresets.ANCILLARY_PRESET)

        except Exception as e:
            self.health.set(state="ERROR", fatal_error=f"brainflow session error: {e}")
            log("[EmotiBit]", f"ERROR: cannot start session/stream ({e})")
            try:
                if self._board is not None:
                    self._board.release_session()
            except Exception:
                pass
            return

        while not stop_event.is_set():
            try:
                self._drain_and_push(
                    preset=0,
                    ts_row=int(self._ts_default),
                    indices=list(self._imu_idx),
                    outlet=self.out_imu,
                    label="IMU",
                )
                self._drain_and_push(
                    preset=1,
                    ts_row=int(self._ts_aux),
                    indices=list(self._ppg_idx),
                    outlet=self.out_ppg,
                    label="PPG",
                    filters=self._ppg_filters,
                )
                if self._eda_idx and self._temp_idx:
                    indices = [int(self._eda_idx[0]), int(self._temp_idx[0])]
                    self._drain_and_push(
                        preset=2,
                        ts_row=int(self._ts_anc),
                        indices=indices,
                        outlet=self.out_eda_temp,
                        label="EDA_TEMP",
                    )
                # Battery: poll latest value
                self._poll_battery()
            except Exception as e:
                self.health.set(state="ERROR", fatal_error=f"runtime error: {e}")
                log("[EmotiBit]", f"ERROR: runtime exception ({e})")
                break

            now = float(local_clock())

            if self.connected_at is not None and self.last_data_at is None:
                if (now - self.connected_at) >= NO_DATA_WARN_S and (now - self.last_warn_at) >= NO_DATA_REPEAT_S:
                    log("[EmotiBit]", f"WARNING: connected but NO DATA yet (>{now - self.connected_at:.1f}s).")
                    self.last_warn_at = now

            if self.last_data_at is not None:
                age = now - self.last_data_at

                if (now - self.last_heartbeat_at) >= STATUS_HEARTBEAT_S:
                    log("[EmotiBit]", f"Data flowing (last sample {age*1000:.0f} ms ago).")
                    self.last_heartbeat_at = now

                if age >= NO_DATA_WARN_S and (now - self.last_warn_at) >= NO_DATA_REPEAT_S:
                    log("[EmotiBit]", f"WARNING: NO DATA for {age:.1f}s (stream stalled?).")
                    self.last_warn_at = now

            time.sleep(float(EMOTIBIT_POLL_INTERVAL))

        try:
            if self._board is not None:
                try:
                    self._board.stop_stream()
                except Exception:
                    pass
                try:
                    self._board.release_session()
                except Exception:
                    pass
        except Exception:
            pass

        self.health.set(state="STOPPED", detail="stopped")
        log("[EmotiBit]", "Thread stopped.")

# =====================================================
# Setup wizard (PyQt5 dialogs)
# =====================================================

def _setup_qt_app():
    """Create and theme the QApplication for wizard dialogs."""
    from PyQt5 import QtWidgets as Qw, QtGui as Qg, QtCore as Qc
    app = Qw.QApplication.instance()
    if app is None:
        app = Qw.QApplication(sys.argv)
    app.setStyle("Fusion")
    # Load Montserrat
    font_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "Viewer", "fonts")
    if os.path.isdir(font_dir):
        for fn in os.listdir(font_dir):
            if fn.endswith(".ttf"):
                Qg.QFontDatabase.addApplicationFont(os.path.join(font_dir, fn))
    app.setFont(Qg.QFont("Montserrat", 10))
    # Dark palette
    pal = Qg.QPalette()
    _c = Qg.QColor
    for role, c in [
        (Qg.QPalette.Window, _c("#0f1318")),
        (Qg.QPalette.WindowText, _c("#e8ecf0")),
        (Qg.QPalette.Base, _c("#252d38")),
        (Qg.QPalette.Text, _c("#e8ecf0")),
        (Qg.QPalette.Button, _c("#1e252e")),
        (Qg.QPalette.ButtonText, _c("#e8ecf0")),
        (Qg.QPalette.Highlight, _c("#05abc4")),
        (Qg.QPalette.HighlightedText, _c("#ffffff")),
    ]:
        pal.setColor(role, c)
    app.setPalette(pal)
    return app


@dataclass
class ParticipantConfig:
    """Configuration for one participant from the setup wizard."""
    participant_id: str           # "P01", "P02", ...
    polar_enabled: bool = False
    polar_address: str = ""       # BLE MAC
    polar_name: str = ""          # friendly name
    emotibit_enabled: bool = False
    emotibit_serial: str = ""     # serial number
    emotibit_name: str = ""       # friendly name


def run_setup_wizard() -> Optional[List[ParticipantConfig]]:
    """Run the multi-participant setup wizard. Returns config list or None if cancelled."""
    from PyQt5 import QtWidgets as Qw, QtGui as Qg, QtCore as Qc

    app = _setup_qt_app()
    ACCENT = "#05abc4"
    BG_CARD = "#1e252e"
    BORDER = "#2a3340"
    GRAY = "#657179"
    TEXT = "#e8ecf0"

    btn_style = f"""
        QPushButton {{
            background: {BG_CARD}; color: {TEXT};
            border: 1px solid {BORDER}; border-radius: 6px;
            padding: 8px 20px; font-size: 13px; font-weight: 600;
        }}
        QPushButton:hover {{ border-color: {ACCENT}; color: {ACCENT}; }}
        QPushButton:pressed {{ background: {ACCENT}; color: white; }}
    """

    # ── Dialog 1: Number of participants ──
    d1 = Qw.QDialog()
    d1.setWindowTitle("BiHome — Setup")
    d1.setFixedSize(380, 220)
    l1 = Qw.QVBoxLayout(d1)
    l1.setSpacing(12)
    l1.setContentsMargins(24, 24, 24, 24)

    title = Qw.QLabel("BiHome")
    title.setStyleSheet(f"font-family: 'Montserrat Black'; font-size: 28px; color: {ACCENT};")
    l1.addWidget(title)
    l1.addWidget(Qw.QLabel("How many participants?"))

    spin = Qw.QSpinBox()
    spin.setRange(1, 6); spin.setValue(1)
    spin.setStyleSheet(f"""
        QSpinBox {{
            background: #252d38; color: {TEXT}; border: 1px solid {BORDER};
            border-radius: 6px; padding: 8px; font-size: 16px; min-width: 80px;
        }}
    """)
    l1.addWidget(spin)

    ok1 = Qw.QPushButton("Next")
    ok1.setStyleSheet(btn_style)
    ok1.clicked.connect(d1.accept)
    l1.addWidget(ok1)

    if d1.exec_() != Qw.QDialog.Accepted:
        return None
    n_participants = spin.value()

    # ── Dialog 2: Device assignment ──
    d2 = Qw.QDialog()
    d2.setWindowTitle("BiHome — Device Assignment")
    d2.setMinimumWidth(550)
    l2 = Qw.QVBoxLayout(d2)
    l2.setSpacing(12)
    l2.setContentsMargins(24, 24, 24, 24)

    title2 = Qw.QLabel("BiHome")
    title2.setStyleSheet(f"font-family: 'Montserrat Black'; font-size: 24px; color: {ACCENT};")
    l2.addWidget(title2)
    l2.addWidget(Qw.QLabel("Assign devices to each participant:"))

    grid = Qw.QGridLayout()
    grid.setSpacing(8)
    # Header
    for ci, h in enumerate(["", "Polar H10", "", "EmotiBit", ""]):
        lbl = Qw.QLabel(h)
        lbl.setStyleSheet(f"color: {GRAY}; font-size: 10px; font-weight: bold;")
        grid.addWidget(lbl, 0, ci)

    polar_names = list(KNOWN_POLAR.keys())
    emotibit_names = list(KNOWN_EMOTIBIT.keys())

    polar_cbs = []; polar_combos = []
    emo_cbs = []; emo_combos = []

    for pi in range(n_participants):
        pid = f"P{pi+1:02d}"
        lbl = Qw.QLabel(pid)
        lbl.setStyleSheet(f"font-weight: bold; font-size: 14px; color: {ACCENT};")
        grid.addWidget(lbl, pi + 1, 0)

        # Polar checkbox + combo
        pcb = Qw.QCheckBox()
        pcb.setChecked(len(polar_names) > 0)
        grid.addWidget(pcb, pi + 1, 1)
        pcombo = Qw.QComboBox()
        pcombo.addItems(polar_names if polar_names else ["(no Polar found)"])
        pcombo.setStyleSheet(f"QComboBox {{ background: #252d38; color: {TEXT}; border: 1px solid {BORDER}; border-radius: 4px; padding: 4px; }}")
        if pi < len(polar_names):
            pcombo.setCurrentIndex(pi)
        pcb.toggled.connect(pcombo.setEnabled)
        pcombo.setEnabled(pcb.isChecked())
        grid.addWidget(pcombo, pi + 1, 2)
        polar_cbs.append(pcb); polar_combos.append(pcombo)

        # EmotiBit checkbox + combo
        ecb = Qw.QCheckBox()
        ecb.setChecked(len(emotibit_names) > 0)
        grid.addWidget(ecb, pi + 1, 3)
        ecombo = Qw.QComboBox()
        ecombo.addItems(emotibit_names if emotibit_names else ["(no EmotiBit found)"])
        ecombo.setStyleSheet(f"QComboBox {{ background: #252d38; color: {TEXT}; border: 1px solid {BORDER}; border-radius: 4px; padding: 4px; }}")
        ecb.toggled.connect(ecombo.setEnabled)
        ecombo.setEnabled(ecb.isChecked())
        grid.addWidget(ecombo, pi + 1, 4)
        emo_cbs.append(ecb); emo_combos.append(ecombo)

    l2.addLayout(grid)

    ok2 = Qw.QPushButton("Connect")
    ok2.setStyleSheet(btn_style)
    ok2.clicked.connect(d2.accept)
    l2.addWidget(ok2)

    if d2.exec_() != Qw.QDialog.Accepted:
        return None

    # Build config list
    configs = []
    for pi in range(n_participants):
        pid = f"P{pi+1:02d}"
        pc = ParticipantConfig(participant_id=pid)
        if polar_cbs[pi].isChecked() and polar_names:
            pname = polar_combos[pi].currentText()
            pc.polar_enabled = True
            pc.polar_name = pname
            entry = KNOWN_POLAR.get(pname, ("", ""))
            pc.polar_address = entry[0] if isinstance(entry, tuple) else entry
        if emo_cbs[pi].isChecked() and emotibit_names:
            ename = emo_combos[pi].currentText()
            pc.emotibit_enabled = True
            pc.emotibit_name = ename
            entry = KNOWN_EMOTIBIT.get(ename, ("", ""))
            pc.emotibit_serial = entry[0] if isinstance(entry, tuple) else entry
        configs.append(pc)

    return configs


def run_connection_dialog(healths: Dict[str, DeviceHealth]) -> bool:
    """Show connection progress. Returns True when all connected, False if cancelled."""
    from PyQt5 import QtWidgets as Qw, QtCore as Qc

    app = Qw.QApplication.instance() or _setup_qt_app()
    ACCENT = "#05abc4"

    d = Qw.QDialog()
    d.setWindowTitle("BiHome — Connecting")
    d.setFixedSize(400, 200)
    lay = Qw.QVBoxLayout(d)
    lay.setContentsMargins(24, 24, 24, 24)
    lay.setSpacing(12)

    title = Qw.QLabel("BiHome")
    title.setStyleSheet(f"font-family: 'Montserrat Black'; font-size: 24px; color: {ACCENT};")
    lay.addWidget(title)

    status_lbl = Qw.QLabel("Connecting to devices...")
    status_lbl.setStyleSheet("font-size: 13px;")
    status_lbl.setWordWrap(True)
    lay.addWidget(status_lbl)

    progress = Qw.QProgressBar()
    progress.setRange(0, len(healths))
    progress.setStyleSheet(f"""
        QProgressBar {{ border: 1px solid #2a3340; border-radius: 6px; text-align: center; background: #252d38; color: white; }}
        QProgressBar::chunk {{ background: {ACCENT}; border-radius: 5px; }}
    """)
    lay.addWidget(progress)

    cancel_btn = Qw.QPushButton("Cancel")
    cancel_btn.clicked.connect(d.reject)
    lay.addWidget(cancel_btn)

    _result = [False]

    def _poll():
        connected = 0
        details = []
        for name, h in healths.items():
            state, detail, _, _, first_data, _ = h.snapshot()
            if first_data or state == "ACTIVE":
                connected += 1
                details.append(f"{name}: connected")
            elif state == "ERROR":
                details.append(f"{name}: ERROR")
            else:
                details.append(f"{name}: {detail or state}...")
        progress.setValue(connected)
        status_lbl.setText("\n".join(details))
        if connected >= len(healths):
            status_lbl.setText("All devices connected!")
            Qc.QTimer.singleShot(800, d.accept)

    timer = Qc.QTimer()
    timer.timeout.connect(_poll)
    timer.start(500)

    if d.exec_() == Qw.QDialog.Accepted:
        _result[0] = True
    timer.stop()
    return _result[0]


# =====================================================
# Main
# =====================================================

def main():
    threads: List[threading.Thread] = []
    devices: List[str] = []

    # ── Run setup wizard ──
    configs = run_setup_wizard()
    if configs is None:
        print("Setup cancelled.")
        return

    # ── Create per-participant threads ──
    all_healths: Dict[str, DeviceHealth] = {}

    for pc in configs:
        pid = pc.participant_id

        if pc.polar_enabled and pc.polar_address:
            label_map = make_polar_label_map(pid, pc.polar_name)
            h = DeviceHealth(name=f"{pid} {pc.polar_name}", enabled=True)
            all_healths[h.name] = h
            t = BleakPolarThread(address=pc.polar_address, label_map=label_map,
                                 health=h, participant_id=pid,
                                 device_name=pc.polar_name)
            t.start()
            threads.append(t)
            devices.append(h.name)
            log("[Main]", f"Started {h.name} → {pc.polar_address}")

        if pc.emotibit_enabled:
            h = DeviceHealth(name=f"{pid} {pc.emotibit_name}", enabled=True)
            all_healths[h.name] = h
            t = EmotiBitThread(health=h, participant_id=pid,
                               serial_number=pc.emotibit_serial,
                               device_name=pc.emotibit_name)
            t.start()
            threads.append(t)
            devices.append(h.name)
            log("[Main]", f"Started {h.name}")

    if not threads:
        print("No devices configured. Exiting.")
        return

    # ── Connection progress dialog ──
    connected = run_connection_dialog(all_healths)
    if not connected:
        print("Connection cancelled.")
        stop_event.set()
        return

    # ── Launch viewer ──
    viewer_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "Viewer", "lsl_viewer.py")
    viewer_proc = None
    if os.path.isfile(viewer_path):
        log("[Main]", f"Launching viewer: {viewer_path}")
        viewer_proc = subprocess.Popen([sys.executable, viewer_path])

    # ── Monitor (replaces old main loop) ──
    monitor_devices = [MonitoredDevice(health=h, thread=None) for h in all_healths.values()]
    # Find thread references
    for md in monitor_devices:
        for t in threads:
            if hasattr(t, 'health') and t.health is md.health:
                md.thread = t
                break

    mon = SystemMonitorThread(monitor_devices)
    mon.start()

    print(f"\n=== Acquisition running ({len(configs)} participants, {len(threads)} devices) ===")
    print("Active:", ", ".join(devices))
    print("Type 'quit' to stop.\n", flush=True)

    try:
        for line in sys.stdin:
            cmd = line.strip()
            if cmd.lower() == "quit":
                break
    except KeyboardInterrupt:
        pass
    finally:
        stop_event.set()
        if viewer_proc:
            try:
                viewer_proc.terminate()
            except Exception:
                pass
        time.sleep(0.5)
        print("Shutdown complete.", flush=True)

    wifi_thread: Optional[ArduinoWiFiECGThread] = None
    polar_thread: Optional[ArduinoUSBPolarThread] = None
    bleak_polar_thread: Optional[BleakPolarThread] = None
    emo_thread: Optional[EmotiBitThread] = None

    if ENABLE_ARDUINO_WIFI_ECG:
        wifi_thread = ArduinoWiFiECGThread(
            ip=ARD_WIFI_IP,
            port=ARD_WIFI_PORT,
            data_label=ARD_WIFI_DATA_LABEL,
            n_ch=ARD_WIFI_NCH,
            health=health_wifi,
        )
        wifi_thread.start()
        threads.append(wifi_thread)
        devices.append("ArduinoWiFi(ECG)")

    if ENABLE_ARDUINO_USB_POLAR:
        polar_thread = ArduinoUSBPolarThread(
            port=POLAR_SERIAL_PORT,
            baud=POLAR_SERIAL_BAUD,
            label_map=POLAR_LABEL_MAP,
            health=health_polar,
        )
        polar_thread.start()
        threads.append(polar_thread)
        devices.append("ArduinoUSB(Polar)")

    if ENABLE_BLEAK_POLAR:
        bleak_polar_thread = BleakPolarThread(
            address=POLAR_BLE_ADDRESS,
            label_map=POLAR_LABEL_MAP,
            health=health_bleak_polar,
        )
        bleak_polar_thread.start()
        threads.append(bleak_polar_thread)
        devices.append("BleakPolar(H10)")

    if ENABLE_EMOTIBIT:
        emo_thread = EmotiBitThread(health=health_emo)
        emo_thread.start()
        threads.append(emo_thread)
        devices.append("EmotiBit")

    monitor_devices: List[MonitoredDevice] = [
        MonitoredDevice(health=health_wifi,  thread=wifi_thread),
        MonitoredDevice(health=health_polar, thread=polar_thread),
        MonitoredDevice(health=health_bleak_polar, thread=bleak_polar_thread),
        MonitoredDevice(health=health_emo,   thread=emo_thread),
    ]

    mon = SystemMonitorThread(monitor_devices)
    mon.start()

    print("\n=== Acquisition starting ===", flush=True)
    print("Active devices:", ", ".join(devices) if devices else "(none)", flush=True)

    print("LSL streams created (expected):", flush=True)
    if ENABLE_ARDUINO_WIFI_ECG:
        print(" - ArduinoWiFi_ECG", flush=True)
        print(" - Clock_ArduinoWiFi_ECG", flush=True)
    if ENABLE_ARDUINO_USB_POLAR:
        for lbl, (sname, _, _) in POLAR_LABEL_MAP.items():
            print(f" - {sname} (internal label={lbl})", flush=True)
        print(" - Clock_ArduinoUSB_Polar", flush=True)
    if ENABLE_BLEAK_POLAR:
        for lbl, (sname, _, _) in POLAR_LABEL_MAP.items():
            print(f" - {sname} (BLE direct, label={lbl})", flush=True)
    if ENABLE_EMOTIBIT:
        print(" - EmotiBit_IMU", flush=True)
        print(" - EmotiBit_PPG", flush=True)
        print(" - EmotiBit_EDA_TEMP", flush=True)
        if EMOTIBIT_CLOCK_STREAM:
            print(" - Clock_EmotiBit", flush=True)

    print("\nNOTE:", flush=True)
    print("  The SYSTEM monitor will report problems until ALL enabled devices are streaming.", flush=True)

    print("\nCommands:", flush=True)
    print("  quit                      -> stop everything", flush=True)
    print("  wifi:<cmd>                -> send <cmd> to Arduino WiFi (TCP)", flush=True)
    print("  polar:<cmd>               -> send <cmd> to Arduino USB (Serial)", flush=True)
    print("  <cmd>                     -> broadcast to both Arduinos (if present)", flush=True)
    print("Examples: led_on, led_off\n", flush=True)

    try:
        for line in sys.stdin:
            cmd = line.strip()
            if not cmd:
                continue
            if cmd.lower() == "quit":
                break

            target = None
            payload = cmd
            if ":" in cmd:
                maybe_target, payload2 = cmd.split(":", 1)
                maybe_target = maybe_target.strip().lower()
                payload2 = payload2.strip()
                if maybe_target in ("wifi", "polar"):
                    target = maybe_target
                    payload = payload2

            if target is None:
                if wifi_thread and wifi_thread.sock:
                    tcp_send(wifi_thread.sock, payload)
                if polar_thread and polar_thread.ser:
                    serial_send(polar_thread.ser, payload)
            else:
                if target == "wifi" and wifi_thread and wifi_thread.sock:
                    tcp_send(wifi_thread.sock, payload)
                elif target == "polar" and polar_thread and polar_thread.ser:
                    serial_send(polar_thread.ser, payload)

            time.sleep(0.01)

    except KeyboardInterrupt:
        pass
    finally:
        stop_event.set()
        time.sleep(0.25)

        try:
            if wifi_thread and wifi_thread.sock:
                wifi_thread.sock.close()
        except Exception:
            pass
        try:
            if polar_thread and polar_thread.ser:
                polar_thread.ser.close()
        except Exception:
            pass

        print("Shutdown complete.", flush=True)

if __name__ == "__main__":
    main() 