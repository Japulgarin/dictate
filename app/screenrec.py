"""Screen recording via Recordly (open source, MIT) and the round camera preview via the Rust
helper (rs/target/release/dictate.exe)."""
import os
import subprocess
import logging
import threading

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
EXE = os.path.join(ROOT, "rs", "target", "release", "dictate.exe")
CONFIG = os.path.join(ROOT, "app", "config.toml")
# Built from https://github.com/DougNix/recordly with `electron-builder --win --dir`.
RECORDLY_EXE = os.path.join(os.path.expanduser("~"), "recordly", "release", "win-unpacked", "Recordly.exe")
NO_WINDOW = 0x08000000  # CREATE_NO_WINDOW: no console window flashing up


def available():
    return os.path.exists(EXE)


def open_recordly(minimized=False):
    """Start Recordly. With minimized=True it opens in the taskbar without taking focus, so it waits in
    the background. (If it is already running it keeps a single instance and comes to the front.)
    Returns False if it isn't built."""
    if not os.path.exists(RECORDLY_EXE):
        return False
    si = subprocess.STARTUPINFO()
    si.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    si.wShowWindow = 7 if minimized else 1  # SW_SHOWMINNOACTIVE / SW_SHOWNORMAL
    subprocess.Popen([RECORDLY_EXE], cwd=os.path.dirname(RECORDLY_EXE), startupinfo=si)
    return True


def _spawn(args, stdout):
    # The helper's errors go to recorder.log; the child keeps its own handle after we close ours.
    with open(os.path.join(ROOT, "recorder.log"), "a", encoding="utf-8") as log:
        return subprocess.Popen([EXE, *args, "--config", CONFIG], stdin=subprocess.PIPE, stdout=stdout,
                                stderr=log, text=True, creationflags=NO_WINDOW)


class ScreenRecorder:
    """Runs `dictate.exe --record`: screen + mic + cursor until its stdin closes. It prints one line per
    step (recording, saved <mp4>, rendering, studio <mp4>, error <msg>); each line goes to on_line."""

    def __init__(self, on_line, out_dir="", region="full"):
        self.proc = None
        self.on_line = on_line
        self.out_dir = out_dir  # where the videos are saved (empty = the helper's default folder)
        self.region = region    # "full", "left" or "right" half of the screen

    @property
    def on(self):
        return self.proc is not None and self.proc.poll() is None

    def toggle(self):
        if self.on:
            self.proc.stdin.close()  # EOF tells it to stop, save and render
            self.proc = None
        else:
            args = ["--record", "--region", self.region]
            if self.out_dir:
                args += ["--out", self.out_dir]
            self.proc = _spawn(args, subprocess.PIPE)
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

    def resize(self, delta):
        """Sends + or - to the camera window, which makes the circle bigger or smaller."""
        if self.on:
            self.proc.stdin.write("+\n" if delta > 0 else "-\n")
            self.proc.stdin.flush()


def make_vertical(src):
    """Writes <stamp>_instagram.mp4: 1080x1920 (9:16) with the screen centred over a blurred copy of itself.
    Tries the NVIDIA encoder first (about 1.7x faster), then CPU. Runs ffmpeg, so call it from a worker thread."""
    base = src[:-len(".studio.mp4")] if src.endswith(".studio.mp4") else os.path.splitext(src)[0]
    dst = base + "_instagram.mp4"
    vf = ("[0:v]split[a][b];"
          "[a]scale=1080:1920:force_original_aspect_ratio=increase,crop=1080:1920,boxblur=20:5[bg];"
          "[b]scale=1080:-2[fg];[bg][fg]overlay=(W-w)/2:(H-h)/2,setsar=1")
    tail = ["-r", "30", "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "160k", "-movflags", "+faststart", dst]
    for enc in (["-c:v", "h264_nvenc", "-preset", "p4", "-cq", "23"],
                ["-c:v", "libx264", "-preset", "veryfast", "-crf", "23"]):
        cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-i", src, "-filter_complex", vf] + enc + tail
        if subprocess.run(cmd, creationflags=NO_WINDOW).returncode == 0:
            return dst
        logging.warning("vertical encode with %s failed for %s", enc[1], src)
    logging.error("instagram version failed for %s", src)
    return None
