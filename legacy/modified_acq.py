
#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Multi-device acquisition + time-sync + LSL streaming

Behavior changes in this version:
- very low-noise logging
- 1 startup message
- 1 message when each device connects
- 1 message when all enabled devices are streaming correctly
- 1 message when a device disconnects / loses connection
- automatic reconnect / retry loops for all devices
- specific error messages are still preserved
"""

import sys
import time
import socket
import select
import threading
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple, Any

from pylsl import StreamInfo, StreamOutlet, local_clock

# =====================================================
# Feature flags
# =====================================================

ENABLE_ARDUINO_WIFI_ECG = True
ENABLE_ARDUINO_USB_POLAR = True
ENABLE_EMOTIBIT = True

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
    "Sens": ("PolarH10_Sens", ["ecg", "ax", "ay", "az"], 130.0),
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
EMOTIBIT_IP = "192.168.50.163"
EMOTIBIT_TIMEOUT = 5
EMOTIBIT_POLL_INTERVAL = 0.05
EMOTIBIT_SERIAL_NUMBER = ""

EMOTIBIT_CLOCK_STREAM = True
EMOTIBIT_CLOCK_ALPHA = 0.02

# --- Reconnect / log throttling ---
RECONNECT_BACKOFF_S = 2.0
RETRY_LOG_REPEAT_S = 10.0

# --- Health thresholds ---
NO_DATA_WARN_S = 5.0

# =====================================================
# Logging
# =====================================================

def log(tag: str, msg: str) -> None:
    print(f"{time.strftime('%H:%M:%S')} {tag} {msg}", flush=True)


class RetryLogMixin:
    def __init__(self):
        self._last_retry_err_msg: Optional[str] = None
        self._last_retry_err_at: float = 0.0

    def log_retry_error(self, tag: str, msg: str) -> None:
        now = float(local_clock())
        if (msg != self._last_retry_err_msg) or ((now - self._last_retry_err_at) >= RETRY_LOG_REPEAT_S):
            log(tag, msg)
            self._last_retry_err_msg = msg
            self._last_retry_err_at = now

    def reset_retry_error(self) -> None:
        self._last_retry_err_msg = None
        self._last_retry_err_at = 0.0


# =====================================================
# Global stop
# =====================================================

stop_event = threading.Event()
send_lock = threading.Lock()
_UNSET = object()

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
        state: Any = _UNSET,
        detail: Any = _UNSET,
        connected_at: Any = _UNSET,
        last_data_at: Any = _UNSET,
        first_data: Any = _UNSET,
        fatal_error: Any = _UNSET,
    ) -> None:
        with self.lock:
            if state is not _UNSET:
                self.state = state
            if detail is not _UNSET:
                self.detail = detail
            if connected_at is not _UNSET:
                self.connected_at = connected_at
            if last_data_at is not _UNSET:
                self.last_data_at = last_data_at
            if first_data is not _UNSET:
                self.first_data = first_data
            if fatal_error is not _UNSET:
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
        self.prev_connected: Dict[str, bool] = {}
        self.prev_ready: Dict[str, bool] = {}
        self.prev_all_ok: bool = False

    @staticmethod
    def _evaluate(dev: MonitoredDevice) -> Tuple[bool, bool, str, str]:
        h = dev.health

        if not h.enabled:
            return True, True, "DISABLED", "disabled"

        state, detail, connected_at, last_data_at, _, fatal_error = h.snapshot()

        if dev.thread is None:
            return False, False, "NO_THREAD", "thread not created"
        if (not dev.thread.is_alive()) and (not stop_event.is_set()):
            return False, False, "THREAD_DEAD", "thread not alive"

        if state == "STOPPED":
            return False, False, "STOPPED", (detail or "stopped")

        if fatal_error:
            return False, False, "ERROR", fatal_error

        connected = (state == "ACTIVE") and (connected_at is not None)

        if not connected:
            if state in ("INIT", "CONNECTING"):
                return False, False, "CONNECTING", (detail or state)
            if state == "ERROR":
                return False, False, "ERROR", (detail or "error")
            return False, False, state, (detail or state)

        if last_data_at is None:
            return True, False, "WAIT_DATA", "connected, waiting first data"

        age = float(local_clock()) - float(last_data_at)
        if age >= NO_DATA_WARN_S:
            return True, False, "STALL", f"no data for {age:.1f}s"

        return True, True, "OK", "streaming"

    def run(self):
        while not stop_event.is_set():
            enabled_rows = []
            for d in self.devices:
                if not d.health.enabled:
                    continue
                connected, ready, code, msg = self._evaluate(d)
                name = d.health.name
                enabled_rows.append((name, connected, ready, code, msg))

                prev_conn = self.prev_connected.get(name, False)
                prev_ready = self.prev_ready.get(name, False)

                if connected and not prev_conn:
                    log("[SYSTEM]", f"{name} connected ✅")
                elif prev_conn and not connected:
                    log("[SYSTEM]", f"{name} disconnected ⚠️ ({msg})")
                elif prev_ready and not ready and connected and code == "STALL":
                    log("[SYSTEM]", f"{name} stalled ⚠️ ({msg})")

                self.prev_connected[name] = connected
                self.prev_ready[name] = ready

            all_ok = bool(enabled_rows) and all(ready for _, _, ready, _, _ in enabled_rows)
            if all_ok and not self.prev_all_ok:
                log("[SYSTEM]", "All enabled devices connected and streaming ✅")
            self.prev_all_ok = all_ok

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

        self.outlet = StreamOutlet(info)

    def reset(self) -> None:
        with self.lock:
            self.pending.clear()
            self.offset = None
            self.delay = None
            self._prev_offset = None

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
    return StreamOutlet(info)

# =====================================================
# TCP helpers
# =====================================================

def tcp_connect(ip: str, port: int) -> socket.socket:
    soc = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    soc.connect((ip, port))
    soc.setblocking(False)
    return soc

def tcp_send(sock: socket.socket, line: str) -> None:
    try:
        data = (line + "\n").encode("utf-8")
        with send_lock:
            sock.sendall(data)
    except OSError as e:
        log("[TCP]", f"Send error ({line!r}): {e}")

def tcp_read_lines(sock: socket.socket, buffer: str, timeout: float = 0.1) -> Tuple[str, List[str], bool, Optional[str]]:
    try:
        r, _, _ = select.select([sock], [], [], timeout)
        if not r:
            return buffer, [], False, None

        chunk = sock.recv(4096)
        if not chunk:
            return buffer, [], True, "connection closed/EOF"

        buffer += chunk.decode("utf-8", errors="replace")
        parts = buffer.split("\n")
        buffer = parts[-1]
        lines = [p.strip() for p in parts[:-1] if p.strip()]
        return buffer, lines, False, None

    except OSError as e:
        return buffer, [], True, str(e)

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
        return ser
    except Exception as e:
        return e

def serial_send(ser, line: str) -> None:
    try:
        data = (line + "\n").encode("utf-8")
        with send_lock:
            ser.write(data)
    except Exception as e:
        log("[SERIAL]", f"Send error ({line!r}): {e}")

def serial_read_lines(ser, buffer: str, max_bytes: int = 8192) -> Tuple[str, List[str], bool, Optional[str]]:
    try:
        if hasattr(ser, "is_open") and not ser.is_open:
            return buffer, [], True, "serial port closed"

        n = ser.in_waiting
        if n <= 0:
            return buffer, [], False, None

        chunk = ser.read(min(n, max_bytes))
        if not chunk:
            return buffer, [], False, None

        buffer += chunk.decode("utf-8", errors="replace")
        parts = buffer.split("\n")
        buffer = parts[-1]
        lines = [p.strip() for p in parts[:-1] if p.strip()]
        return buffer, lines, False, None
    except Exception as e:
        return buffer, [], True, str(e)

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
    "wrap,us32,val1,val2,...,valN"
    """
    try:
        parts = [p.strip() for p in value.split(",") if p.strip()]
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

class ArduinoWiFiECGThread(threading.Thread, RetryLogMixin):
    def __init__(self, ip: str, port: int, data_label: str, n_ch: int, health: DeviceHealth):
        threading.Thread.__init__(self, daemon=True)
        RetryLogMixin.__init__(self)

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
        self.last_sync_warn_at: float = 0.0

    def _close_socket(self) -> None:
        try:
            if self.sock:
                self.sock.close()
        except Exception:
            pass
        self.sock = None

    def _set_connecting(self, detail: str) -> None:
        self.health.set(
            state="CONNECTING",
            detail=detail,
            connected_at=None,
            last_data_at=None,
            first_data=False,
            fatal_error=None,
        )

    def _connect_once(self) -> bool:
        self._set_connecting(f"{self.ip}:{self.port}")
        self.sync.reset()
        self.buf = ""

        try:
            self.sock = tcp_connect(self.ip, self.port)
            self.connected_at = float(local_clock())
            self.last_data_at = None
            self.health.set(
                state="ACTIVE",
                detail="connected",
                connected_at=self.connected_at,
                last_data_at=None,
                first_data=False,
                fatal_error=None,
            )
            self.reset_retry_error()
            return True
        except Exception as e:
            self._close_socket()
            self._set_connecting(f"retrying ({e})")
            self.log_retry_error("[ArduinoWiFiECG]", f"Connect error: {e}")
            return False

    def _handle_disconnect(self, reason: str, *, log_error: bool = False) -> None:
        if log_error:
            self.log_retry_error("[ArduinoWiFiECG]", reason)
        self._close_socket()
        self.connected_at = None
        self.last_data_at = None
        self._set_connecting(f"reconnecting ({reason})")

    def send_sync(self):
        if not self.sock:
            return
        seq = self.sync.mark_request()
        tcp_send(self.sock, f"SYNC:{seq}")

    def run(self):
        if not ENABLE_ARDUINO_WIFI_ECG:
            return

        while not stop_event.is_set():
            if not self._connect_once():
                time.sleep(RECONNECT_BACKOFF_S)
                continue

            next_sync = 0.0

            while (not stop_event.is_set()) and self.sock:
                now = float(local_clock())

                if now >= next_sync:
                    self.send_sync()
                    next_sync = now + float(SYNC_INTERVAL_S)

                self.buf, lines, closed, err = tcp_read_lines(self.sock, self.buf, timeout=0.1)
                if closed:
                    self._handle_disconnect(err or "connection closed", log_error=False)
                    break

                for line in lines:
                    for label, payload in split_messages(line):
                        if label == "T":
                            tt = parse_T_payload(payload)
                            if tt is None:
                                continue
                            seq, t2, t3 = tt
                            _, sync_err = self.sync.update_from_reply(seq, t2, t3)
                            now_log = float(local_clock())
                            if sync_err is not None and (now_log - self.last_sync_warn_at) >= 5.0:
                                log("[ArduinoWiFiECG]", f"SYNC rejected: {sync_err}")
                                self.last_sync_warn_at = now_log
                            continue

                        if label == self.data_label:
                            parsed = parse_wrapped_sample(payload, self.n_ch)
                            if parsed is None:
                                continue
                            t_dev_s, vals = parsed
                            ts_host = self.sync.estimate_host_time(t_dev_s)

                            now2 = float(local_clock())
                            self.last_data_at = now2
                            self.health.set(
                                last_data_at=now2,
                                first_data=True,
                                detail="streaming",
                                fatal_error=None,
                            )

                            try:
                                self.outlet.push_sample([float(v) for v in vals], timestamp=float(ts_host))
                            except Exception as e:
                                log("[ArduinoWiFiECG]", f"LSL push error: {e}")

            if not stop_event.is_set():
                time.sleep(RECONNECT_BACKOFF_S)

        self._close_socket()
        self.health.set(state="STOPPED", detail="stopped", fatal_error=None)

# =====================================================
# Arduino USB (Polar bridge) thread
# =====================================================

class ArduinoUSBPolarThread(threading.Thread, RetryLogMixin):
    def __init__(self, port: str, baud: int, label_map: Dict[str, Tuple[str, List[str], float]], health: DeviceHealth):
        threading.Thread.__init__(self, daemon=True)
        RetryLogMixin.__init__(self)

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
        self.last_sync_warn_at: float = 0.0

    def _close_serial(self) -> None:
        try:
            if self.ser:
                self.ser.close()
        except Exception:
            pass
        self.ser = None

    def _set_connecting(self, detail: str) -> None:
        self.health.set(
            state="CONNECTING",
            detail=detail,
            connected_at=None,
            last_data_at=None,
            first_data=False,
            fatal_error=None,
        )

    def _connect_once(self) -> bool:
        self._set_connecting(f"{self.port}@{self.baud}")
        self.sync.reset()
        self.buf = ""
        self.last_line_at = None
        self.last_data_any_at = None
        self.last_data_by_label = {}

        ser_or_exc = serial_open(self.port, self.baud)
        if ser_or_exc is None:
            self._set_connecting("retrying (pyserial missing)")
            return False

        if isinstance(ser_or_exc, Exception):
            self._close_serial()
            self._set_connecting(f"retrying ({ser_or_exc})")
            self.log_retry_error("[ArduinoUSBPolar]", f"Connect error: {ser_or_exc}")
            return False

        self.ser = ser_or_exc
        self.connected_at = float(local_clock())
        self.health.set(
            state="ACTIVE",
            detail="connected",
            connected_at=self.connected_at,
            last_data_at=None,
            first_data=False,
            fatal_error=None,
        )
        self.reset_retry_error()
        return True

    def _handle_disconnect(self, reason: str, *, log_error: bool = False) -> None:
        if log_error:
            self.log_retry_error("[ArduinoUSBPolar]", reason)
        self._close_serial()
        self.connected_at = None
        self.last_line_at = None
        self.last_data_any_at = None
        self.last_data_by_label = {}
        self._set_connecting(f"reconnecting ({reason})")

    def send_sync(self):
        if not self.ser:
            return
        seq = self.sync.mark_request()
        serial_send(self.ser, f"SYNC:{seq}")

    def _push_label(self, lbl: str, values: List[float], t_dev_s: float) -> None:
        ts_host = self.sync.estimate_host_time(t_dev_s)
        try:
            self.outlets[lbl].push_sample([float(v) for v in values], timestamp=float(ts_host))
            now = float(local_clock())
            self.last_data_any_at = now
            self.last_data_by_label[lbl] = now
            self.health.set(last_data_at=now, first_data=True, detail="streaming", fatal_error=None)
        except Exception as e:
            log("[ArduinoUSBPolar]", f"LSL push error ({lbl}): {e}")

    def run(self):
        if not ENABLE_ARDUINO_USB_POLAR:
            return

        while not stop_event.is_set():
            if not self._connect_once():
                time.sleep(RECONNECT_BACKOFF_S)
                continue

            next_sync = 0.0

            while (not stop_event.is_set()) and self.ser:
                now = float(local_clock())
                if now >= next_sync:
                    self.send_sync()
                    next_sync = now + float(SYNC_INTERVAL_S)

                self.buf, lines, disconnected, err = serial_read_lines(self.ser, self.buf)
                if disconnected:
                    self._handle_disconnect(err or "serial disconnected", log_error=False)
                    break

                for line in lines:
                    self.last_line_at = float(local_clock())

                    for label, payload in split_messages(line):
                        if label == "T":
                            tt = parse_T_payload(payload)
                            if tt is None:
                                continue
                            seq, t2, t3 = tt
                            _, sync_err = self.sync.update_from_reply(seq, t2, t3)
                            now_log = float(local_clock())
                            if sync_err is not None and (now_log - self.last_sync_warn_at) >= 5.0:
                                log("[ArduinoUSBPolar]", f"SYNC rejected: {sync_err}")
                                self.last_sync_warn_at = now_log
                            continue

                        if label in self.label_map and label in self.outlets:
                            _, ch_labels, _ = self.label_map[label]
                            n_ch = len(ch_labels)
                            parsed = parse_wrapped_sample(payload, n_ch)
                            if parsed is None:
                                continue
                            t_dev_s, vals = parsed
                            self._push_label(label, vals, t_dev_s)

                time.sleep(0.001)

            if not stop_event.is_set():
                time.sleep(RECONNECT_BACKOFF_S)

        self._close_serial()
        self.health.set(state="STOPPED", detail="stopped", fatal_error=None)

# =====================================================
# EmotiBit thread
# =====================================================

class EmotiBitThread(threading.Thread, RetryLogMixin):
    def __init__(self, health: DeviceHealth):
        threading.Thread.__init__(self, daemon=True)
        RetryLogMixin.__init__(self)

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

        self._board = None
        self._board_id = None

        self._imu_idx = None
        self._ppg_idx = None
        self._eda_idx = None
        self._temp_idx = None
        self._ts_default = None
        self._ts_aux = None
        self._ts_anc = None

    def _set_connecting(self, detail: str) -> None:
        self.health.set(
            state="CONNECTING",
            detail=detail,
            connected_at=None,
            last_data_at=None,
            first_data=False,
            fatal_error=None,
        )

    def _cleanup_board(self) -> None:
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
        self._board = None
        self._board_id = None

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

    def _drain_and_push(self, preset: int, ts_row: int, indices: List[int], outlet: StreamOutlet) -> int:
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
                outlet.push_sample(vals, timestamp=float(ts_host))
                pushed += 1
        except Exception:
            return pushed

        if pushed > 0:
            now2 = float(local_clock())
            self.last_data_at = now2
            self.health.set(last_data_at=now2, first_data=True, detail="streaming", fatal_error=None)

        return pushed

    def _connect_once(self) -> bool:
        self._set_connecting(f"BrainFlow ip={EMOTIBIT_IP}")
        self._cleanup_board()
        self.offset_ema = None
        self.last_data_at = None

        try:
            from brainflow.board_shim import BoardShim, BrainFlowInputParams, BoardIds, BrainFlowPresets
        except Exception as e:
            self.log_retry_error("[EmotiBit]", f"BrainFlow import error: {e}")
            return False

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
            self._board.prepare_session()
            self._board.start_stream(45000, "")

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

            self.connected_at = float(local_clock())
            self.health.set(
                state="ACTIVE",
                detail="connected",
                connected_at=self.connected_at,
                last_data_at=None,
                first_data=False,
                fatal_error=None,
            )
            self.reset_retry_error()
            return True

        except Exception as e:
            self._cleanup_board()
            self._set_connecting(f"retrying ({e})")
            self.log_retry_error("[EmotiBit]", f"Connect/session error: {e}")
            return False

    def _handle_disconnect(self, reason: str, *, log_error: bool = False) -> None:
        if log_error:
            self.log_retry_error("[EmotiBit]", reason)
        self._cleanup_board()
        self.connected_at = None
        self.last_data_at = None
        self._set_connecting(f"reconnecting ({reason})")

    def run(self):
        if not ENABLE_EMOTIBIT:
            return

        while not stop_event.is_set():
            if not self._connect_once():
                time.sleep(RECONNECT_BACKOFF_S)
                continue

            try:
                while not stop_event.is_set():
                    self._drain_and_push(
                        preset=0,
                        ts_row=int(self._ts_default),
                        indices=list(self._imu_idx),
                        outlet=self.out_imu,
                    )
                    self._drain_and_push(
                        preset=1,
                        ts_row=int(self._ts_aux),
                        indices=list(self._ppg_idx),
                        outlet=self.out_ppg,
                    )
                    if self._eda_idx and self._temp_idx:
                        indices = [int(self._eda_idx[0]), int(self._temp_idx[0])]
                        self._drain_and_push(
                            preset=2,
                            ts_row=int(self._ts_anc),
                            indices=indices,
                            outlet=self.out_eda_temp,
                        )

                    time.sleep(float(EMOTIBIT_POLL_INTERVAL))
            except Exception as e:
                self._handle_disconnect(f"runtime error: {e}", log_error=True)

            if not stop_event.is_set() and self._board is not None:
                # if we exited the inner loop without exception, treat it as a disconnect and retry
                self._handle_disconnect("stream stopped unexpectedly", log_error=False)

            if not stop_event.is_set():
                time.sleep(RECONNECT_BACKOFF_S)

        self._cleanup_board()
        self.health.set(state="STOPPED", detail="stopped", fatal_error=None)

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

    print(f"=== Acquisition starting | enabled devices: {', '.join(devices) if devices else '(none)'} ===", flush=True)

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
