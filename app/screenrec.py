"""Screen recording via Recordly (open source, MIT) and the round camera preview via the Rust
helper (rs/target/release/dictate.exe)."""
import os
import subprocess
import threading

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
EXE = os.path.join(ROOT, "rs", "target", "release", "dictate.exe")
CONFIG = os.path.join(ROOT, "app", "config.toml")
# Built from https://github.com/DougNix/recordly with `electron-builder --win --dir`.
RECORDLY_EXE = os.path.join(os.path.expanduser("~"), "recordly", "release", "win-unpacked", "Recordly.exe")
NO_WINDOW = 0x08000000  # CREATE_NO_WINDOW: no console window flashing up


def available():
    return os.path.exists(EXE)


def open_recordly():
    """Start Recordly, or bring it to the front if it's already open (it keeps a single instance).
    Returns False if it isn't built."""
    if not os.path.exists(RECORDLY_EXE):
        return False
    subprocess.Popen([RECORDLY_EXE], cwd=os.path.dirname(RECORDLY_EXE))
    return True


def _spawn(args, stdout):
    # The helper's errors go to recorder.log; the child keeps its own handle after we close ours.
    with open(os.path.join(ROOT, "recorder.log"), "a", encoding="utf-8") as log:
        return subprocess.Popen([EXE, *args, "--config", CONFIG], stdin=subprocess.PIPE, stdout=stdout,
                                stderr=log, text=True, creationflags=NO_WINDOW)


class ScreenRecorder:
    """Runs `dictate.exe --record`: screen + mic + cursor until its stdin closes. It prints one line per
    step (recording, saved <mp4>, rendering, studio <mp4>, error <msg>); each line goes to on_line."""

    def __init__(self, on_line):
        self.proc = None
        self.on_line = on_line

    @property
    def on(self):
        return self.proc is not None and self.proc.poll() is None

    def toggle(self):
        if self.on:
            self.proc.stdin.close()  # EOF tells it to stop, save and render
            self.proc = None
        else:
            self.proc = _spawn(["--record"], subprocess.PIPE)
            threading.Thread(target=self._read, args=(self.proc,), daemon=True).start()

    def _read(self, proc):
        for line in proc.stdout:
            self.on_line(line.strip())

    def close(self):
        if self.on:
            self.toggle()


class CameraBubble:
    """Runs `dictate.exe --camera-only`, the round webcam preview. Closing its stdin makes it exit."""

    def __init__(self):
        self.proc = None

    @property
    def on(self):
        return self.proc is not None and self.proc.poll() is None

    def toggle(self):
        if self.on:
            self.proc.stdin.close()
            self.proc = None
        else:
            self.proc = _spawn(["--camera-only"], subprocess.DEVNULL)

    def close(self):
        if self.on:
            self.toggle()
