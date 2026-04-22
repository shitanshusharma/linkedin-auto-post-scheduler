"""Post record model and helpers."""

from __future__ import annotations

import secrets
from datetime import datetime, timezone

from pydantic import BaseModel, ConfigDict, Field

from core.constants import ACTIVE_POST_STATUSES, POST_RECORD


class PostContent(BaseModel):
    """Nested content block inside a post record."""

    hook: str
    body: str
    cta: str


class PostRecord(BaseModel):
    """A single post as stored in posts.json."""

    model_config = ConfigDict(extra="allow")

    id: str
    topic: str
    status: str
    approval_token: str | None = None
    telegram_message_id: int | None = None
    content: PostContent
    composed_text: str
    risk_flags: list[str] = Field(default_factory=list)
    proposed_edit: str | None = None
    publish_attempted_at: str | None = None
    created_at: str
    approved_at: str | None = None
    posted_at: str | None = None
    linkedin_post_id: str | None = None
    error: str | None = None


def next_post_id(existing_posts: list[PostRecord]) -> str:
    """Deterministic id: post_YYYY_MM_DD_NNN (NNN increments per day)."""
    now = datetime.now(timezone.utc)
    prefix = now.strftime(POST_RECORD.POST_ID_PREFIX_DATE_FORMAT)
    max_n = 0
    for p in existing_posts:
        if not p.id.startswith(prefix):
            continue
        suffix = p.id[len(prefix):]
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
) -> PostRecord:
    now = datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
    return PostRecord(
        id=post_id,
        topic=topic_title,
        status=ACTIVE_POST_STATUSES.PENDING,
        approval_token=approval_token,
        telegram_message_id=telegram_message_id,
        content=PostContent(hook=hook, body=body, cta=cta),
        composed_text=compose_text(hook, body, cta),
        risk_flags=risk_flags,
        created_at=now,
    )
