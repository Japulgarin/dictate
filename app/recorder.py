"""Microphone capture. The stream stays open so start() is instant, and a short pre-roll
buffer keeps the first word even if you start talking right as you press the hotkey.
Audio is only kept in memory; nothing is saved unless you're holding the hotkey."""
import collections
import logging
import threading
import time

import numpy as np
import sounddevice as sd

SR = 16000
BLOCK = 480  # 30 ms


class Recorder:
    def __init__(self, preroll_s=0.3):
        self._lock = threading.Lock()
        self._recording = False
        self._chunks = []
        self._preroll = collections.deque(maxlen=max(1, int(preroll_s * SR / BLOCK)))
        self._stream = None
        self._open()

    def _open(self):
        self._last_cb = time.monotonic()
        self._stream = sd.InputStream(samplerate=SR, channels=1, dtype="float32",
                                      blocksize=BLOCK, callback=self._cb)
        self._stream.start()

    def _reopen_if_dead(self):
        """After sleep or a mic change the stream can silently stop delivering audio: reconnect."""
        if self._stream.active and time.monotonic() - self._last_cb < 1:
            return
        logging.warning("mic stream dead, reconnecting")
        try:
            self._stream.close()
        except Exception:
            pass
        sd._terminate()  # refresh the device list (default mic may have changed)
        sd._initialize()
        self._open()

    def _cb(self, data, *_):
        self._last_cb = time.monotonic()
        with self._lock:
            if self._recording:
                self._chunks.append(data.copy())
            else:
                self._preroll.append(data.copy())

    def start(self):
        self._reopen_if_dead()
        with self._lock:
            if self._recording:
                return
            self._chunks = list(self._preroll)
            self._preroll.clear()
            self._recording = True

    def stop(self):
        with self._lock:
            self._recording = False
            chunks, self._chunks = self._chunks, []
        if not chunks:
            return np.zeros(0, dtype=np.float32)
        return np.concatenate(chunks)[:, 0]

    def close(self):
        self._stream.stop()
        self._stream.close()
