"""Global hotkey: on_down() when every key in the combo is held, on_up() when any is released.
With toggle=True only on_down() fires, once per press of the combo."""
import ctypes
import sys

from pynput import keyboard

# Windows virtual-key codes for modifiers, to double-check what's really held down
VK = {"ctrl": (0x11,), "alt": (0x12,), "shift": (0x10,), "cmd": (0x5B, 0x5C)}


def _really_down(group):
    if sys.platform != "win32" or group not in VK:
        return True
    return any(ctypes.windll.user32.GetAsyncKeyState(v) & 0x8000 for v in VK[group])

ALIASES = {
    "ctrl": {keyboard.Key.ctrl, keyboard.Key.ctrl_l, keyboard.Key.ctrl_r},
    "alt": {keyboard.Key.alt, keyboard.Key.alt_l, keyboard.Key.alt_r, keyboard.Key.alt_gr},
    "shift": {keyboard.Key.shift, keyboard.Key.shift_l, keyboard.Key.shift_r},
    "cmd": {keyboard.Key.cmd, keyboard.Key.cmd_l, keyboard.Key.cmd_r},
}


def _group(key):
    for name, keys in ALIASES.items():
        if key in keys:
            return name
    if isinstance(key, keyboard.Key):
        return key.name
    if isinstance(key, keyboard.KeyCode) and key.char:
        return key.char.lower()
    return None


class HoldHotkey:
    def __init__(self, combo, on_down, on_up, toggle=False):
        self.combo = {p.strip().lower() for p in combo.split("+")}
        self.on_down, self.on_up = on_down, on_up
        self.toggle = toggle
        self.held = set()
        self.active = False
        self.listener = keyboard.Listener(on_press=self._press, on_release=self._release)

    def start(self):
        self.listener.start()

    def stop(self):
        self.listener.stop()

    def _press(self, key):
        g = _group(key)
        if g:
            self.held.add(g)
        self.held = {k for k in self.held if k == g or _really_down(k)}
        if self.active and not self.combo <= self.held:
            self._released()
        if not self.active and self.combo <= self.held:
            self.active = True
            self.on_down()

    def _release(self, key):
        g = _group(key)
        self.held.discard(g)
        if self.active and g in self.combo:
            self._released()

    def _released(self):
        self.active = False
        if not self.toggle:
            self.on_up()
