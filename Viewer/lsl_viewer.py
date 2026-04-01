"""
BiHome LSL Viewer – timestamp-faithful multi-stream viewer.

Displays all discovered LSL streams using the *LSL timestamps* on the X-axis.
Features: auto-discovery, per-channel visibility, per-stream toggle, scrolling
time axis, per-channel Y-scale (auto/manual), live delay estimate, CSV diagnostics,
BPM and HRV computation from beat channel, beat markers on ECG.

    pip install pylsl pyqtgraph PyQt5 numpy
"""
from __future__ import annotations

import os, sys, time, threading, subprocess, signal
from collections import deque
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

import numpy as np
import pylsl
from PyQt5 import QtCore, QtGui, QtWidgets
import pyqtgraph as pg

pg.setConfigOptions(antialias=False)

# ── constants ────────────────────────────────────────────────────────────────
MAX_BUF = 50_000          # ~6 min @ 130 Hz
WIN_S = 10.0
REFRESH_MS = 50           # 20 FPS – smooth scrolling
RESOLVE_S = 1.0
PULL_CHUNK = 1024
REDISCOVER_S = 5.0
SNAPSHOT_TAIL = 5000      # max samples processed per snapshot (~38s @ 130 Hz)
Y_AXIS_WIDTH = 80         # fixed pixel width for left Y-axis labels (alignment)
BEAT_SHIFT_S = 0.40       # beat detection delay (POST_S) — shift beat channel left

# LabRecorder paths (relative to this file's directory)
_HERE = os.path.dirname(os.path.abspath(__file__))
_REC_DIR = os.path.join(os.path.dirname(_HERE), "LabRecorder")
LABRECORDER_CLI = os.path.join(_REC_DIR, "LabRecorderCLI.exe")
RECORDINGS_DIR = os.path.join(_HERE, "recordings")
COLORS = [
    "#1f77b4", "#ff7f0e", "#2ca02c", "#d62728", "#9467bd",
    "#8c564b", "#e377c2", "#7f7f7f", "#bcbd22", "#17becf",
    "#aec7e8", "#ffbb78", "#98df8a", "#ff9896", "#c5b0d5",
]


# ── ring buffer ──────────────────────────────────────────────────────────────

class RingBuffer:
    """Pre-allocated numpy circular buffer.  O(1) append, fast snapshot."""

    def __init__(self, srate: float = 0.0):
        self._ts = np.empty(MAX_BUF, dtype=np.float64)
        self._vs = np.empty(MAX_BUF, dtype=np.float64)
        self._head = 0
        self._count = 0
        self.srate = srate
        self.lock = threading.Lock()

    @property
    def size(self):
        return self._count

    def append_batch(self, ts_arr: np.ndarray, vs_arr: np.ndarray):
        """Append arrays of timestamps and values in one lock acquisition."""
        n = len(ts_arr)
        if n == 0:
            return
        with self.lock:
            h = self._head
            if h + n <= MAX_BUF:
                self._ts[h:h + n] = ts_arr
                self._vs[h:h + n] = vs_arr
            else:
                p1 = MAX_BUF - h
                self._ts[h:] = ts_arr[:p1]
                self._vs[h:] = vs_arr[:p1]
                p2 = n - p1
                self._ts[:p2] = ts_arr[p1:]
                self._vs[:p2] = vs_arr[p1:]
            self._head = (h + n) % MAX_BUF
            self._count = min(self._count + n, MAX_BUF)

    def snapshot(self, t_min: float):
        """Return cleaned (ts, vs) with NaN at gaps."""
        with self.lock:
            n = self._count
            if n == 0:
                return np.empty(0), np.empty(0)
            take = min(n, SNAPSHOT_TAIL)
            start = (self._head - take) % MAX_BUF
            if start + take <= MAX_BUF:
                ts = self._ts[start:start + take].copy()
                vs = self._vs[start:start + take].copy()
            else:
                p1 = MAX_BUF - start
                ts = np.concatenate((self._ts[start:], self._ts[:take - p1]))
                vs = np.concatenate((self._vs[start:], self._vs[:take - p1]))

        # sort (nearly ordered → mergesort is O(n))
        idx = np.argsort(ts, kind="mergesort")
        ts, vs = ts[idx], vs[idx]

        # dedup exact timestamps (keep last)
        _, ui = np.unique(ts[::-1], return_index=True)
        ui = len(ts) - 1 - ui; ui.sort()
        ts, vs = ts[ui], vs[ui]

        # collapse interleaved pairs (imputer artefact)
        if len(ts) > 2 and self.srate > 0:
            gap = 0.6 / self.srate
            dt = np.diff(ts)
            keep = np.empty(len(ts), dtype=bool)
            keep[-1] = True
            keep[:-1] = dt > gap
            ts, vs = ts[keep], vs[keep]

        # crop to window
        mask = ts >= t_min
        ts, vs = ts[mask], vs[mask]

        # insert NaN at data gaps → breaks horizontal lines
        if len(ts) > 1 and self.srate > 0:
            gap_thresh = 3.0 / self.srate
            dt = np.diff(ts)
            gaps = np.where(dt > gap_thresh)[0]
            if len(gaps) > 0:
                ins = gaps + 1
                ts = np.insert(ts, ins, ts[gaps] + 1e-9)
                vs = np.insert(vs, ins, np.nan)

        return ts, vs

    def raw_snapshot(self, t_min: float):
        """Return raw (ts, vs) without gap insertion — for beat extraction."""
        with self.lock:
            n = self._count
            if n == 0:
                return np.empty(0), np.empty(0)
            take = min(n, SNAPSHOT_TAIL)
            start = (self._head - take) % MAX_BUF
            if start + take <= MAX_BUF:
                ts = self._ts[start:start + take].copy()
                vs = self._vs[start:start + take].copy()
            else:
                p1 = MAX_BUF - start
                ts = np.concatenate((self._ts[start:], self._ts[:take - p1]))
                vs = np.concatenate((self._vs[start:], self._vs[:take - p1]))
        idx = np.argsort(ts, kind="mergesort")
        ts, vs = ts[idx], vs[idx]
        mask = ts >= t_min
        return ts[mask], vs[mask]


# ── stream state ─────────────────────────────────────────────────────────────

@dataclass
class StreamState:
    name: str
    stype: str
    srate: float
    source_id: str
    ch_labels: List[str]
    inlet: pylsl.StreamInlet
    bufs: List[RingBuffer] = field(default_factory=list)
    latest_ts: float = 0.0
    delay: float = 0.0

    def __post_init__(self):
        self.bufs = [RingBuffer(self.srate) for _ in self.ch_labels]


# ── reader thread ────────────────────────────────────────────────────────────

class Reader(threading.Thread):
    def __init__(self, st: StreamState):
        super().__init__(daemon=True)
        self.st = st
        self._stop = threading.Event()

    def run(self):
        inlet, nch = self.st.inlet, len(self.st.ch_labels)
        total = 0
        print(f"[Reader] start '{self.st.name}' ({nch} ch)")
        while not self._stop.is_set():
            try:
                samples, timestamps = inlet.pull_chunk(
                    timeout=0.05, max_samples=PULL_CHUNK)
            except Exception as e:
                print(f"[Reader] err '{self.st.name}': {e}"); break
            if not timestamps:
                continue
            now = pylsl.local_clock()
            self.st.latest_ts = timestamps[-1]
            self.st.delay = now - timestamps[-1]

            ts_np = np.array(timestamps, dtype=np.float64)
            smp_np = np.array(samples, dtype=np.float64)
            for ci in range(nch):
                self.st.bufs[ci].append_batch(ts_np, smp_np[:, ci])

            if total == 0:
                print(f"[Reader] first data '{self.st.name}': {len(timestamps)} smp, "
                      f"ts0={timestamps[0]:.3f}, now={now:.3f}, "
                      f"delta={now - timestamps[0]:.1f}s")
            total += len(timestamps)
        print(f"[Reader] stop '{self.st.name}' ({total} total)")

    def stop(self):
        self._stop.set()


# ── Y-scale widget ───────────────────────────────────────────────────────────

class YScaleWidget(QtWidgets.QWidget):
    changed = QtCore.pyqtSignal()

    def __init__(self):
        super().__init__()
        lay = QtWidgets.QHBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0); lay.setSpacing(4)
        self.auto_cb = QtWidgets.QCheckBox("Auto Y")
        self.auto_cb.setChecked(True)
        self.auto_cb.toggled.connect(self._toggle)
        lay.addWidget(self.auto_cb)
        self.mn = QtWidgets.QDoubleSpinBox(); self.mn.setRange(-1e9, 1e9)
        self.mn.setDecimals(1); self.mn.setPrefix("min "); self.mn.setValue(-1)
        self.mn.setEnabled(False); self.mn.valueChanged.connect(self.changed)
        lay.addWidget(self.mn)
        self.mx = QtWidgets.QDoubleSpinBox(); self.mx.setRange(-1e9, 1e9)
        self.mx.setDecimals(1); self.mx.setPrefix("max "); self.mx.setValue(1)
        self.mx.setEnabled(False); self.mx.valueChanged.connect(self.changed)
        lay.addWidget(self.mx)

    def _toggle(self, on):
        self.mn.setEnabled(not on); self.mx.setEnabled(not on)
        self.changed.emit()

    def is_auto(self): return self.auto_cb.isChecked()
    def manual(self): return self.mn.value(), self.mx.value()


# ── channel row ──────────────────────────────────────────────────────────────

@dataclass
class ChRow:
    skey: str
    ci: int
    label: str
    cb: QtWidgets.QCheckBox
    ys: YScaleWidget
    curve: object = None
    plot: object = None
    color: str = ""
    beat_scatter: object = None   # scatter plot for beat markers on ECG


# ── main window ──────────────────────────────────────────────────────────────

class Viewer(QtWidgets.QMainWindow):

    _new_streams = QtCore.pyqtSignal()

    def __init__(self):
        super().__init__()
        self.setWindowTitle("BiHome LSL Viewer")
        self.resize(1400, 800)

        self.streams: Dict[str, StreamState] = {}
        self.readers: List[Reader] = []
        self.rows: List[ChRow] = []
        self._prev_vis: List[bool] = []
        self._t_ref = pylsl.local_clock()
        self._color_idx = 0

        self._diag_done: set = set()
        self._diag_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "diag")

        # BPM / HRV state
        self._beat_times: deque = deque(maxlen=200)  # recent beat timestamps
        self._bpm = 0.0
        self._hrv_sdnn = 0.0
        self._hrv_rmssd = 0.0

        self._build_ui()
        self._new_streams.connect(self._on_new_streams)

        self._resolver_pending: List[StreamState] = []
        self._resolver_lock = threading.Lock()
        self._start_resolver()

        # Data recomputation interval (heavy work: snapshot, BPM, beat markers)
        self._data_interval = 5          # recompute every N frames
        self._frame_count = 0
        # Cache: per-row (x, vs) and beat overlay
        self._cached_data: Dict[int, Tuple[np.ndarray, np.ndarray]] = {}
        self._cached_beats: Optional[Tuple[np.ndarray, np.ndarray]] = None  # (bt_x, bt_y) per ECG
        self._cached_beat_map: Dict[int, Tuple[np.ndarray, np.ndarray]] = {}

        # Smooth draw cursor per stream: advances at real-time rate,
        # revealing data gradually instead of in 560ms bursts
        self._draw_cursor: Dict[str, float] = {}
        self._last_refresh_ts: float = pylsl.local_clock()

        self._timer = QtCore.QTimer()
        self._timer.timeout.connect(self._refresh)
        self._timer.start(REFRESH_MS)

        self._disco_timer = QtCore.QTimer()
        self._disco_timer.timeout.connect(self._start_resolver)
        self._disco_timer.start(int(REDISCOVER_S * 1000))

    # ── UI ────────────────────────────────────────────────────────────────

    def _build_ui(self):
        sp = QtWidgets.QSplitter(QtCore.Qt.Horizontal)
        self.setCentralWidget(sp)

        side = QtWidgets.QWidget()
        sl = QtWidgets.QVBoxLayout(side); sl.setContentsMargins(4, 4, 4, 4)

        tw = QtWidgets.QHBoxLayout()
        tw.addWidget(QtWidgets.QLabel("Window (s):"))
        self.win_spin = QtWidgets.QDoubleSpinBox()
        self.win_spin.setRange(1, 600); self.win_spin.setDecimals(1)
        self.win_spin.setValue(WIN_S); self.win_spin.setSingleStep(1)
        tw.addWidget(self.win_spin)
        sl.addLayout(tw)

        self.pause_btn = QtWidgets.QPushButton("Pause")
        self.pause_btn.setCheckable(True)
        sl.addWidget(self.pause_btn)

        ref_btn = QtWidgets.QPushButton("Re-discover streams")
        ref_btn.clicked.connect(self._start_resolver)
        sl.addWidget(ref_btn)

        # ── Recording controls ──
        sl.addWidget(self._hline())
        rec_row = QtWidgets.QHBoxLayout()
        self.rec_btn = QtWidgets.QPushButton("  REC")
        self.rec_btn.setCheckable(True)
        self.rec_btn.setStyleSheet(
            "QPushButton { font-weight: bold; }"
            "QPushButton:checked { background-color: #cc2222; color: white; }")
        self.rec_btn.clicked.connect(self._toggle_recording)
        rec_row.addWidget(self.rec_btn)
        self.rec_name = QtWidgets.QLineEdit()
        self.rec_name.setPlaceholderText("filename (default: datetime)")
        rec_row.addWidget(self.rec_name)
        sl.addLayout(rec_row)
        self.rec_status = QtWidgets.QLabel("")
        self.rec_status.setStyleSheet("font-size: 11px; color: #aaaaaa;")
        self.rec_status.setWordWrap(True)
        sl.addWidget(self.rec_status)

        # Recording state
        self._rec_proc: Optional[subprocess.Popen] = None
        self._rec_file: str = ""
        self._rec_start_time: float = 0.0

        sl.addWidget(self._hline())
        self.delay_lbl = QtWidgets.QLabel("Delays: --")
        self.delay_lbl.setWordWrap(True)
        sl.addWidget(self.delay_lbl)

        # BPM / HRV display
        sl.addWidget(self._hline())
        self.hr_lbl = QtWidgets.QLabel("")
        self.hr_lbl.setStyleSheet("font-size: 18px; font-weight: bold; color: #ff4444;")
        self.hr_lbl.setWordWrap(True)
        sl.addWidget(self.hr_lbl)
        self.hrv_lbl = QtWidgets.QLabel("")
        self.hrv_lbl.setStyleSheet("font-size: 13px; color: #aaaaaa;")
        self.hrv_lbl.setWordWrap(True)
        sl.addWidget(self.hrv_lbl)

        sl.addWidget(self._hline())

        scr = QtWidgets.QScrollArea(); scr.setWidgetResizable(True)
        self.ch_widget = QtWidgets.QWidget()
        self.ch_lay = QtWidgets.QVBoxLayout(self.ch_widget)
        self.ch_lay.setAlignment(QtCore.Qt.AlignTop)
        scr.setWidget(self.ch_widget)
        sl.addWidget(scr, stretch=1)
        sp.addWidget(side)

        self.pw = pg.GraphicsLayoutWidget()
        self.pw.setBackground("k")
        sp.addWidget(self.pw)
        sp.setStretchFactor(0, 0); sp.setStretchFactor(1, 1)
        sp.setSizes([350, 1050])

    @staticmethod
    def _hline():
        f = QtWidgets.QFrame()
        f.setFrameShape(QtWidgets.QFrame.HLine)
        f.setFrameShadow(QtWidgets.QFrame.Sunken)
        return f

    # ── background stream discovery ──────────────────────────────────────

    def _start_resolver(self):
        threading.Thread(target=self._resolve_bg, daemon=True).start()

    def _resolve_bg(self):
        try:
            infos = pylsl.resolve_streams(wait_time=RESOLVE_S)
        except Exception:
            return
        new_states = []
        for info in infos:
            key = f"{info.name()}  [{info.source_id()}]"
            with self._resolver_lock:
                if key in self.streams:
                    continue
            nch = info.channel_count()
            try:
                inlet = pylsl.StreamInlet(info, max_buflen=360,
                                          max_chunklen=0, recover=True)
                inlet.open_stream(timeout=5.0)
                fi = inlet.info()
            except Exception as e:
                print(f"[LSL] err opening {info.name()}: {e}"); continue

            labels = []
            ch = fi.desc().child("channels").child("channel")
            for i in range(nch):
                l = ch.child_value("label")
                labels.append(l if l else f"ch{i}")
                ch = ch.next_sibling()

            st = StreamState(
                name=info.name(), stype=info.type(),
                srate=info.nominal_srate(), source_id=info.source_id(),
                ch_labels=labels, inlet=inlet,
            )
            new_states.append((key, st))
            print(f"[LSL] opened '{info.name()}' ({info.type()}, "
                  f"{info.nominal_srate():.0f}Hz, {nch}ch, id={info.source_id()})")

        if new_states:
            with self._resolver_lock:
                self._resolver_pending.extend(new_states)
            self._new_streams.emit()

    def _on_new_streams(self):
        with self._resolver_lock:
            pending = list(self._resolver_pending)
            self._resolver_pending.clear()

        added = False
        for key, st in pending:
            if key in self.streams:
                continue
            self.streams[key] = st
            self._add_stream_ui(key, st)
            r = Reader(st); self.readers.append(r); r.start()
            added = True

        if added:
            self._rebuild_plots()

    def _add_stream_ui(self, key: str, st: StreamState):
        hw = QtWidgets.QWidget()
        hl = QtWidgets.QHBoxLayout(hw); hl.setContentsMargins(0, 4, 0, 0)
        lbl = QtWidgets.QLabel(f"<b>{st.name}</b> <small>({st.stype}, "
                               f"{st.srate:.0f}Hz, {len(st.ch_labels)}ch)</small>")
        lbl.setWordWrap(True)
        hl.addWidget(lbl, stretch=1)
        rec_cb = QtWidgets.QCheckBox("REC")
        rec_cb.setChecked(True)
        rec_cb.setToolTip("Include this stream in recording")
        rec_cb.setStyleSheet("QCheckBox { color: #ff6666; }")
        hl.addWidget(rec_cb)
        # Store reference for recording filter
        if not hasattr(self, '_stream_rec_cbs'):
            self._stream_rec_cbs: Dict[str, QtWidgets.QCheckBox] = {}
        self._stream_rec_cbs[key] = rec_cb
        tb = QtWidgets.QPushButton("Toggle all"); tb.setFixedWidth(80)
        hl.addWidget(tb)
        self.ch_lay.addWidget(hw)

        cbs: List[QtWidgets.QCheckBox] = []
        for ci, cl in enumerate(st.ch_labels):
            rw = QtWidgets.QWidget()
            rl = QtWidgets.QHBoxLayout(rw)
            rl.setContentsMargins(12, 0, 0, 0); rl.setSpacing(6)
            color = COLORS[self._color_idx % len(COLORS)]; self._color_idx += 1
            sw = QtWidgets.QLabel("\u25a0")
            sw.setStyleSheet(f"color:{color}; font-size:14px")
            cb = QtWidgets.QCheckBox(cl); cb.setChecked(True)
            ys = YScaleWidget()
            rl.addWidget(sw); rl.addWidget(cb); rl.addWidget(ys, stretch=1)
            self.ch_lay.addWidget(rw)
            self.rows.append(ChRow(skey=key, ci=ci,
                                   label=f"{st.name}/{cl}", cb=cb,
                                   ys=ys, color=color))
            cbs.append(cb)

        def _make_toggle(cbs=cbs):
            def _t():
                on = any(c.isChecked() for c in cbs)
                for c in cbs: c.setChecked(not on)
            return _t
        tb.clicked.connect(_make_toggle())
        self.ch_lay.addWidget(self._hline())

    # ── plot management ──────────────────────────────────────────────────

    def _rebuild_plots(self):
        self.pw.clear()
        ri = 0
        for cr in self.rows:
            if not cr.cb.isChecked():
                cr.curve = cr.plot = None; cr.beat_scatter = None; continue
            p = self.pw.addPlot(row=ri, col=0); ri += 1
            p.setLabel("left", cr.label)
            p.setLabel("bottom", "time (s)")
            p.showGrid(x=True, y=True, alpha=0.3)
            p.enableAutoRange(axis='y')
            p.disableAutoRange(axis='x')
            # Fix alignment: set fixed width for left axis
            p.getAxis('left').setWidth(Y_AXIS_WIDTH)
            pen = pg.mkPen(color=cr.color, width=1)
            cr.curve = p.plot(pen=pen, connect="finite")
            cr.curve.setClipToView(True)
            # Add beat scatter for ECG channels
            if cr.label.lower().endswith("/ecg"):
                cr.beat_scatter = pg.ScatterPlotItem(
                    pen=None, brush=pg.mkBrush(255, 60, 60, 200),
                    size=8, symbol='o')
                p.addItem(cr.beat_scatter)
            else:
                cr.beat_scatter = None
            cr.plot = p
        self._prev_vis = [r.cb.isChecked() for r in self.rows]

    # ── BPM / HRV ───────────────────────────────────────────────────────

    def _find_beat_channel(self) -> Optional[Tuple[str, int]]:
        """Find the beat channel (skey, ch_index) in streams."""
        for key, st in self.streams.items():
            for ci, cl in enumerate(st.ch_labels):
                if cl.lower() == "beat":
                    return key, ci
        return None

    def _update_hr(self, t_min: float):
        """Extract beat timestamps from the beat channel and compute BPM/HRV."""
        bc = self._find_beat_channel()
        if bc is None:
            return
        skey, ci = bc
        buf = self.streams[skey].bufs[ci]
        ts, vs = buf.raw_snapshot(t_min)
        if len(ts) < 2:
            return

        # Find samples where beat == 1.0
        beat_mask = vs > 0.5
        beat_ts = ts[beat_mask]

        if len(beat_ts) < 2:
            return

        # Update beat_times deque with new beats
        # Only add beats newer than what we have
        last_known = self._beat_times[-1] if self._beat_times else 0.0
        for bt in beat_ts:
            if bt > last_known:
                self._beat_times.append(bt)
                last_known = bt

        # Compute from recent beats (last 30s)
        now_ts = beat_ts[-1]
        recent = [t for t in self._beat_times if t > now_ts - 30.0]
        if len(recent) < 3:
            return

        rr = np.diff(recent)
        # Filter physiological range
        valid = rr[(rr >= 0.3) & (rr <= 1.8)]
        if len(valid) < 2:
            return

        mean_rr = np.mean(valid)
        self._bpm = 60.0 / mean_rr

        # HRV metrics
        self._hrv_sdnn = float(np.std(valid, ddof=1)) * 1000.0  # ms
        diffs = np.diff(valid)
        self._hrv_rmssd = float(np.sqrt(np.mean(diffs ** 2))) * 1000.0 if len(diffs) > 0 else 0.0

    # ── refresh ──────────────────────────────────────────────────────────

    def _refresh(self):
        if self.pause_btn.isChecked():
            return

        now = pylsl.local_clock()
        win = self.win_spin.value()
        t_ref = self._t_ref

        # Smooth draw cursor: advances at real-time rate between data bursts
        dt_frame = now - self._last_refresh_ts
        self._last_refresh_ts = now
        dt_frame = min(dt_frame, 0.1)  # cap to avoid jumps after pause

        stream_tend: Dict[str, float] = {}
        parts = []
        for key, st in self.streams.items():
            if st.latest_ts > 0:
                target = st.latest_ts
                cursor = self._draw_cursor.get(key, target)
                # Advance cursor at real-time rate
                cursor += dt_frame
                # Never exceed actual data we have
                cursor = min(cursor, target)
                self._draw_cursor[key] = cursor
                stream_tend[key] = cursor
                parts.append(f"{st.name}: {(now - target) * 1000:.0f}ms")

        # visibility change?
        cv = [r.cb.isChecked() for r in self.rows]
        if cv != self._prev_vis:
            self._cached_data.clear()
            self._cached_beat_map.clear()
            self._rebuild_plots(); return

        self._frame_count += 1
        recompute = (self._frame_count % self._data_interval == 0)

        # Heavy work only on recompute frames
        if recompute:
            self.delay_lbl.setText(
                "Delays: " + ("  |  ".join(parts) if parts else "--"))

            # BPM/HRV
            t_min_hr = now - 60.0
            self._update_hr(t_min_hr)
            if self._bpm > 0:
                self.hr_lbl.setText(f"HR: {self._bpm:.0f} bpm")
                self.hrv_lbl.setText(
                    f"SDNN: {self._hrv_sdnn:.1f} ms  |  RMSSD: {self._hrv_rmssd:.1f} ms")
            else:
                self.hr_lbl.setText("HR: --")
                self.hrv_lbl.setText("")

            # Recompute data for all rows
            for ri, cr in enumerate(self.rows):
                if cr.curve is None or cr.plot is None:
                    continue
                t_end = stream_tend.get(cr.skey, now)
                t_min = t_end - win
                buf = self.streams[cr.skey].bufs[cr.ci]
                ts, vs = buf.snapshot(t_min)

                if len(ts) < 2 or np.all(np.isnan(vs)):
                    self._cached_data[ri] = (np.empty(0), np.empty(0))
                    self._cached_beat_map[ri] = (np.empty(0), np.empty(0))
                    continue

                # diagnostic CSV (once per stream)
                if cr.skey not in self._diag_done and len(ts) > 50:
                    self._dump_csv(cr.skey, t_min)

                self._cached_data[ri] = (ts, vs)  # store absolute timestamps

                # Beat markers for ECG: beat=1.0 appears ~0.4s AFTER the
                # actual R-peak.  For each beat event, find the positive
                # maximum in ECG within [beat_ts - 0.5s, beat_ts].
                if cr.beat_scatter is not None:
                    bc = self._find_beat_channel()
                    if bc is not None:
                        skey_beat, ci_beat = bc
                        if skey_beat == cr.skey:
                            ecg_buf = self.streams[skey_beat].bufs[cr.ci]
                            beat_buf = self.streams[skey_beat].bufs[ci_beat]
                            e_ts, e_vs = ecg_buf.raw_snapshot(t_min - 1.0)
                            b_ts, b_vs = beat_buf.raw_snapshot(t_min)
                            beat_mask = b_vs > 0.5
                            beat_times = b_ts[beat_mask]
                            if len(beat_times) > 0 and len(e_ts) > 10:
                                dot_x, dot_y = [], []
                                for bt in beat_times:
                                    # Search for the tallest positive peak
                                    win_mask = (e_ts >= bt - 0.5) & (e_ts <= bt)
                                    if not np.any(win_mask):
                                        continue
                                    win_vs = e_vs[win_mask]
                                    win_ts = e_ts[win_mask]
                                    pk_idx = int(np.argmax(win_vs))
                                    dot_x.append(win_ts[pk_idx] - t_ref)
                                    dot_y.append(win_vs[pk_idx])
                                self._cached_beat_map[ri] = (
                                    np.array(dot_x), np.array(dot_y))
                            else:
                                self._cached_beat_map[ri] = (np.empty(0), np.empty(0))

        # Fast path: clip cached data to smoothly advancing t_end, update plots
        for ri, cr in enumerate(self.rows):
            if cr.curve is None or cr.plot is None:
                continue

            t_end = stream_tend.get(cr.skey, now)
            xl = t_end - t_ref - win
            xr = t_end - t_ref

            cached = self._cached_data.get(ri)
            if cached is None:
                continue
            ts_abs, vs_all = cached

            if len(ts_abs) == 0:
                cr.curve.setData([], [])
                if cr.beat_scatter:
                    cr.beat_scatter.setData([], [])
                continue

            # Clip right edge to t_end — reveals points gradually as
            # wall clock advances, instead of showing entire burst at once
            rmask = ts_abs <= t_end
            ts_vis = ts_abs[rmask]
            vs_vis = vs_all[rmask]

            x = ts_vis - t_ref
            # Shift beat channel left by POST_S to align with ECG spikes
            if cr.label.lower().endswith("/beat"):
                x = x - BEAT_SHIFT_S
            cr.curve.setData(x, vs_vis)
            cr.plot.setXRange(xl, xr, padding=0)

            # Beat scatter
            if cr.beat_scatter is not None:
                bt = self._cached_beat_map.get(ri)
                if bt is not None and len(bt[0]) > 0:
                    bt_x, bt_y = bt
                    vis = (bt_x >= xl) & (bt_x <= xr)
                    cr.beat_scatter.setData(bt_x[vis], bt_y[vis])
                else:
                    cr.beat_scatter.setData([], [])

            # Y range (only on recompute to avoid jitter)
            if recompute:
                if cr.ys.is_auto():
                    ymn = float(np.nanmin(vs_vis)) if len(vs_vis) > 0 else 0
                    ymx = float(np.nanmax(vs_vis)) if len(vs_vis) > 0 else 1
                    if np.isnan(ymn) or np.isnan(ymx):
                        continue
                    mg = (ymx - ymn) * 0.08
                    if mg < 1e-6:
                        mg = max(abs(ymx) * 0.1, 0.5)
                    cr.plot.setYRange(ymn - mg, ymx + mg, padding=0)
                else:
                    a, b = cr.ys.manual()
                    cr.plot.setYRange(a, b, padding=0)

    # ── LabRecorder integration ────────────────────────────────────────

    def _toggle_recording(self):
        if self.rec_btn.isChecked():
            self._start_recording()
        else:
            self._stop_recording()

    def _start_recording(self):
        if not os.path.isfile(LABRECORDER_CLI):
            self.rec_status.setText(f"ERROR: LabRecorderCLI not found at {LABRECORDER_CLI}")
            self.rec_btn.setChecked(False)
            return

        # Build filename
        name = self.rec_name.text().strip()
        if not name:
            name = time.strftime("%Y%m%d_%H%M%S")
        if not name.endswith(".xdf"):
            name += ".xdf"

        os.makedirs(RECORDINGS_DIR, exist_ok=True)
        filepath = os.path.join(RECORDINGS_DIR, name)

        # Build predicates only for streams with REC checkbox checked
        preds = []
        for key, st in self.streams.items():
            cb = self._stream_rec_cbs.get(key)
            if cb is None or cb.isChecked():
                preds.append(f"name='{st.name}'")

        if not preds:
            self.rec_status.setText("ERROR: No streams selected for recording")
            self.rec_btn.setChecked(False)
            return

        cmd = [LABRECORDER_CLI, filepath] + preds

        try:
            # CREATE_NEW_PROCESS_GROUP allows clean shutdown via CTRL_BREAK
            self._rec_proc = subprocess.Popen(
                cmd,
                creationflags=subprocess.CREATE_NEW_PROCESS_GROUP,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            self._rec_file = filepath
            self._rec_start_time = time.time()
            self.rec_btn.setText("  STOP")
            self.rec_name.setEnabled(False)
            # Freeze REC checkboxes during recording
            for cb in self._stream_rec_cbs.values():
                cb.setEnabled(False)
            self.rec_status.setText(f"Recording to: {name}")
            self.rec_status.setStyleSheet("font-size: 11px; color: #ff4444;")
            print(f"[REC] Started: {' '.join(cmd)}")
        except Exception as e:
            self.rec_status.setText(f"ERROR: {e}")
            self.rec_btn.setChecked(False)

    def _stop_recording(self):
        if self._rec_proc is not None:
            try:
                # Send CTRL_BREAK for graceful shutdown (writes XDF footer)
                self._rec_proc.send_signal(signal.CTRL_BREAK_EVENT)
                try:
                    self._rec_proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    self._rec_proc.kill()
            except Exception as e:
                print(f"[REC] Stop error: {e}")
                try:
                    self._rec_proc.kill()
                except Exception:
                    pass
            self._rec_proc = None

        elapsed = time.time() - self._rec_start_time if self._rec_start_time else 0
        fname = os.path.basename(self._rec_file)
        self.rec_btn.setText("  REC")
        self.rec_name.setEnabled(True)
        # Re-enable REC checkboxes
        for cb in self._stream_rec_cbs.values():
            cb.setEnabled(True)
        self.rec_status.setStyleSheet("font-size: 11px; color: #44cc44;")
        print(f"[REC] Stopped. File: {self._rec_file} ({elapsed:.1f}s)")

        # Export CSVs from XDF in background
        xdf_path = self._rec_file
        if os.path.isfile(xdf_path):
            self.rec_status.setText(f"Saved: {fname} ({elapsed:.0f}s) — exporting CSV...")
            threading.Thread(target=self._export_csv_from_xdf,
                             args=(xdf_path,), daemon=True).start()
        else:
            self.rec_status.setText(f"Saved: {fname} ({elapsed:.0f}s)")

    def _export_csv_from_xdf(self, xdf_path: str):
        """Export one CSV per stream from an XDF file (runs in background thread)."""
        try:
            import pyxdf
            streams, header = pyxdf.load_xdf(xdf_path)
        except ImportError:
            print("[REC] pyxdf not installed — skipping CSV export (pip install pyxdf)")
            self._update_rec_status_safe("CSV skipped (pyxdf not installed)")
            return
        except Exception as e:
            print(f"[REC] XDF load error: {e}")
            self._update_rec_status_safe(f"CSV error: {e}")
            return

        csv_dir = os.path.splitext(xdf_path)[0]  # e.g. recordings/20260401_132000/
        os.makedirs(csv_dir, exist_ok=True)

        exported = []
        for stream in streams:
            info = stream['info']
            name = info['name'][0] if isinstance(info['name'], list) else info['name']
            safe_name = name.replace(" ", "_").replace("/", "_")

            ts = stream['time_stamps']
            data = stream['time_series']

            if len(ts) == 0:
                continue

            # Get channel labels
            ch_labels = []
            try:
                ch_node = info['desc'][0]['channels'][0]['channel']
                if isinstance(ch_node, list):
                    for ch in ch_node:
                        ch_labels.append(ch['label'][0] if isinstance(ch['label'], list) else ch['label'])
                else:
                    ch_labels.append(ch_node['label'][0] if isinstance(ch_node['label'], list) else ch_node['label'])
            except Exception:
                nch = data.shape[1] if len(data.shape) > 1 else 1
                ch_labels = [f"ch{i}" for i in range(nch)]

            # Get sample rate for filename
            try:
                srate = float(info['nominal_srate'][0] if isinstance(info['nominal_srate'], list)
                              else info['nominal_srate'])
            except Exception:
                srate = len(ts) / (ts[-1] - ts[0]) if len(ts) > 1 else 0
            srate_str = f"{srate:.0f}Hz" if srate > 0 else "irr"
            csv_path = os.path.join(csv_dir, f"{safe_name}_{srate_str}.csv")
            try:
                with open(csv_path, "w") as f:
                    f.write("timestamp," + ",".join(ch_labels) + "\n")
                    for i in range(len(ts)):
                        row = [f"{ts[i]:.9f}"]
                        if len(data.shape) > 1:
                            row.extend(f"{data[i, c]}" for c in range(data.shape[1]))
                        else:
                            row.append(f"{data[i]}")
                        f.write(",".join(row) + "\n")
                exported.append(safe_name)
                print(f"[REC] CSV exported: {csv_path} ({len(ts)} samples)")
            except Exception as e:
                print(f"[REC] CSV export error for {name}: {e}")

        msg = f"XDF + {len(exported)} CSV exported"
        self._update_rec_status_safe(msg)

    def _update_rec_status_safe(self, msg: str):
        """Thread-safe status update."""
        QtCore.QMetaObject.invokeMethod(
            self.rec_status, "setText",
            QtCore.Qt.QueuedConnection,
            QtCore.Q_ARG(str, msg))

    # ── CSV diagnostic ───────────────────────────────────────────────────

    def _dump_csv(self, skey: str, t_min: float):
        self._diag_done.add(skey)
        st = self.streams[skey]
        try:
            os.makedirs(self._diag_dir, exist_ok=True)
            safe = st.name.replace(" ", "_").replace("/", "_")
            path = os.path.join(
                self._diag_dir, f"{safe}_{time.strftime('%Y%m%d_%H%M%S')}.csv")
            snaps = [b.snapshot(t_min) for b in st.bufs]
            ref_ts = max(snaps, key=lambda s: len(s[0]))[0]
            if len(ref_ts) == 0:
                return
            with open(path, "w") as f:
                f.write("timestamp," + ",".join(st.ch_labels) + "\n")
                for i in range(len(ref_ts)):
                    row = [f"{ref_ts[i]:.9f}"]
                    for ts_a, vs_a in snaps:
                        row.append(f"{vs_a[i]}" if i < len(vs_a) else "")
                    f.write(",".join(row) + "\n")
            print(f"[DIAG] saved '{st.name}' ({len(ref_ts)} smp) -> {path}")
        except Exception as e:
            print(f"[DIAG] err: {e}")

    # ── cleanup ──────────────────────────────────────────────────────────

    def closeEvent(self, ev):
        self._timer.stop(); self._disco_timer.stop()
        if self._rec_proc is not None:
            self._stop_recording()
        for r in self.readers: r.stop()
        for r in self.readers: r.join(timeout=2)
        super().closeEvent(ev)


# ── main ─────────────────────────────────────────────────────────────────────

def main():
    app = QtWidgets.QApplication(sys.argv)
    app.setStyle("Fusion")
    pal = QtGui.QPalette()
    for role, c in [
        (QtGui.QPalette.Window, (30, 30, 30)),
        (QtGui.QPalette.WindowText, (220, 220, 220)),
        (QtGui.QPalette.Base, (40, 40, 40)),
        (QtGui.QPalette.Text, (220, 220, 220)),
        (QtGui.QPalette.Button, (50, 50, 50)),
        (QtGui.QPalette.ButtonText, (220, 220, 220)),
        (QtGui.QPalette.Highlight, (42, 130, 218)),
    ]:
        pal.setColor(role, QtGui.QColor(*c))
    app.setPalette(pal)
    v = Viewer(); v.show()
    sys.exit(app.exec_())

if __name__ == "__main__":
    main()
