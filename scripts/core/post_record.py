"""Build Post Storage Schema objects and deterministic ids."""

from __future__ import annotations

import secrets
from datetime import datetime, timezone
from typing import Any

from core.constants import ACTIVE_POST_STATUSES, POST_RECORD


def next_post_id(existing_posts: list[dict[str, Any]]) -> str:
    """Deterministic id: post_YYYY_MM_DD_NNN (NNN increments per day)."""
    now = datetime.now(timezone.utc)
    prefix = now.strftime(POST_RECORD.POST_ID_PREFIX_DATE_FORMAT)
    max_n = 0
    for p in existing_posts:
        pid = p.get("id")
        if not isinstance(pid, str) or not pid.startswith(prefix):
            continue
        suffix = pid[len(prefix) :]
        try:
            max_n = max(max_n, int(suffix))
        except ValueError:
            continue
    return f"{prefix}{max_n + 1:0{POST_RECORD.POST_ID_SEQUENCE_WIDTH}d}"


def new_approval_token() -> str:
    return secrets.token_hex(POST_RECORD.APPROVAL_TOKEN_NUM_BYTES)


def compose_text(hook: str, body: str, cta: str) -> str:
    return f"{hook}\n\n{body}\n\n{cta}"


def build_post(
    *,
    post_id: str,
    topic_title: str,
    hook: str,
    body: str,
    cta: str,
    risk_flags: list[str],
    approval_token: str,
    telegram_message_id: int,
) -> dict[str, Any]:
    now = datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
    composed = compose_text(hook, body, cta)
    return {
        "id": post_id,
        "topic": topic_title,
        "status": ACTIVE_POST_STATUSES.PENDING,
        "approval_token": approval_token,
        "telegram_message_id": telegram_message_id,
        "content": {"hook": hook, "body": body, "cta": cta},
        "composed_text": composed,
        "risk_flags": risk_flags,
        "proposed_edit": None,
        "publish_attempted_at": None,
        "created_at": now,
        "approved_at": None,
        "posted_at": None,
        "linkedin_post_id": None,
        "error": None,
    }

