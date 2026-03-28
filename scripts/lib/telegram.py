import os
from typing import Any, Optional

import requests

# Telegram Bot API: callback_data max length (bytes)
MAX_CALLBACK_DATA_BYTES = 64


def send_message(
    bot_token: str,
    chat_id: str,
    text: str,
    *,
    parse_mode: Optional[str] = None,
    reply_markup: Optional[dict[str, Any]] = None,
) -> dict:
    """Telegram Bot API sendMessage. Raises on HTTP error."""
    url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
    payload: dict = {"chat_id": chat_id, "text": text}
    if parse_mode:
        payload["parse_mode"] = parse_mode
    if reply_markup is not None:
        payload["reply_markup"] = reply_markup
    r = requests.post(url, json=payload, timeout=30)
    r.raise_for_status()
    return r.json()


def inline_approve_edit_reject(post_id: str, approval_token: str) -> dict[str, Any]:
    """Inline keyboard: a:/e:/r: + post_id + token (callback_data ≤ 64 bytes)."""
    def cb(prefix: str) -> str:
        s = f"{prefix}:{post_id}:{approval_token}"
        n = len(s.encode("utf-8"))
        if n > MAX_CALLBACK_DATA_BYTES:
            raise ValueError(
                f"callback_data exceeds {MAX_CALLBACK_DATA_BYTES} bytes ({n}): "
                "shorten post_id or approval_token"
            )
        return s

    return {
        "inline_keyboard": [
            [
                {"text": "✅ Approve", "callback_data": cb("a")},
                {"text": "✏️ Edit", "callback_data": cb("e")},
                {"text": "❌ Reject", "callback_data": cb("r")},
            ]
        ]
    }


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
