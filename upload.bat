@echo off
rem Double-click or run from a terminal: upload.bat "what changed"
powershell -NoProfile -ExecutionPolicy RemoteSigned -File "%~dp0upload.ps1" %*
pause
