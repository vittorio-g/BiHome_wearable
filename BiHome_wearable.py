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

import sys
import time
import socket
import select
import threading
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

from pylsl import StreamInfo, StreamOutlet, local_clock

# =====================================================
# Feature flags
# =====================================================

ENABLE_ARDUINO_WIFI_ECG = False
ENABLE_ARDUINO_USB_POLAR = True
ENABLE_EMOTIBIT = False

# Weighted moving average on ECG (WiFi + Polar) and EmotiBit PPG.
# Set to False to stream raw unfiltered values.
ENABLE_SIGNAL_FILTER = True

# R-peak template imputation for Polar H10 ECG gaps (BLE packet loss).
# When enabled, gaps in the Polar ECG stream are filled with a synthetic signal
# built from the average beat template + estimated RR interval.
# Only gaps ≤ IMPUTER_MAX_GAP_S are imputed; longer gaps pass through as NaN.
ENABLE_ECG_IMPUTATION  = True
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

# --- Arduino MKR over USB/Serial (Polar BLE PMD) ---
POLAR_SERIAL_PORT = "COM5"
POLAR_SERIAL_BAUD = 921600

POLAR_LABEL_MAP = {
    # 'beat' is the last channel: 0 normally, 1 when an R-peak is confirmed.
    # It is computed in Python (not from firmware) — see PolarECGImputer.
    "Sens": ("PolarH10_Sens", ["ecg","ax","ay","az","beat"], 130.0),
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
                    log("[SETUP]", f"✓ {name} collegato")

                # Messaggio una-tantum in caso di errore irreversibile
                err_msg = fatal_error or (detail if state == "ERROR" else None)
                if err_msg and name not in _logged_error:
                    _logged_error.add(name)
                    log("[SETUP]", f"✗ {name} errore: {err_msg}")

                # Thread morto inaspettatamente
                if (d.thread is not None and not d.thread.is_alive()
                        and not stop_event.is_set()
                        and name not in _logged_error):
                    _logged_error.add(name)
                    log("[SETUP]", f"✗ {name} thread terminato inaspettatamente")

            # Sblocca gli stream quando tutti i dispositivi abilitati sono connessi
            if not ready_event.is_set():
                enabled = [d for d in self.devices if d.health.enabled]
                if enabled and all(d.health.name in _logged_active for d in enabled):
                    ready_event.set()
                    log("[SETUP]", "Tutti i dispositivi collegati — avvio degli stream LSL ▶")

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
    REFRACT_S   = 0.25    # min time between two accepted peaks
    AMP_FRAC    = 0.40    # beat must reach ≥ 40 % of the adaptive peak-amplitude EMA
    MIN_AMP_UV  = 150.0   # µV — fallback threshold before EMA is initialised
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
        return imputed + [(ts, val, is_beat)]

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
        win  = self._buf_v[ci - self._pre : ci + self._post + 1]
        wmin, wmax = min(win), max(win)

        # Local-max/min check over ±LOCAL_WIN samples — much less strict than
        # requiring global max of the full window, robust to T-waves and noise.
        lo = max(0, ci - self.LOCAL_WIN)
        hi = min(n - 1, ci + self.LOCAL_WIN)
        local = self._buf_v[lo : hi + 1]

        pos_amp = cval - wmin   # amplitude as positive peak
        neg_amp = wmax - cval   # amplitude as negative peak (inverted ECG)

        if pos_amp >= neg_amp:
            amp          = pos_amp
            is_local_ext = (cval == max(local))
        else:
            amp          = neg_amp
            is_local_ext = (cval == min(local))

        if not is_local_ext:
            return

        # Adaptive amplitude threshold: 40 % of recent peak EMA, or MIN_AMP_UV
        # as fallback before the EMA is initialised.
        threshold = (self._peak_amp_ema * self.AMP_FRAC
                     if self._peak_amp_ema is not None
                     else self.MIN_AMP_UV)
        if amp < threshold:
            return

        # ---- Valid R-peak ----
        peak_ts          = self._buf_t[ci]
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

def serial_read_lines(ser, buffer: str, max_bytes: int = 8192) -> Tuple[str, List[str]]:
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
            vals = [float(v) for k, v in ordered if k not in ("wrap", "us32")]
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

    def run(self):
        if not ENABLE_ARDUINO_USB_POLAR:
            return
        if not self.connect():
            return

        next_sync = 0.0
        try:
          while not stop_event.is_set():
            now = float(local_clock())
            if now >= next_sync:
                self.send_sync()
                next_sync = now + float(SYNC_INTERVAL_S)

            self.buf, lines = serial_read_lines(self.ser, self.buf)

            for line in lines:
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
                        # 'beat' is the last channel — computed in Python, not from firmware.
                        n_fw_ch = len(ch_labels) - 1
                        parsed = parse_wrapped_sample(payload, n_fw_ch)
                        if parsed is None:
                            continue
                        t_dev_s, vals = parsed
                        self._push_label(label, vals, t_dev_s)

            now3 = float(local_clock())

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

            time.sleep(0.001)

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
# EmotiBit thread (unchanged timing logic)
# =====================================================

class EmotiBitThread(threading.Thread):
    def __init__(self, health: DeviceHealth):
        super().__init__(daemon=True)
        self.health = health

        self.out_imu = make_lsl_outlet(
            stream_name="EmotiBit_IMU",
            stream_type="BIO",
            channel_labels=["acc_x","acc_y","acc_z","gyro_x","gyro_y","gyro_z","mag_x","mag_y","mag_z"],
            nominal_srate=0.0,
            source_id="emotibit_imu",
        )
        self.out_ppg = make_lsl_outlet(
            stream_name="EmotiBit_PPG",
            stream_type="BIO",
            channel_labels=["ppg_0","ppg_1","ppg_2"],
            nominal_srate=0.0,
            source_id="emotibit_ppg",
        )
        self.out_eda_temp = make_lsl_outlet(
            stream_name="EmotiBit_EDA_TEMP",
            stream_type="BIO",
            channel_labels=["eda","temp"],
            nominal_srate=0.0,
            source_id="emotibit_eda_temp",
        )

        self.clock_outlet: Optional[StreamOutlet] = None
        if EMOTIBIT_CLOCK_STREAM:
            info = StreamInfo(
                name="Clock_EmotiBit",
                type="CLOCK",
                channel_count=4,
                nominal_srate=0.0,
                channel_format="float32",
                source_id="clock_emotibit",
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
            if EMOTIBIT_SERIAL_NUMBER:
                params.serial_number = str(EMOTIBIT_SERIAL_NUMBER)
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
# Main
# =====================================================

def main():
    threads: List[threading.Thread] = []
    devices: List[str] = []

    health_wifi = DeviceHealth(name="ArduinoWiFi(ECG)", enabled=ENABLE_ARDUINO_WIFI_ECG)
    health_polar = DeviceHealth(name="ArduinoUSB(Polar)", enabled=ENABLE_ARDUINO_USB_POLAR)
    health_emo = DeviceHealth(name="EmotiBit", enabled=ENABLE_EMOTIBIT)

    wifi_thread: Optional[ArduinoWiFiECGThread] = None
    polar_thread: Optional[ArduinoUSBPolarThread] = None
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

    if ENABLE_EMOTIBIT:
        emo_thread = EmotiBitThread(health=health_emo)
        emo_thread.start()
        threads.append(emo_thread)
        devices.append("EmotiBit")

    monitor_devices: List[MonitoredDevice] = [
        MonitoredDevice(health=health_wifi,  thread=wifi_thread),
        MonitoredDevice(health=health_polar, thread=polar_thread),
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