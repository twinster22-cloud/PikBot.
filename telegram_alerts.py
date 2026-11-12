"""Free Telegram alerts for PikBot. No third-party messaging subscription required.

Configure TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID as Render secret environment
variables. Never commit either value. Call notify() from monitored events.
"""
import logging
import os
import requests

LOG = logging.getLogger(__name__)
TELEGRAM_API = "https://api.telegram.org"

def notify(message: str) -> bool:
    """Send a Telegram alert; return False if unconfigured or delivery fails."""
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    chat_id = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
    if not token or not chat_id:
        LOG.warning("Telegram alerts not configured")
        return False
    if not isinstance(message, str) or not message.strip():
        return False
    try:
        response = requests.post(
            f"{TELEGRAM_API}/bot{token}/sendMessage",
            json={"chat_id": chat_id, "text": message[:4096],
                  "disable_web_page_preview": True},
            timeout=8,
        )
        if response.status_code != 200:
            LOG.warning("Telegram delivery failed (HTTP %s)", response.status_code)
            return False
        return bool(response.json().get("ok"))
    except (requests.RequestException, ValueError):
        LOG.exception("Telegram delivery failed")
        return False

def launch_verified(details: str) -> bool:
    """Call ONLY after deployment and every required interactive check pass."""
    return notify("✅ PikBot launch verified\n" + details)

def critical_failure(details: str) -> bool:
    return notify("🚨 PikBot critical failure\n" + details)

def service_recovered(details: str) -> bool:
    return notify("✅ PikBot service recovered\n" + details)
