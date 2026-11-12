"""Best-effort in-process PikBot endpoint monitor.

This is not an external uptime monitor: it cannot alert when Render is fully down,
or detect failed builds that never start the app.
"""
import logging
import os
import threading
import time
from urllib.parse import urljoin
import requests

LOG = logging.getLogger(__name__)
_STARTED = False
_LOCK = threading.Lock()

def start_monitor():
    global _STARTED
    if os.getenv("PIKBOT_MONITOR_ENABLED", "1") != "1":
        return
    with _LOCK:
        if _STARTED:
            return
        _STARTED = True
    threading.Thread(target=_monitor_loop, name="pikbot-telegram-monitor", daemon=True).start()

def _monitor_loop():
    # Render can run multiple Gunicorn workers. Elect one process to monitor.
    import fcntl
    lock_file = open("/tmp/pikbot-telegram-monitor.lock", "a+")
    try:
        fcntl.flock(lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        return
    from telegram_alerts import critical_failure, service_recovered
    base = os.getenv("PIKBOT_MONITOR_URL", "https://pikbot-6orx.onrender.com").rstrip("/") + "/"
    paths = ("/health", "/about-pikbot", "/login")
    failures = 0
    incident = False
    # Wait for deployment warm-up; no alert on the first transient failure.
    time.sleep(90)
    while True:
        errors = []
        for path in paths:
            try:
                response = requests.get(urljoin(base, path.lstrip("/")),
                                        timeout=12, allow_redirects=False)
                if response.status_code != 200:
                    errors.append(f"{path}: HTTP {response.status_code}")
            except requests.RequestException as exc:
                errors.append(f"{path}: {type(exc).__name__}")
        if errors:
            failures += 1
            if failures >= 3 and not incident:
                incident = True
                critical_failure("Endpoint checks failed three times: " + "; ".join(errors))
        else:
            failures = 0
            if incident:
                incident = False
                service_recovered("All monitored endpoints responding: " + ", ".join(paths))
        time.sleep(300)
