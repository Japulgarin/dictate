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
from hotkey import HoldHotkey, _really_down  # noqa: E402
from pynput.keyboard import Controller as KeyController, Key  # noqa: E402
from PySide6.QtCore import QObject, QPoint, Qt, QTimer, Signal  # noqa: E402
from PySide6.QtGui import QAction, QActionGroup, QColor, QFont, QIcon, QPainter, QPixmap  # noqa: E402
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


class Bridge(QObject):
    """Thread-safe signals from the hotkey/worker threads into the Qt main thread."""
    state = Signal(str)          # "listening" | "transcribing" | "idle" | "loading" | "error:<msg>"
    result = Signal(str, float)  # text, seconds taken


class Bubble(QWidget):
    clicked = Signal()

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
        self.color = QColor("#222")
        self._drag = None
        self.hide_timer = QTimer(self, singleShot=True, timeout=self.show_idle)
        # keep the bubble alive and on top even after the taskbar / Start menu covers it
        self.keepalive = QTimer(self, interval=2000, timeout=self._keep_on_top)
        self.keepalive.start()

    def _keep_on_top(self):
        if not self.isVisible():
            self.show()
        self.raise_()

    def show_idle(self):
        """Small always-visible dot so you know the app is running. Click it to open your notes."""
        self.show_msg("🎙", "#2563eb")
        self.close_btn.hide()
        self.adjustSize()

    def show_msg(self, text, color, autohide=False):
        self.close_btn.show()
        self.color = QColor(color)
        self.label.setText(text)
        self.label.setMaximumWidth(380)
        self.adjustSize()
        if not self._moved():
            scr = QApplication.primaryScreen().availableGeometry()
            self.move(scr.right() - self.width() - 24, scr.bottom() - self.height() - 24)
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
        if (e.globalPosition().toPoint() - self._press_at).manhattanLength() < 5:
            self.clicked.emit()
        self._drag = None


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
        self.notes = NotesWindow()
        self.bubble.clicked.connect(self.show_notes)
        self.recorder = Recorder(CFG.get("preroll_s", 0.3))
        self.engine = None
        self.model_name = CFG.get("model", "whisper-large-v3-turbo")
        self.language = CFG.get("language", "auto")
        self.auto_paste = CFG.get("auto_paste", True)
        self.busy = threading.Lock()
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
        paste = QAction("Auto-paste", menu, checkable=True, checked=self.auto_paste)
        paste.toggled.connect(lambda on: setattr(self, "auto_paste", on))
        menu.addAction(paste)
        menu.addSeparator()
        menu.addAction("Quit", self.quit)
        self.tray.setContextMenu(menu)
        self.tray.activated.connect(lambda r: r == QSystemTrayIcon.Trigger and self.show_notes())
        self.tray.show()
        self.menu = menu

    def show_notes(self):
        self.notes.show()
        self.notes.raise_()
        self.notes.activateWindow()

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

    # hotkey thread
    def on_toggle(self):
        if self.recording:
            self.on_up()
        else:
            self.on_down()

    def on_down(self):
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
        self.recorder.close()
        self.tray.hide()
        self.bubble.keepalive.stop()
        self.bubble.hide()
        self.qt.quit()

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
