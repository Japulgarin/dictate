"""Dictate: press (or hold) a hotkey, speak, get the text in a bubble + notes window + clipboard. Runs fully local."""
import datetime as dt
import logging
import os
import sys
import threading
import time
import tomllib

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
NOTES_DIR = os.path.join(ROOT, "notes")
os.makedirs(NOTES_DIR, exist_ok=True)
logging.basicConfig(filename=os.path.join(ROOT, "dictate.log"), level=logging.INFO,
                    format="%(asctime)s %(levelname)s %(message)s")

import engines  # noqa: E402
import screenrec  # noqa: E402
from hotkey import HoldHotkey, _really_down  # noqa: E402
from pynput.keyboard import Controller as KeyController, Key  # noqa: E402
from PySide6.QtCore import QObject, QPoint, QSize, Qt, QTimer, Signal  # noqa: E402
from PySide6.QtGui import (QAction, QActionGroup, QColor, QFont, QIcon, QPainter, QPen, QPixmap,  # noqa: E402
                           QPolygon)
from PySide6.QtWidgets import (QApplication, QHBoxLayout, QLabel, QMenu, QPlainTextEdit,  # noqa: E402
                               QPushButton, QScrollArea, QSystemTrayIcon, QVBoxLayout, QWidget)
from recorder import SR, Recorder  # noqa: E402

with open(os.path.join(HERE, "config.toml"), "rb") as f:
    CFG = tomllib.load(f)

MIN_AUDIO_S = 0.3
_keys = KeyController()


TERMINALS = {t.lower() for t in CFG.get("terminal_apps", [
    "WindowsTerminal.exe", "OpenConsole.exe", "conhost.exe", "cmd.exe", "powershell.exe", "pwsh.exe",
    "wezterm-gui.exe", "alacritty.exe", "mintty.exe", "Hyper.exe", "Tabby.exe", "Warp.exe", "kitty.exe"])}


def foreground_exe():
    """Process name of the window you're typing into, e.g. 'WindowsTerminal.exe'."""
    import ctypes
    from ctypes import wintypes
    u32, k32 = ctypes.windll.user32, ctypes.windll.kernel32
    pid = wintypes.DWORD()
    u32.GetWindowThreadProcessId(u32.GetForegroundWindow(), ctypes.byref(pid))
    h = k32.OpenProcess(0x1000, False, pid.value)  # PROCESS_QUERY_LIMITED_INFORMATION
    if not h:
        return ""
    try:
        buf, n = ctypes.create_unicode_buffer(1024), wintypes.DWORD(1024)
        k32.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(n))
        return os.path.basename(buf.value)
    finally:
        k32.CloseHandle(h)


def paste_into_focused_app():
    """Wait until you've let go of the hotkey modifiers, then paste into whatever app has focus
    (Ctrl+Shift+V in terminals, Ctrl+V everywhere else)."""
    deadline = time.time() + 2
    while time.time() < deadline and any(_really_down(m) for m in ("ctrl", "alt", "shift", "cmd")):
        time.sleep(0.02)
    time.sleep(0.05)
    exe = foreground_exe()
    terminal = exe.lower() in TERMINALS
    logging.info("paste into %s (%s)", exe or "?", "ctrl+shift+v" if terminal else "ctrl+v")
    with _keys.pressed(Key.ctrl):
        if terminal:
            with _keys.pressed(Key.shift):
                _keys.tap("v")
        else:
            _keys.tap("v")
TOGGLE = CFG.get("hotkey_mode", "toggle") == "toggle"  # press once to start, again to stop
HOW = "press {} to start/stop" if TOGGLE else "hold {} to talk"
SILENCE_RMS = CFG.get("silence_rms", 0.01)  # loudest 30 ms frame must exceed this, else it's treated as silence


def is_silent(audio):
    n = int(0.03 * SR)
    frames = audio[: len(audio) // n * n].reshape(-1, n)
    return float((frames ** 2).mean(axis=1).max() ** 0.5) < SILENCE_RMS


def dot_icon(color):
    pm = QPixmap(64, 64)
    pm.fill(Qt.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.Antialiasing)
    p.setBrush(QColor(color))
    p.setPen(Qt.NoPen)
    p.drawEllipse(6, 6, 52, 52)
    p.end()
    return QIcon(pm)


def glyph_icon(kind):
    """White drawn icon for the bubble buttons (emoji such as ⏸ ⏺ show up as blue boxes on Windows)."""
    pm = QPixmap(64, 64)
    pm.fill(Qt.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.Antialiasing)
    white = QColor("white")
    p.setBrush(white)
    p.setPen(Qt.NoPen)
    if kind == "mic":
        p.drawRoundedRect(24, 6, 16, 32, 8, 8)
        p.setBrush(Qt.NoBrush)
        p.setPen(QPen(white, 5, Qt.SolidLine, Qt.RoundCap))
        p.drawArc(14, 14, 36, 34, 180 * 16, 180 * 16)  # cradle under the capsule
        p.drawLine(32, 48, 32, 58)
    elif kind == "pause":
        p.drawRoundedRect(16, 12, 11, 40, 4, 4)
        p.drawRoundedRect(37, 12, 11, 40, 4, 4)
    elif kind == "play":
        p.drawPolygon(QPolygon([QPoint(20, 10), QPoint(54, 32), QPoint(20, 54)]))
    elif kind == "record":
        p.drawEllipse(14, 14, 36, 36)
    elif kind == "stop":
        p.drawRoundedRect(16, 16, 32, 32, 5, 5)
    elif kind == "camera":
        p.drawRoundedRect(4, 18, 40, 28, 6, 6)
        p.drawPolygon(QPolygon([QPoint(46, 27), QPoint(60, 18), QPoint(60, 46), QPoint(46, 37)]))
    elif kind == "notes":
        p.drawRoundedRect(14, 6, 36, 52, 5, 5)
        p.setPen(QPen(QColor("#7c3aed"), 4, Qt.SolidLine, Qt.RoundCap))
        for y in (20, 31, 42):
            p.drawLine(22, y, 42, y)
    p.end()
    return QIcon(pm)


def btn_css(bg, radius):
    return (f"QPushButton{{background:{bg};color:white;border:2px solid rgba(255,255,255,70);border-radius:{radius}px;}}"
            "QPushButton:hover{border-color:white;}"
            "QPushButton:checked{background:#22c55e;border-color:white;}")


class Bridge(QObject):
    """Thread-safe signals from the hotkey/worker threads into the Qt main thread."""
    state = Signal(str)          # "listening" | "transcribing" | "idle" | "loading" | "error:<msg>"
    result = Signal(str, float)  # text, seconds taken
    screen = Signal(str)         # lines printed by the screen recorder: "recording", "saved <mp4>", ...
    toggle_screen = Signal()     # screen-recording hotkey (Shift+Win+Alt by default)
    restart_app = Signal()       # restart hotkey (Win+Alt+Z by default)


class Bubble(QWidget):
    clicked = Signal()
    pause_clicked = Signal()
    notes_clicked = Signal()
    record_clicked = Signal()
    camera_clicked = Signal()

    def __init__(self):
        super().__init__(None, Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint | Qt.Tool
                         | Qt.WindowDoesNotAcceptFocus)
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setAttribute(Qt.WA_ShowWithoutActivating)
        self.label = QLabel(self)
        self.label.setWordWrap(True)
        self.label.setFont(QFont("Segoe UI", 10))
        self.label.setStyleSheet("color: white;")
        lay = QHBoxLayout(self)
        lay.setContentsMargins(16, 10, 16, 10)
        lay.addWidget(self.label)
        self.close_btn = QPushButton("✕", self)
        self.close_btn.setFixedSize(20, 20)
        self.close_btn.setCursor(Qt.PointingHandCursor)
        self.close_btn.setToolTip("Ocultar mensaje (para salir: icono de la bandeja → Quit)")
        self.close_btn.setFocusPolicy(Qt.NoFocus)
        self.close_btn.setStyleSheet("QPushButton{color:white;background:rgba(0,0,0,60);border:none;"
                                     "border-radius:10px;font-weight:bold;}"
                                     "QPushButton:hover{background:#dc2626;}")
        self.close_btn.clicked.connect(self.show_idle)  # only dismisses the message; quit is in the tray menu
        lay.addWidget(self.close_btn, 0, Qt.AlignTop)
        # Controls that pop up next to the dot when you click it: small round buttons with drawn icons,
        # one colour each, in the order camera, record screen, notes, pause.
        self.cam_btn = QPushButton(self)
        self.rec_btn = QPushButton(self)
        self.notes_btn = QPushButton(self)
        self.pause_btn = QPushButton(self)
        self.cam_btn.setCheckable(True)  # green while the camera is on
        self.rec_btn.setCheckable(True)  # green while a screen recording is running
        spec = [(self.cam_btn, "#0d9488", "camera", "Show camera", self.camera_clicked),
                (self.rec_btn, "#e11d48", "record", "Record screen", self.record_clicked),
                (self.notes_btn, "#7c3aed", "notes", "Notes", self.notes_clicked),
                (self.pause_btn, "#d97706", "pause", "Pause (free GPU)", self.pause_clicked)]
        self.ctrl_btns = [b for b, *_ in spec]
        self.ctrl_bg = {b: bg for b, bg, *_ in spec}
        self.scale = 1.0
        for b, bg, icon, tip, signal in spec:
            b.setIcon(glyph_icon(icon))
            b.setToolTip(tip)
            b.setCursor(Qt.PointingHandCursor)
            b.setFocusPolicy(Qt.NoFocus)
            b.hide()
            b.clicked.connect(signal)
            lay.addWidget(b)
        # − / + only show in size mode (double-click the bubble) so you can resize it live.
        self.size_btns = []
        for text, delta, tip in (("−", -0.1, "Smaller bubble"), ("+", 0.1, "Bigger bubble")):
            b = QPushButton(text, self)
            b.setToolTip(tip)
            b.setCursor(Qt.PointingHandCursor)
            b.setFocusPolicy(Qt.NoFocus)
            b.hide()
            b.clicked.connect(lambda _=False, d=delta: self.set_scale(round(min(2.0, max(0.3, self.scale + d)), 2)))
            lay.addWidget(b)
            self.size_btns.append(b)
            self.ctrl_btns.append(b)
            self.ctrl_bg[b] = "#374151"
        self._style_buttons()
        self.paused = False
        self.camera_on = False
        self.screen_on = False
        self.size_mode = False
        self._skip_click = False  # set by a double-click so its two releases don't toggle the buttons
        self.color = QColor("#222")
        self._drag = None
        self.hide_timer = QTimer(self, singleShot=True, timeout=self._fold)
        # keep the bubble alive and on top even after the taskbar / Start menu covers it
        self.keepalive = QTimer(self, interval=2000, timeout=self._keep_on_top)
        self.keepalive.start()

    def _keep_on_top(self):
        if not self.isVisible():
            self.show()
        self.raise_()

    def show_idle(self):
        """Small always-visible dot so you know the app is running. Click it for pause / notes buttons."""
        if self.paused:
            self.show_msg("❚❚", "#4b5563")
        else:
            self.show_msg("🎙", "#2563eb")
        self.close_btn.hide()
        self.adjustSize()

    def controls_visible(self):
        return self.pause_btn.isVisible()

    def show_controls(self):
        """Mic plus Dictate, Pause/Resume, Camera, Record screen and Notes buttons; folds back to the
        plain dot after a few seconds."""
        self.show_idle()
        self.pause_btn.setIcon(glyph_icon("play" if self.paused else "pause"))
        self.pause_btn.setToolTip("Resume" if self.paused else "Pause (free GPU)")
        self.cam_btn.setChecked(self.camera_on)
        self.cam_btn.setToolTip("Hide camera" if self.camera_on else "Show camera")
        self.rec_btn.setChecked(self.screen_on)
        self.rec_btn.setIcon(glyph_icon("stop" if self.screen_on else "record"))
        self.rec_btn.setToolTip("Stop recording" if self.screen_on else "Record screen")
        for b in self.ctrl_btns:
            b.setVisible(self.size_mode or b not in self.size_btns)
        self.adjustSize()
        self._reanchor()
        self.hide_timer.start(5000)

    def _reanchor(self):
        if not self._moved():
            scr = QApplication.primaryScreen().availableGeometry()
            self.move(scr.right() - self.width() - 24, scr.bottom() - self.height() - 24)

    def set_scale(self, s):
        """Bigger or smaller bubble (tray → Bubble size, or bubble_scale in config.toml)."""
        self.scale = s
        self.label.setFont(QFont("Segoe UI", max(7, round(10 * s))))
        self.label.setMinimumSize(20, 20)  # the dot stays big enough to click at small sizes
        self.close_btn.setFixedSize(max(16, round(20 * s)), max(16, round(20 * s)))
        pad = round(10 * s)
        self.layout().setContentsMargins(round(16 * s), pad, round(16 * s), pad)
        self._style_buttons()
        self.adjustSize()
        self._reanchor()

    def _style_buttons(self):
        side = max(24, round(28 * self.scale))  # floor so the buttons can always be clicked
        icon = max(12, round(16 * self.scale))
        for b in self.ctrl_btns:
            b.setFixedSize(side, side)
            b.setIconSize(QSize(icon, icon))
            b.setStyleSheet(btn_css(self.ctrl_bg[b], side // 2))

    def show_recording(self, text):
        """Dark bubble with red text while a screen recording runs. The buttons stay open, so you can stop it."""
        self.show_msg(text, "#111827", keep_controls=True)
        self.label.setStyleSheet("color: #f87171; font-weight: bold;")

    def show_msg(self, text, color, autohide=False, keep_controls=False):
        if not keep_controls:
            for b in self.ctrl_btns:
                b.hide()
        self.close_btn.show()
        self.color = QColor(color)
        self.label.setStyleSheet("color: white;")
        self.label.setText(text)
        self.label.setMaximumWidth(round(380 * self.scale))
        self.adjustSize()
        self._reanchor()
        self.show()
        self.raise_()
        self.update()
        if autohide:
            self.hide_timer.start(int(CFG.get("bubble_hide_after_s", 4) * 1000))
        else:
            self.hide_timer.stop()

    def _moved(self):
        return getattr(self, "_user_pos", False)

    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        p.setBrush(self.color)
        p.setPen(Qt.NoPen)
        p.drawRoundedRect(self.rect(), 18, 18)

    def mousePressEvent(self, e):
        self._drag = e.globalPosition().toPoint() - self.pos()
        self._press_at = e.globalPosition().toPoint()

    def mouseMoveEvent(self, e):
        if self._drag is not None:
            self.move(e.globalPosition().toPoint() - self._drag)
            self._user_pos = True

    def mouseReleaseEvent(self, e):
        if self._skip_click:
            self._skip_click = False
        elif (e.globalPosition().toPoint() - self._press_at).manhattanLength() < 5:
            self.clicked.emit()
        self._drag = None

    def mouseDoubleClickEvent(self, e):
        """Double-click: open the buttons with − / + to resize the bubble."""
        self._skip_click = True
        self.size_mode = True
        self.show_controls()
        self.hide_timer.stop()  # size mode stays open until you click the dot again

    def _fold(self):
        """Hide the buttons and leave size mode; used by the timer and by clicking the dot again."""
        self.size_mode = False
        self.show_idle()


class NotesWindow(QWidget):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Dictate — notes")
        self.setWindowIcon(dot_icon("#3b82f6"))
        self.resize(460, 560)
        outer = QVBoxLayout(self)
        top = QHBoxLayout()
        self.status = QLabel("")
        folder = QPushButton("Open notes folder")
        folder.clicked.connect(lambda: os.startfile(NOTES_DIR))
        top.addWidget(self.status, 1)
        top.addWidget(folder)
        outer.addLayout(top)
        self.list_host = QWidget()
        self.list = QVBoxLayout(self.list_host)
        self.list.addStretch(1)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setWidget(self.list_host)
        outer.addWidget(scroll)

    def add(self, text, when):
        box = QWidget()
        lay = QVBoxLayout(box)
        lay.setContentsMargins(4, 4, 4, 8)
        head = QHBoxLayout()
        head.addWidget(QLabel(f"<span style='color:gray'>{when:%H:%M:%S}</span>"), 1)
        edit = QPlainTextEdit(text)
        edit.setFixedHeight(max(54, min(160, 22 * (len(text) // 50 + 2))))
        copy = QPushButton("Copy")
        copy.clicked.connect(lambda: QApplication.clipboard().setText(edit.toPlainText()))
        head.addWidget(copy)
        lay.addLayout(head)
        lay.addWidget(edit)
        self.list.insertWidget(0, box)


class App:
    def __init__(self):
        self.qt = QApplication(sys.argv)
        self.qt.setQuitOnLastWindowClosed(False)
        self.bridge = Bridge()
        self.bridge.state.connect(self.on_state)
        self.bridge.result.connect(self.on_result)
        self.bubble = Bubble()
        self.bubble.set_scale(CFG.get("bubble_scale", 1.0))
        self.notes = NotesWindow()
        self.bubble.clicked.connect(self.on_bubble_click)
        self.bubble.pause_clicked.connect(lambda: self.pause_action.setChecked(not self.paused))
        self.bubble.notes_clicked.connect(self.show_notes)
        self.bubble.record_clicked.connect(self.toggle_screen_recording)
        self.bubble.camera_clicked.connect(self.toggle_camera)
        self.camera = screenrec.CameraBubble()
        self.screen_rec = screenrec.ScreenRecorder(self.bridge.screen.emit)
        self.bridge.screen.connect(self.on_screen_line)
        self.bridge.toggle_screen.connect(self.toggle_screen_recording)
        self.bridge.restart_app.connect(self.restart)
        # Extra global hotkeys: start/stop a screen recording, and restart the app.
        self.screen_hotkey = HoldHotkey(CFG.get("screen_hotkey", "shift+cmd+alt"),
                                        self.bridge.toggle_screen.emit, None, toggle=True)
        self.restart_hotkey = HoldHotkey(CFG.get("restart_hotkey", "cmd+alt+z"),
                                         self.bridge.restart_app.emit, None, toggle=True)
        self.screen_hotkey.start()
        self.restart_hotkey.start()
        self.exit_code = 0  # 1 on restart: the watchdog (run.pyw) starts the app again
        self.rec_timer = QTimer(interval=1000, timeout=self._tick_rec)
        self.rec_started = 0.0
        self.recorder = Recorder(CFG.get("preroll_s", 0.3))
        self.engine = None
        self.model_name = CFG.get("model", "whisper-large-v3-turbo")
        self.language = CFG.get("language", "auto")
        self.auto_paste = CFG.get("auto_paste", True)
        self.busy = threading.Lock()
        self.paused = False
        self._build_tray()
        self.recording = False
        if TOGGLE:
            self.hotkey = HoldHotkey(CFG.get("hotkey", "ctrl+cmd"), self.on_toggle, None, toggle=True)
        else:
            self.hotkey = HoldHotkey(CFG.get("hotkey", "ctrl+cmd"), self.on_down, self.on_up)
        self.hotkey.start()
        self.load_model(self.model_name)

    def _build_tray(self):
        self.tray = QSystemTrayIcon(dot_icon("#3b82f6"))
        menu = QMenu()
        menu.addAction("Show notes", self.show_notes)
        models = menu.addMenu("Model")
        grp = QActionGroup(menu)
        for name in engines.MODELS:
            a = QAction(name, models, checkable=True, checked=(name == self.model_name))
            a.triggered.connect(lambda _=False, n=name: self.load_model(n))
            grp.addAction(a)
            models.addAction(a)
        langs = menu.addMenu("Language")
        lgrp = QActionGroup(menu)
        for code in ("auto", "es", "en", "de"):
            a = QAction(code, langs, checkable=True, checked=(code == self.language))
            a.triggered.connect(lambda _=False, c=code: setattr(self, "language", c))
            lgrp.addAction(a)
            langs.addAction(a)
        sizes = menu.addMenu("Bubble size")
        sgrp = QActionGroup(menu)
        for name, s in (("Small", 0.7), ("Normal", 1.0), ("Large", 1.4), ("Extra large", 1.8)):
            a = QAction(name, sizes, checkable=True, checked=(s == CFG.get("bubble_scale", 1.0)))
            a.triggered.connect(lambda _=False, v=s: self.bubble.set_scale(v))
            sgrp.addAction(a)
            sizes.addAction(a)
        paste = QAction("Auto-paste", menu, checkable=True, checked=self.auto_paste)
        paste.toggled.connect(lambda on: setattr(self, "auto_paste", on))
        menu.addAction(paste)
        menu.addSeparator()
        self.pause_action = QAction("Pause (free GPU)", menu, checkable=True)
        self.pause_action.toggled.connect(self.set_paused)
        menu.addAction(self.pause_action)
        menu.addAction("Quit", self.quit)
        self.tray.setContextMenu(menu)
        self.tray.activated.connect(lambda r: r == QSystemTrayIcon.Trigger and self.show_notes())
        self.tray.show()
        self.menu = menu

    def on_bubble_click(self):
        """Clicking the mic shows Pause/Resume + Notes next to it; clicking again folds them away."""
        if self.bubble.controls_visible():
            self.bubble._fold()
        else:
            self.bubble.show_controls()

    def show_notes(self):
        self.notes.show()
        self.notes.raise_()
        self.notes.activateWindow()

    def _recorder_missing(self):
        if screenrec.available():
            return False
        self.bubble.show_msg("Recorder not built: cd rs && cargo build --release", "#7f1d1d", autohide=True)
        return True

    def toggle_screen_recording(self):
        """Red button: start or stop a screen recording. Stopping saves it and renders the studio version;
        then Recordly opens with the video so you can look at it."""
        if self._recorder_missing():
            return
        self.screen_rec.toggle()
        self.bubble.screen_on = self.screen_rec.on
        if self.screen_rec.on:
            self.rec_started = time.monotonic()
            self.rec_timer.start(1000)
            self._show_rec_status()
        else:
            self.rec_timer.stop()
            self.bubble.show_msg("● Guardando video…", "#4b5563")

    def _show_rec_status(self):
        """Red banner with the elapsed time; it stays up until the recording is saved."""
        secs = int(time.monotonic() - self.rec_started)
        self.bubble.show_recording(f"● Grabando pantalla {secs // 60:02d}:{secs % 60:02d}")

    def _tick_rec(self):
        if self.screen_rec.on:
            self._show_rec_status()

    def on_screen_line(self, line):
        kind, _, arg = line.partition(" ")
        logging.info("screen: %s", line)
        if kind == "recording":
            self._show_rec_status()
        elif kind == "saved":
            self.bubble.show_msg("Video guardado, renderizando…", "#4b5563")
        elif kind == "studio":
            # Recordly waits minimized, so the studio render is ready when you want to look at it.
            if screenrec.open_recordly(minimized=True):
                self.bubble.show_msg(f"Video listo, Recordly en segundo plano: {os.path.basename(arg)}",
                                     "#2563eb", autohide=True)
            else:
                self.bubble.show_msg("Recordly not built (see app/screenrec.py)", "#7f1d1d", autohide=True)
        elif kind == "error":
            self.bubble.show_msg(f"Error grabando: {arg}", "#7f1d1d", autohide=True)

    def toggle_camera(self):
        """📷 in the bubble: show or hide the round webcam preview (it gets recorded with the screen)."""
        if self._recorder_missing():
            return
        self.camera.toggle()
        self.bubble.camera_on = self.camera.on
        self.bubble.show_controls()

    def load_model(self, name):
        def work():
            with self.busy:
                self.bridge.state.emit("loading")
                try:
                    self.engine = None
                    t = time.perf_counter()
                    engines.DEVICE = CFG.get("device", "auto")
                    self.engine = engines.load(name)
                    self.model_name = name
                    logging.info("loaded %s in %.1fs", name, time.perf_counter() - t)
                    self.bridge.state.emit("ready")
                except Exception as e:
                    logging.exception("load failed")
                    self.bridge.state.emit(f"error:Could not load {name}: {e}")
        threading.Thread(target=work, daemon=True).start()

    def set_paused(self, on):
        """Pause unloads the model so its VRAM goes back to other apps; resume loads it again."""
        if on == self.paused:
            return
        self.paused = on
        self.bubble.paused = on
        if not on:
            self.load_model(self.model_name)
            return
        if self.recording:
            self.recording = False
            self.recorder.stop()

        def work():
            import gc
            with self.busy:
                self.engine = None
                gc.collect()
                logging.info("paused: model unloaded")
                self.bridge.state.emit("paused")
        threading.Thread(target=work, daemon=True).start()

    # hotkey thread
    def on_toggle(self):
        if self.recording:
            self.on_up()
        else:
            self.on_down()

    def on_down(self):
        if self.paused:
            logging.info("hotkey ignored: paused")
            return
        if self.engine is None or self.busy.locked():
            logging.info("hotkey ignored: model %s", "loading" if self.engine is None else "busy")
            return
        logging.info("hotkey down")
        try:
            self.recorder.start()
            self.recording = True
            self.bridge.state.emit("listening")
        except Exception as e:
            logging.exception("mic")
            self.bridge.state.emit(f"error:Microphone error: {e}")

    def on_up(self):
        if not self.recording:
            return
        self.recording = False
        audio = self.recorder.stop()
        logging.info("hotkey up: %.2fs audio", len(audio) / SR)
        if len(audio) < MIN_AUDIO_S * SR:
            self.bridge.state.emit("idle")
            return
        if is_silent(audio):
            logging.info("skipped as silence (peak below silence_rms=%s)", SILENCE_RMS)
            self.bridge.result.emit("", 0.0)
            return
        threading.Thread(target=self._transcribe, args=(audio,), daemon=True).start()

    def _transcribe(self, audio):
        with self.busy:
            self.bridge.state.emit("transcribing")
            try:
                t = time.perf_counter()
                lang = None if self.language == "auto" else self.language
                text = self.engine.transcribe(audio, language=lang)
                self.bridge.result.emit(text, time.perf_counter() - t)
            except Exception as e:
                logging.exception("transcribe")
                self.bridge.state.emit(f"error:{e}")

    # Qt main thread
    def on_state(self, s):
        if s == "listening":
            self.bubble.show_msg("●  Listening…", "#dc2626")
        elif s == "transcribing":
            self.bubble.show_msg("…  Transcribing", "#d97706")
        elif s == "loading":
            self.tray.setIcon(dot_icon("#9ca3af"))
            self.notes.status.setText(f"Loading {self.model_name}…")
            self.bubble.show_msg("Loading model…", "#4b5563")
        elif s == "ready":
            self.tray.setIcon(dot_icon("#3b82f6"))
            self.notes.status.setText(f"Model: {self.model_name}")
            self.tray.setToolTip(f"Dictate — {HOW.format(CFG.get('hotkey'))} ({self.model_name})")
            self.bubble.show_msg(f"Ready — {HOW.format(CFG.get('hotkey'))}", "#2563eb", autohide=True)
        elif s == "paused":
            self.tray.setIcon(dot_icon("#6b7280"))
            self.tray.setToolTip("Dictate — paused (tray → uncheck Pause to resume)")
            self.notes.status.setText("Paused — model unloaded, GPU free")
            self.bubble.show_msg("❚❚  Paused", "#4b5563")
        elif s == "idle":
            self.bubble.show_idle()
        elif s.startswith("error:"):
            self.bubble.show_msg(s[6:], "#7f1d1d", autohide=True)

    def on_result(self, text, secs):
        if not text:
            self.bubble.show_msg("(heard nothing)", "#4b5563", autohide=True)
            return
        now = dt.datetime.now()
        QApplication.clipboard().setText(text)
        self.notes.add(text, now)
        with open(os.path.join(NOTES_DIR, f"{now:%Y-%m-%d}.md"), "a", encoding="utf-8") as f:
            f.write(f"- **{now:%H:%M:%S}** {text}\n")
        logging.info("%.2fs %d chars", secs, len(text))
        self.bubble.show_msg(f"📋 {text}", "#15803d", autohide=True)
        if self.auto_paste:
            threading.Thread(target=paste_into_focused_app, daemon=True).start()

    def quit(self):
        logging.info("quit from tray menu")
        self.hotkey.stop()
        self.screen_hotkey.stop()
        self.restart_hotkey.stop()
        self.recorder.close()
        self.camera.close()
        self.screen_rec.close()
        self.tray.hide()
        self.bubble.keepalive.stop()
        self.bubble.hide()
        self.qt.exit(self.exit_code)

    def restart(self):
        """Win+Alt+Z: quit with code 1, and the watchdog starts Dictate again. Not while recording,
        so the video still gets saved."""
        if self.screen_rec.on:
            self._show_rec_status()
            self.bubble.show_msg("Para la grabación antes de reiniciar", "#7f1d1d", autohide=True)
            return
        logging.info("restart: hotkey")
        self.exit_code = 1
        self.quit()

    def run(self):
        return self.qt.exec()


def _log_crash(*exc):
    logging.critical("crash", exc_info=exc)


if __name__ == "__main__":
    import ctypes
    sys.excepthook = _log_crash
    threading.excepthook = lambda a: _log_crash(a.exc_type, a.exc_value, a.exc_traceback)
    _mutex = ctypes.windll.kernel32.CreateMutexW(None, False, r"Local\DictateAppSingleInstance")
    if ctypes.windll.kernel32.GetLastError() == 183:  # ERROR_ALREADY_EXISTS: already running
        sys.exit(0)
    sys.exit(App().run())
