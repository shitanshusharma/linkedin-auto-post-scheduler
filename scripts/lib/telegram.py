import os
from typing import Optional

import requests


def send_message(
    bot_token: str,
    chat_id: str,
    text: str,
    *,
    parse_mode: Optional[str] = None,
) -> dict:
    """Telegram Bot API sendMessage. Raises on HTTP error."""
    url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
    payload: dict = {"chat_id": chat_id, "text": text}
    if parse_mode:
        payload["parse_mode"] = parse_mode
    r = requests.post(url, json=payload, timeout=30)
    r.raise_for_status()
    return r.json()


def log_bot_send(text: str) -> None:
    """Fire-and-forget style: log failures to stderr, never raise."""
    token = os.environ.get("TELEGRAM_LOG_BOT_TOKEN")
    chat = os.environ.get("TELEGRAM_LOG_CHAT_ID")
    if not token or not chat:
        return
    try:
        send_message(token, chat, text)
    except Exception as exc:  # noqa: BLE001 — intentional for audit channel
        print(f"[log_bot] failed: {exc}", flush=True)
