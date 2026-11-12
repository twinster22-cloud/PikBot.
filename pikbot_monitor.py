"""PikBot Telegram monitoring. In-process checks are not independent uptime checks.

Only report observable endpoint failures/recoveries. Never equate HTTP 200 with
successful authenticated UI interactions or fresh provider offers.
"""
import fcntl
import logging
import os
import threading
import time
from urllib.parse import urljoin

import requests

LOG = logging.getLogger(__name__)
_STARTED = False
_LOCK = threading.Lock()
PATHS = ("/health", "/about-pikbot", "/login")


def start_monitor():
    global _STARTED
    if os.getenv("PIKBOT_MONITOR_ENABLED", "1") != "1":
        return
    with _LOCK:
        if _STARTED:
            return
        _STARTED = True
    threading.Thread(target=_monitor_loop, name="pikbot-telegram-monitor", daemon=True).start()


def check_public_routes(base, session=None):
    """Check availability and About disclaimer, not browser button behavior."""
    client = session or requests
    errors = []
    for path in PATHS:
        try:
            response = client.get(urljoin(base, path.lstrip("/")),
                                  timeout=12, allow_redirects=False)
            if response.status_code != 200:
                errors.append(f"{path}: HTTP {response.status_code}")
            elif path == "/about-pikbot":
                body = response.text.lower()
                if "important disclaimer" not in body or "no pick" not in body:
                    errors.append("/about-pikbot: required disclaimer missing")
            elif path == "/health":
                if response.json().get("status") != "ok":
                    errors.append("/health: unexpected health payload")
        except (requests.RequestException, ValueError) as exc:
            errors.append(f"{path}: {type(exc).__name__}")
    return errors


def _monitor_loop():
    lock_file = open("/tmp/pikbot-telegram-monitor.lock", "a+")
    try:
        fcntl.flock(lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        return
    from telegram_alerts import critical_failure, service_recovered
    base = os.getenv("PIKBOT_MONITOR_URL", "https://pikbot-6orx.onrender.com").rstrip("/") + "/"
    failures = 0
    incident = False
    time.sleep(90)
    while True:
        errors = check_public_routes(base)
        if errors:
            failures += 1
            if failures >= 3 and not incident:
                incident = True
                critical_failure("Public route checks failed three times: " + "; ".join(errors)
                                 + "\nAuthenticated features and scanner freshness are not verified by this check.")
        else:
            failures = 0
            if incident:
                incident = False
                service_recovered("Public routes and About disclaimer responding. "
                                  "Auto Pick, scanner freshness, and member/owner interactions still require separate verification.")
        time.sleep(300)
