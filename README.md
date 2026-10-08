# Dictate

Press a hotkey, talk, and the text is pasted where your cursor is (terminals too). Runs **100% locally**, on **GPU or CPU**.

![demo](docs/demo.jpg)

▶ [Watch the demo video](docs/demo.mp4)

- Floating **bubble** shows state: 🎙 ready · ● listening · … transcribing · 📋 result (drag it, click it to open your notes)
- Default hotkey **Ctrl + Win** (toggle: press to start, press again to stop)
- Models: NVIDIA Parakeet v3 (default, best in my benchmark in `bench/results.md`), Whisper, Canary
- Languages: auto-detects Spanish / English / German

## Quick start (Windows)

```powershell
git clone https://github.com/Japulgarin/dictate
cd dictate
python -m venv .venv
.venv\Scripts\pip install -r requirements.txt
start_dictate.bat
```

You should see the bubble in the bottom-right corner and a tray icon. If not, check `dictate.log`.

## GPU or CPU

In `app/config.toml`:

```toml
device = "auto"   # auto = NVIDIA GPU if found, otherwise CPU | "cuda" | "cpu"
```

For CPU-only machines install `onnxruntime` instead of `onnxruntime-gpu`. Parakeet works well on CPU (first load ~30 s, then a few seconds per utterance); `whisper-small` is the lightest option.

## Start it with a hotkey / at login

1. Right-click `start_dictate.bat` → Send to → Desktop (create shortcut).
2. Shortcut → Properties → **Shortcut key** → press e.g. `Ctrl+Alt+D`. Pressing it again restarts the app.
3. To start at login, copy the shortcut into `shell:startup` (Win+R).

## Settings

Edit `app/config.toml` and restart: hotkey, `hotkey_mode` (`toggle`/`hold`), model, language, `auto_paste`, `silence_rms` (raise it if noise gets transcribed, lower it if it says "heard nothing").
No text appears? Check your default microphone in Windows Sound settings.
