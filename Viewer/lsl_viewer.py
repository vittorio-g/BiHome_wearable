"""
BiHome LSL Viewer – timestamp-faithful multi-stream viewer.

Displays all discovered LSL streams using the *LSL timestamps* on the X-axis.
Features: auto-discovery, per-channel visibility, per-stream toggle, scrolling
time axis, per-channel Y-scale (auto/manual), live delay estimate, CSV diagnostics,
BPM and HRV computation from beat channel, beat markers on ECG.

    pip install pylsl pyqtgraph PyQt5 numpy
"""
from __future__ import annotations

import os, sys, time, json, threading, subprocess, signal
from collections import deque
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

import numpy as np
import pylsl
from PyQt5 import QtCore, QtGui, QtWidgets
import pyqtgraph as pg

pg.setConfigOptions(antialias=False)

# ── theme ────────────────────────────────────────────────────────────────────
ACCENT      = "#05abc4"     # BiHome teal
ACCENT_DIM  = "#048a9e"     # darker teal for hover/pressed
GRAY        = "#657179"     # secondary text
BG_DARK     = "#0f1318"     # main background
BG_PANEL    = "#171d24"     # sidebar panel
BG_CARD     = "#1e252e"     # card/section background
BG_INPUT    = "#252d38"     # input fields
BORDER      = "#2a3340"     # subtle borders
TEXT_PRIMARY = "#e8ecf0"    # primary text
TEXT_DIM     = "#8a939c"    # dimmed text
RED_REC     = "#e83a3a"     # recording red
GREEN_OK    = "#3acc6c"     # success green

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
_FONT_DIR = os.path.join(_HERE, "fonts")
_REC_DIR = os.path.join(os.path.dirname(_HERE), "LabRecorder")
LABRECORDER_CLI = os.path.join(_REC_DIR, "LabRecorderCLI.exe")
RECORDINGS_DIR = os.path.join(_HERE, "recordings")
SETTINGS_FILE = os.path.join(_HERE, "viewer_settings.json")
COLORS = [
    "#05abc4", "#ff7f0e", "#3acc6c", "#e83a3a", "#9467bd",
    "#f0c040", "#e377c2", "#8a939c", "#bcbd22", "#17becf",
    "#6ec8d8", "#ffbb78", "#98df8a", "#ff9896", "#c5b0d5",
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

    _SPIN_STYLE = f"""
        QDoubleSpinBox {{
            background: {BG_INPUT}; color: {TEXT_DIM};
            border: 1px solid {BORDER}; border-radius: 3px;
            padding: 2px 2px; font-size: 10px;
            min-width: 56px; max-width: 68px;
        }}
        QDoubleSpinBox:disabled {{ color: {BORDER}; background: transparent; border-color: transparent; }}
        QDoubleSpinBox::up-button, QDoubleSpinBox::down-button {{
            background: {BG_CARD}; border: none; width: 12px;
        }}
    """

    def __init__(self):
        super().__init__()
        lay = QtWidgets.QHBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0); lay.setSpacing(4)
        self.auto_cb = QtWidgets.QCheckBox()
        self.auto_cb.setToolTip("Auto Y-scale")
        self.auto_cb.setChecked(True)
        self.auto_cb.setFixedWidth(32)
        self.auto_cb.setStyleSheet(f"""
            QCheckBox {{ spacing: 0px; }}
            QCheckBox::indicator {{ width: 16px; height: 16px; }}
        """)
        self.auto_cb.toggled.connect(self._toggle)
        lay.addWidget(self.auto_cb)
        self.mn = QtWidgets.QDoubleSpinBox(); self.mn.setRange(-1e9, 1e9)
        self.mn.setDecimals(1); self.mn.setValue(-1)
        self.mn.setEnabled(False); self.mn.valueChanged.connect(self.changed)
        self.mn.setStyleSheet(self._SPIN_STYLE)
        lay.addWidget(self.mn)
        self.mx = QtWidgets.QDoubleSpinBox(); self.mx.setRange(-1e9, 1e9)
        self.mx.setDecimals(1); self.mx.setValue(1)
        self.mx.setEnabled(False); self.mx.valueChanged.connect(self.changed)
        self.mx.setStyleSheet(self._SPIN_STYLE)
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
    beat_scatter: object = None
    row_widget: object = None     # the QWidget container for this row


# ── draggable channel row ────────────────────────────────────────────────────

class DraggableChannelRow(QtWidgets.QWidget):
    """A channel row widget that supports drag & drop reordering."""

    def __init__(self, viewer, parent=None):
        super().__init__(parent)
        self._viewer = viewer
        self._ch_row = None  # set after construction
        self.setAcceptDrops(True)
        self._drag_start = None

    def mousePressEvent(self, ev):
        if ev.button() == QtCore.Qt.LeftButton:
            self._drag_start = ev.pos()
        super().mousePressEvent(ev)

    def mouseMoveEvent(self, ev):
        if (self._drag_start is not None
                and (ev.pos() - self._drag_start).manhattanLength() > 10):
            drag = QtGui.QDrag(self)
            mime = QtCore.QMimeData()
            # Store the row index
            idx = self._viewer.rows.index(self._ch_row) if self._ch_row else -1
            mime.setText(str(idx))
            drag.setMimeData(mime)
            # Semi-transparent snapshot as drag pixmap
            pix = self.grab()
            painter = QtGui.QPainter(pix)
            painter.fillRect(pix.rect(), QtGui.QColor(0, 0, 0, 100))
            painter.end()
            drag.setPixmap(pix)
            drag.setHotSpot(ev.pos())
            self.setCursor(QtCore.Qt.ClosedHandCursor)
            drag.exec_(QtCore.Qt.MoveAction)
            self.setCursor(QtCore.Qt.ArrowCursor)
            self._drag_start = None
        super().mouseMoveEvent(ev)

    def mouseReleaseEvent(self, ev):
        self._drag_start = None
        super().mouseReleaseEvent(ev)

    def dragEnterEvent(self, ev):
        if ev.mimeData().hasText():
            ev.acceptProposedAction()
            self.setStyleSheet(f"border-top: 2px solid {ACCENT};")

    def dragLeaveEvent(self, ev):
        self.setStyleSheet("")

    def dropEvent(self, ev):
        self.setStyleSheet("")
        src_idx = int(ev.mimeData().text())
        if self._ch_row is None:
            return
        dst_idx = self._viewer.rows.index(self._ch_row)
        if src_idx == dst_idx or src_idx < 0:
            return
        # Move the row in the list
        row = self._viewer.rows.pop(src_idx)
        self._viewer.rows.insert(dst_idx, row)
        self._viewer._rebuild_channel_layout()
        self._viewer._rebuild_plots()


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
        self._load_settings()
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
        sp.setHandleWidth(1)
        self.setCentralWidget(sp)

        side = QtWidgets.QWidget()
        side.setObjectName("sidebar")
        side.setStyleSheet(f"#sidebar {{ background: {BG_PANEL}; }}")
        sl = QtWidgets.QVBoxLayout(side)
        sl.setContentsMargins(16, 16, 16, 12)
        sl.setSpacing(8)

        # ── Logo / Title ──
        logo = QtWidgets.QLabel("BiHome")
        logo.setStyleSheet(f"""
            font-family: 'Montserrat Black', 'Montserrat', sans-serif;
            font-size: 28px; font-weight: 900;
            color: {ACCENT}; letter-spacing: 1px;
            padding-bottom: 2px;
        """)
        sl.addWidget(logo)
        sub = QtWidgets.QLabel("LSL Stream Viewer")
        sub.setStyleSheet(f"font-size: 11px; color: {GRAY}; padding-bottom: 6px;")
        sl.addWidget(sub)
        sl.addWidget(self._sep())

        # ── Controls row ──
        ctrl = QtWidgets.QHBoxLayout(); ctrl.setSpacing(6)
        self.pause_btn = QtWidgets.QPushButton("Pause")
        self.pause_btn.setCheckable(True)
        self.pause_btn.toggled.connect(self._on_pause_toggled)
        self._style_btn(self.pause_btn)
        ctrl.addWidget(self.pause_btn)

        ref_btn = QtWidgets.QPushButton("Refresh")
        self._style_btn(ref_btn)
        ref_btn.clicked.connect(self._on_refresh)
        ctrl.addWidget(ref_btn)
        sl.addLayout(ctrl)

        # Window spinner
        tw = QtWidgets.QHBoxLayout(); tw.setSpacing(6)
        wl = QtWidgets.QLabel("Window")
        wl.setStyleSheet(f"color: {TEXT_DIM}; font-size: 11px;")
        tw.addWidget(wl)
        self.win_spin = QtWidgets.QDoubleSpinBox()
        self.win_spin.setRange(1, 600); self.win_spin.setDecimals(1)
        self.win_spin.setValue(WIN_S); self.win_spin.setSingleStep(1)
        self.win_spin.setSuffix(" s")
        self.win_spin.setStyleSheet(f"""
            QDoubleSpinBox {{
                background: {BG_INPUT}; color: {TEXT_PRIMARY};
                border: 1px solid {BORDER}; border-radius: 4px;
                padding: 3px 6px; font-size: 12px;
            }}
            QDoubleSpinBox::up-button, QDoubleSpinBox::down-button {{
                background: {BG_CARD}; border: none; width: 16px;
            }}
        """)
        tw.addWidget(self.win_spin)
        sl.addLayout(tw)

        sl.addWidget(self._sep())

        # ── Recording section ──
        sec_rec = QtWidgets.QLabel("RECORDING")
        sec_rec.setStyleSheet(f"color: {GRAY}; font-size: 10px; font-weight: bold; letter-spacing: 2px;")
        sl.addWidget(sec_rec)

        rec_row = QtWidgets.QHBoxLayout(); rec_row.setSpacing(6)
        self.rec_btn = QtWidgets.QPushButton("  REC")
        self.rec_btn.setCheckable(True)
        self.rec_btn.setStyleSheet(f"""
            QPushButton {{
                background: {BG_CARD}; color: {TEXT_PRIMARY};
                border: 1px solid {BORDER}; border-radius: 4px;
                padding: 5px 12px; font-weight: bold; font-size: 12px;
            }}
            QPushButton:hover {{ border-color: {RED_REC}; }}
            QPushButton:checked {{
                background: {RED_REC}; color: white; border-color: {RED_REC};
            }}
        """)
        self.rec_btn.clicked.connect(self._toggle_recording)
        rec_row.addWidget(self.rec_btn)
        self.rec_name = QtWidgets.QLineEdit()
        self.rec_name.setPlaceholderText("filename (datetime)")
        self.rec_name.setStyleSheet(f"""
            QLineEdit {{
                background: {BG_INPUT}; color: {TEXT_PRIMARY};
                border: 1px solid {BORDER}; border-radius: 4px;
                padding: 5px 8px; font-size: 12px;
            }}
            QLineEdit:focus {{ border-color: {ACCENT}; }}
        """)
        rec_row.addWidget(self.rec_name)
        sl.addLayout(rec_row)
        self.rec_status = QtWidgets.QLabel("")
        self.rec_status.setStyleSheet(f"font-size: 10px; color: {TEXT_DIM};")
        self.rec_status.setWordWrap(True)
        sl.addWidget(self.rec_status)

        # Recording state
        self._rec_proc: Optional[subprocess.Popen] = None
        self._rec_file: str = ""
        self._rec_start_time: float = 0.0

        sl.addWidget(self._sep())

        # ── Vitals ──
        self.hr_lbl = QtWidgets.QLabel("")
        self.hr_lbl.setStyleSheet(f"""
            font-family: 'Montserrat Bold', 'Montserrat', sans-serif;
            font-size: 26px; font-weight: bold; color: {ACCENT};
        """)
        sl.addWidget(self.hr_lbl)
        self.hrv_lbl = QtWidgets.QLabel("")
        self.hrv_lbl.setStyleSheet(f"font-size: 11px; color: {TEXT_DIM};")
        self.hrv_lbl.setWordWrap(True)
        sl.addWidget(self.hrv_lbl)

        self.delay_lbl = QtWidgets.QLabel("Delays: --")
        self.delay_lbl.setStyleSheet(f"font-size: 10px; color: {GRAY};")
        self.delay_lbl.setWordWrap(True)
        sl.addWidget(self.delay_lbl)

        sl.addWidget(self._sep())

        # ── Streams / Channels section ──
        sec_ch = QtWidgets.QLabel("STREAMS")
        sec_ch.setStyleSheet(f"color: {GRAY}; font-size: 10px; font-weight: bold; letter-spacing: 2px;")
        sl.addWidget(sec_ch)

        # Toggle All button at the top, always visible
        self.toggle_all_btn = QtWidgets.QPushButton("Toggle All Channels")
        self._style_btn(self.toggle_all_btn, small=True)
        self.toggle_all_btn.clicked.connect(self._toggle_all_channels)
        sl.addWidget(self.toggle_all_btn)

        scr = QtWidgets.QScrollArea()
        scr.setWidgetResizable(True)
        scr.setStyleSheet(f"""
            QScrollArea {{ border: none; background: transparent; }}
            QScrollBar:vertical {{
                background: {BG_PANEL}; width: 6px; margin: 0;
            }}
            QScrollBar::handle:vertical {{
                background: {BORDER}; border-radius: 3px; min-height: 20px;
            }}
            QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{ height: 0; }}
        """)
        self.ch_widget = QtWidgets.QWidget()
        self.ch_lay = QtWidgets.QVBoxLayout(self.ch_widget)
        self.ch_lay.setAlignment(QtCore.Qt.AlignTop)
        self.ch_lay.setSpacing(2)
        self.ch_lay.setContentsMargins(0, 0, 0, 0)
        scr.setWidget(self.ch_widget)
        sl.addWidget(scr, stretch=1)
        sp.addWidget(side)

        self.pw = pg.GraphicsLayoutWidget()
        self.pw.setBackground(BG_DARK)
        sp.addWidget(self.pw)
        sp.setStretchFactor(0, 0); sp.setStretchFactor(1, 1)
        sp.setSizes([320, 1080])

    def _toggle_all_channels(self):
        """Toggle visibility of all channels across all streams."""
        if not self.rows:
            return
        on = any(r.cb.isChecked() for r in self.rows)
        for r in self.rows:
            r.cb.setChecked(not on)

    def _jump_to_live(self):
        """Reset draw cursors to latest data — jumps to live edge."""
        self._draw_cursor.clear()
        self._cached_data.clear()
        self._cached_beat_map.clear()
        self._frame_count = 0  # force recompute on next frame

    def _on_pause_toggled(self, checked):
        if not checked:
            # Unpaused → jump to live
            self._jump_to_live()

    def _on_refresh(self):
        """Re-discover streams AND jump to live."""
        self._start_resolver()
        self._jump_to_live()

    def _style_btn(self, btn, small=False):
        """Apply consistent button styling."""
        pad = "3px 8px" if small else "5px 12px"
        fs = "11px" if small else "12px"
        btn.setStyleSheet(f"""
            QPushButton {{
                background: {BG_CARD}; color: {TEXT_PRIMARY};
                border: 1px solid {BORDER}; border-radius: 4px;
                padding: {pad}; font-size: {fs};
            }}
            QPushButton:hover {{ border-color: {ACCENT}; color: {ACCENT}; }}
            QPushButton:pressed {{ background: {ACCENT_DIM}; color: white; }}
            QPushButton:checked {{ background: {ACCENT}; color: white; border-color: {ACCENT}; }}
        """)

    @staticmethod
    def _sep():
        """Subtle separator line."""
        f = QtWidgets.QFrame()
        f.setFrameShape(QtWidgets.QFrame.HLine)
        f.setFixedHeight(1)
        f.setStyleSheet(f"background: {BORDER}; border: none;")
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
            self._apply_stream_settings()
            self._rebuild_plots()

    def _add_stream_ui(self, key: str, st: StreamState):
        # Stream header card
        hw = QtWidgets.QWidget()
        hw.setStyleSheet(f"QWidget {{ background: {BG_CARD}; border-radius: 6px; }}")
        hl = QtWidgets.QHBoxLayout(hw)
        hl.setContentsMargins(10, 6, 10, 6)
        lbl = QtWidgets.QLabel(
            f"<span style='color:{TEXT_PRIMARY}; font-weight:600;'>{st.name}</span>"
            f"<br><span style='color:{GRAY}; font-size:10px;'>"
            f"{st.stype} | {st.srate:.0f} Hz | {len(st.ch_labels)} ch</span>")
        hl.addWidget(lbl, stretch=1)
        rec_cb = QtWidgets.QCheckBox("REC")
        rec_cb.setChecked(True)
        rec_cb.setToolTip("Include this stream in recording")
        rec_cb.setStyleSheet(f"""
            QCheckBox {{ color: {RED_REC}; font-size: 11px; font-weight: bold; }}
            QCheckBox::indicator {{ width: 14px; height: 14px; }}
        """)
        hl.addWidget(rec_cb)
        if not hasattr(self, '_stream_rec_cbs'):
            self._stream_rec_cbs: Dict[str, QtWidgets.QCheckBox] = {}
        self._stream_rec_cbs[key] = rec_cb
        self.ch_lay.addWidget(hw)

        # Column headers (inside scroll area, moves with content)
        hdr = QtWidgets.QWidget()
        hdr_l = QtWidgets.QHBoxLayout(hdr)
        hdr_l.setContentsMargins(24, 4, 4, 0); hdr_l.setSpacing(4)
        self._hdr_ch_lbl = QtWidgets.QLabel("channel")
        self._hdr_ch_lbl.setStyleSheet(f"color: {GRAY}; font-size: 9px;")
        self._hdr_ch_lbl.setMinimumWidth(90)
        hdr_l.addWidget(self._hdr_ch_lbl)
        for h in ("auto", "min", "max"):
            hl2 = QtWidgets.QLabel(h)
            hl2.setStyleSheet(f"color: {GRAY}; font-size: 9px;")
            hl2.setAlignment(QtCore.Qt.AlignCenter)
            hl2.setFixedWidth(32 if h == "auto" else 56)
            hdr_l.addWidget(hl2)
        hdr_l.addStretch()
        self.ch_lay.addWidget(hdr)

        # Channel rows — drag handle + colored toggle button + auto/min/max
        cbs: List[QtWidgets.QCheckBox] = []
        ch_btns: List[QtWidgets.QPushButton] = []

        for ci, cl in enumerate(st.ch_labels):
            rw = DraggableChannelRow(viewer=self)
            rl = QtWidgets.QHBoxLayout(rw)
            rl.setContentsMargins(0, 1, 4, 1); rl.setSpacing(4)
            color = COLORS[self._color_idx % len(COLORS)]; self._color_idx += 1

            # Drag handle
            grip = QtWidgets.QLabel("\u2630")  # ☰ hamburger
            grip.setStyleSheet(f"""
                color: {GRAY}; font-size: 12px;
                padding: 0 4px; min-width: 16px;
            """)
            grip.setCursor(QtGui.QCursor(QtCore.Qt.OpenHandCursor))
            rl.addWidget(grip)

            # Toggle button
            btn = QtWidgets.QPushButton(cl)
            btn.setCheckable(True)
            btn.setChecked(True)
            btn.setMinimumWidth(90)
            r, g, b = int(color[1:3], 16), int(color[3:5], 16), int(color[5:7], 16)
            btn.setStyleSheet(f"""
                QPushButton {{
                    background: rgba({r},{g},{b},0.12); color: {color};
                    border: 1px solid {color}; border-radius: 4px;
                    padding: 4px 10px; font-size: 11px; font-weight: 600;
                    text-align: left;
                }}
                QPushButton:hover {{ background: rgba({r},{g},{b},0.22); }}
                QPushButton:!checked {{
                    background: {BG_INPUT}; color: {GRAY};
                    border: 1px solid {BORDER};
                }}
                QPushButton:!checked:hover {{
                    border-color: {color}; color: {color};
                    background: rgba({r},{g},{b},0.08);
                }}
            """)
            cb = QtWidgets.QCheckBox()
            cb.setChecked(True); cb.setVisible(False)
            btn.toggled.connect(cb.setChecked)
            cb.toggled.connect(btn.setChecked)
            rl.addWidget(btn)
            ch_btns.append(btn)

            ys = YScaleWidget()
            rl.addWidget(ys)
            self.ch_lay.addWidget(rw)

            row_idx = len(self.rows)
            cr = ChRow(skey=key, ci=ci, label=f"{st.name}/{cl}", cb=cb,
                       ys=ys, color=color, row_widget=rw)
            self.rows.append(cr)
            rw._ch_row = cr  # back-reference for drag & drop
            cbs.append(cb)

        QtCore.QTimer.singleShot(0, lambda btns=ch_btns: self._equalize_btn_widths(btns))

    @staticmethod
    def _equalize_btn_widths(btns):
        if not btns:
            return
        max_w = max(b.sizeHint().width() for b in btns)
        max_w = max(max_w, 80)
        for b in btns:
            b.setFixedWidth(max_w)

    def _rebuild_channel_layout(self):
        """Reorder channel row widgets in the layout to match self.rows order."""
        # Detach all widgets from layout (without destroying them)
        detached = []
        while self.ch_lay.count():
            item = self.ch_lay.takeAt(0)
            w = item.widget()
            if w:
                w.setParent(None)
                detached.append(w)

        # Re-add: stream headers + column headers + rows in self.rows order
        seen_streams = set()
        # Collect non-row widgets (headers) by stream key
        # We stored them in detached but can't easily identify them,
        # so we recreate the lightweight headers
        for cr in self.rows:
            skey = cr.skey
            if skey not in seen_streams:
                seen_streams.add(skey)
                st = self.streams[skey]
                # Stream header
                hw = QtWidgets.QWidget()
                hw.setStyleSheet(f"QWidget {{ background: {BG_CARD}; border-radius: 6px; }}")
                hl = QtWidgets.QHBoxLayout(hw)
                hl.setContentsMargins(10, 6, 10, 6)
                lbl = QtWidgets.QLabel(
                    f"<span style='color:{TEXT_PRIMARY}; font-weight:600;'>{st.name}</span>"
                    f"<br><span style='color:{GRAY}; font-size:10px;'>"
                    f"{st.stype} | {st.srate:.0f} Hz | {len(st.ch_labels)} ch</span>")
                hl.addWidget(lbl, stretch=1)
                rec_cb = self._stream_rec_cbs.get(skey)
                if rec_cb:
                    rec_cb.setParent(None)
                    hl.addWidget(rec_cb)
                self.ch_lay.addWidget(hw)
                # Column header
                hdr = QtWidgets.QWidget()
                hdr_l = QtWidgets.QHBoxLayout(hdr)
                hdr_l.setContentsMargins(22, 4, 4, 0); hdr_l.setSpacing(4)
                hdr_ch = QtWidgets.QLabel("channel")
                hdr_ch.setStyleSheet(f"color: {GRAY}; font-size: 9px;")
                hdr_ch.setMinimumWidth(90)
                hdr_l.addWidget(hdr_ch)
                for h in ("auto", "min", "max"):
                    hl2 = QtWidgets.QLabel(h)
                    hl2.setStyleSheet(f"color: {GRAY}; font-size: 9px;")
                    hl2.setAlignment(QtCore.Qt.AlignCenter)
                    hl2.setFixedWidth(32 if h == "auto" else 56)
                    hdr_l.addWidget(hl2)
                hdr_l.addStretch()
                self.ch_lay.addWidget(hdr)

            # Re-add the channel row widget
            if cr.row_widget:
                cr.row_widget.setParent(None)  # detach first
                self.ch_lay.addWidget(cr.row_widget)

        # Clean up old detached widgets (headers that won't be reused)
        for w in detached:
            if w.parent() is None:
                w.deleteLater()

    # ── plot management ──────────────────────────────────────────────────

    def _rebuild_plots(self):
        self.pw.clear()
        ri = 0
        for cr in self.rows:
            if not cr.cb.isChecked():
                cr.curve = cr.plot = None; cr.beat_scatter = None; continue
            p = self.pw.addPlot(row=ri, col=0); ri += 1

            # Themed axis styling
            for axis_name in ('left', 'bottom'):
                ax = p.getAxis(axis_name)
                ax.setPen(pg.mkPen(color=BORDER, width=1))
                ax.setTextPen(pg.mkPen(color=GRAY))
                ax.setStyle(tickFont=QtGui.QFont("Montserrat", 8))

            # Short label (just channel name, not full stream/channel)
            short = cr.label.split("/")[-1] if "/" in cr.label else cr.label
            p.setLabel("left", short, color=GRAY, **{"font-size": "10px"})
            p.setLabel("bottom", "time (s)", color=GRAY, **{"font-size": "9px"})
            p.showGrid(x=True, y=True, alpha=0.08)
            p.enableAutoRange(axis='y')
            p.disableAutoRange(axis='x')
            p.getAxis('left').setWidth(Y_AXIS_WIDTH)
            pen = pg.mkPen(color=cr.color, width=1.2)
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
                                    # Search for the sharpest positive peak (highest derivative)
                                    # in [bt-0.5, bt] — R-peaks have much steeper slopes than T-waves
                                    win_mask = (e_ts >= bt - 0.5) & (e_ts <= bt)
                                    if np.sum(win_mask) < 4:
                                        continue
                                    win_vs = e_vs[win_mask]
                                    win_ts = e_ts[win_mask]
                                    # Compute absolute derivative
                                    dv = np.abs(np.diff(win_vs))
                                    # Find the steepest point, then pick the local max around it
                                    steep_idx = int(np.argmax(dv))
                                    # The R-peak is at or just after the steepest upslope
                                    # Search a small window around steep_idx for the max value
                                    lo = max(0, steep_idx - 2)
                                    hi = min(len(win_vs) - 1, steep_idx + 4)
                                    local_pk = lo + int(np.argmax(win_vs[lo:hi+1]))
                                    dot_x.append(win_ts[local_pk] - t_ref)
                                    dot_y.append(win_vs[local_pk])
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
            self.rec_status.setStyleSheet(f"font-size: 10px; color: {RED_REC};")
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
        self.rec_status.setStyleSheet(f"font-size: 10px; color: {GREEN_OK};")
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

    # ── settings persistence ────────────────────────────────────────────

    def _save_settings(self):
        """Save current UI state to JSON."""
        # Channel visibility: map label → checked
        ch_vis = {}
        for cr in self.rows:
            ch_vis[cr.label] = cr.cb.isChecked()

        # Y-scale per channel
        ch_yscale = {}
        for cr in self.rows:
            if cr.ys.is_auto():
                ch_yscale[cr.label] = {"auto": True}
            else:
                a, b = cr.ys.manual()
                ch_yscale[cr.label] = {"auto": False, "min": a, "max": b}

        # Stream REC checkboxes
        stream_rec = {}
        for key, cb in self._stream_rec_cbs.items():
            # Use stream name as key (more stable than full key with source_id)
            st = self.streams.get(key)
            if st:
                stream_rec[st.name] = cb.isChecked()

        settings = {
            "window_s": self.win_spin.value(),
            "window_geometry": {
                "x": self.x(), "y": self.y(),
                "w": self.width(), "h": self.height(),
            },
            "channel_visibility": ch_vis,
            "channel_yscale": ch_yscale,
            "stream_rec": stream_rec,
        }
        try:
            with open(SETTINGS_FILE, "w") as f:
                json.dump(settings, f, indent=2)
            print(f"[Settings] Saved to {SETTINGS_FILE}")
        except Exception as e:
            print(f"[Settings] Save error: {e}")

    def _load_settings(self):
        """Load saved settings and apply to UI."""
        if not os.path.isfile(SETTINGS_FILE):
            return
        try:
            with open(SETTINGS_FILE) as f:
                s = json.load(f)
        except Exception as e:
            print(f"[Settings] Load error: {e}")
            return

        # Window size
        if "window_s" in s:
            self.win_spin.setValue(s["window_s"])

        # Window geometry
        geo = s.get("window_geometry")
        if geo:
            self.setGeometry(geo.get("x", 100), geo.get("y", 100),
                             geo.get("w", 1400), geo.get("h", 800))

        print(f"[Settings] Loaded from {SETTINGS_FILE}")

    def _apply_stream_settings(self):
        """Apply per-stream/channel settings after streams are discovered."""
        if not os.path.isfile(SETTINGS_FILE):
            return
        try:
            with open(SETTINGS_FILE) as f:
                s = json.load(f)
        except Exception:
            return

        # Channel visibility
        ch_vis = s.get("channel_visibility", {})
        for cr in self.rows:
            if cr.label in ch_vis:
                cr.cb.setChecked(ch_vis[cr.label])

        # Y-scale
        ch_ys = s.get("channel_yscale", {})
        for cr in self.rows:
            if cr.label in ch_ys:
                ys_cfg = ch_ys[cr.label]
                cr.ys.auto_cb.setChecked(ys_cfg.get("auto", True))
                if not ys_cfg.get("auto", True):
                    cr.ys.mn.setValue(ys_cfg.get("min", -1))
                    cr.ys.mx.setValue(ys_cfg.get("max", 1))

        # Stream REC checkboxes
        stream_rec = s.get("stream_rec", {})
        for key, cb in self._stream_rec_cbs.items():
            st = self.streams.get(key)
            if st and st.name in stream_rec:
                cb.setChecked(stream_rec[st.name])

    # ── cleanup ──────────────────────────────────────────────────────────

    def closeEvent(self, ev):
        self._timer.stop(); self._disco_timer.stop()
        self._save_settings()
        if self._rec_proc is not None:
            self._stop_recording()
        for r in self.readers: r.stop()
        for r in self.readers: r.join(timeout=2)
        super().closeEvent(ev)


# ── main ─────────────────────────────────────────────────────────────────────

def _load_fonts():
    """Load bundled Montserrat fonts."""
    db = QtGui.QFontDatabase
    if os.path.isdir(_FONT_DIR):
        for fn in os.listdir(_FONT_DIR):
            if fn.endswith(".ttf"):
                fid = db.addApplicationFont(os.path.join(_FONT_DIR, fn))
                if fid >= 0:
                    families = db.applicationFontFamilies(fid)
                    if families:
                        print(f"[Font] loaded: {families[0]} ({fn})")

def main():
    app = QtWidgets.QApplication(sys.argv)
    _load_fonts()
    app.setStyle("Fusion")

    # Dark palette matching BiHome theme
    pal = QtGui.QPalette()
    for role, c in [
        (QtGui.QPalette.Window, QtGui.QColor(BG_DARK)),
        (QtGui.QPalette.WindowText, QtGui.QColor(TEXT_PRIMARY)),
        (QtGui.QPalette.Base, QtGui.QColor(BG_INPUT)),
        (QtGui.QPalette.AlternateBase, QtGui.QColor(BG_CARD)),
        (QtGui.QPalette.Text, QtGui.QColor(TEXT_PRIMARY)),
        (QtGui.QPalette.Button, QtGui.QColor(BG_CARD)),
        (QtGui.QPalette.ButtonText, QtGui.QColor(TEXT_PRIMARY)),
        (QtGui.QPalette.Highlight, QtGui.QColor(ACCENT)),
        (QtGui.QPalette.HighlightedText, QtGui.QColor("#ffffff")),
        (QtGui.QPalette.ToolTipBase, QtGui.QColor(BG_CARD)),
        (QtGui.QPalette.ToolTipText, QtGui.QColor(TEXT_PRIMARY)),
    ]:
        pal.setColor(role, c)
    app.setPalette(pal)

    # Global font
    app.setFont(QtGui.QFont("Montserrat", 10))

    v = Viewer(); v.show()
    sys.exit(app.exec_())

if __name__ == "__main__":
    main()
