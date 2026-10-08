"""Watchdog: runs main.py and restarts it if it crashes. Quitting from the tray (exit code 0) stops it.
Launching it again (Ctrl+Alt+D) first closes any running Dictate, so it doubles as a restart key."""
import logging
import os
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
logging.basicConfig(filename=os.path.join(os.path.dirname(HERE), "dictate.log"), level=logging.INFO,
                    format="%(asctime)s %(levelname)s %(message)s")
NO_WINDOW = subprocess.CREATE_NO_WINDOW

# close any Dictate already running (old watchdog first, so it can't restart its app)
me = {os.getpid(), os.getppid()}
ps = (r"Get-CimInstance Win32_Process | "
      r"? { $_.CommandLine -like '*dictate\app\run.pyw*' -or $_.CommandLine -like '*dictate\app\main.py*' } | "
      r"sort { $_.CommandLine -notlike '*run.pyw*' } | % { $_.ProcessId }")
out = subprocess.run(["powershell", "-NoProfile", "-Command", ps], capture_output=True, text=True,
                     creationflags=NO_WINDOW).stdout
old = [int(x) for x in out.split() if x.isdigit() and int(x) not in me]
for pid in old:
    subprocess.run(["taskkill", "/F", "/PID", str(pid)], capture_output=True, creationflags=NO_WINDOW)
logging.info("watchdog: started, closed %d old process(es). Restart key: Ctrl+Alt+D", len(old))
time.sleep(1)

crashes = []
while True:
    code = subprocess.call([sys.executable, os.path.join(HERE, "main.py")], cwd=os.path.dirname(HERE))
    if code == 0:
        break
    now = time.time()
    crashes = [t for t in crashes if now - t < 60] + [now]
    if len(crashes) > 5:  # crashing in a loop: give up instead of spinning
        logging.error("watchdog: app crashed %d times in a minute, stopping", len(crashes))
        break
    logging.warning("watchdog: app exited with code %s, restarting", code)
    time.sleep(2)
