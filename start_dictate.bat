@echo off
set HF_HUB_DISABLE_SYMLINKS_WARNING=1
start "" "%~dp0.venv\Scripts\pythonw.exe" "%~dp0app\run.pyw"
