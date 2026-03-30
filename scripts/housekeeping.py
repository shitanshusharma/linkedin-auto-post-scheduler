"""
12h housekeeping entrypoint (GitHub Actions).

Env:
  TELEGRAM_POST_BOT_TOKEN, TELEGRAM_CHAT_ID
  TELEGRAM_LOG_BOT_TOKEN, TELEGRAM_LOG_CHAT_ID — optional Log Bot
"""
from __future__ import annotations

import os
import sys
import traceback
from datetime import datetime, timezone
from pathlib import Path

try:
    from dotenv import load_dotenv
except ImportError:
    load_dotenv = None

_SCRIPT_DIR = Path(__file__).resolve().parent
if str(_SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPT_DIR))

from common.logger import get_logger
from common.paths import repo_root
from common.repo_json import read_json, write_json
from common.time_utils import days_since, hours_since, parse_iso8601, within_half_day_window
from core.constants import (
    ACTIVE_POST_STATUSES,
    ERROR_MESSAGES,
    HOUSEKEEPING_WINDOWS_HOURS,
    POST_STATUSES,
    TOKEN_REMINDER_DAYS,
)
from integrations.git_push import commit_and_push, should_auto_push
from integrations.telegram import send_message

LOGGER = get_logger("housekeeping")


def _post_bot_send(text: str) -> None:
    token = os.environ.get("TELEGRAM_POST_BOT_TOKEN", "").strip()
    chat_id = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
    if not token or not chat_id:
        return
    try:
        send_message(token, chat_id, text)
    except Exception as exc:  # noqa: BLE001
        LOGGER.audit(f"post_bot_send_failed: {exc}")


def main() -> int:
    if load_dotenv is not None:
        load_dotenv(repo_root() / ".env")

    root = repo_root()
    config_path = root / "config.json"
    posts_path = root / "posts.json"

    cfg = read_json(config_path)
    posts = read_json(posts_path)
    if not isinstance(cfg, dict):
        LOGGER.error(ERROR_MESSAGES.CONFIG_JSON_OBJECT_REQUIRED)
        return 1
    if not isinstance(posts, list):
        LOGGER.error(ERROR_MESSAGES.POSTS_JSON_ARRAY_REQUIRED_HOUSEKEEPING)
        return 1

    now = datetime.now(timezone.utc)
    changed_posts = False
    reminders_sent = 0
    expired_count = 0

    for post in posts:
        if not isinstance(post, dict):
            continue
        status = str(post.get("status", ""))
        if status not in ACTIVE_POST_STATUSES.ALL:
            continue

        post_id = str(post.get("id", "?"))
        topic = str(post.get("topic", "Unknown"))
        created_at = parse_iso8601(post.get("created_at"))
        if created_at is None:
            LOGGER.audit(f"invalid_created_at post_id={post_id}")
            continue

        age_hours = hours_since(now, created_at)
        if age_hours >= HOUSEKEEPING_WINDOWS_HOURS.EXPIRY:
            post["status"] = POST_STATUSES.EXPIRED
            post["error"] = "expired_after_48h"
            changed_posts = True
            expired_count += 1
            _post_bot_send(
                f"⌛ Draft expired after 48h without approval.\n\n"
                f"Post: {post_id}\nTopic: {topic}"
            )
            LOGGER.audit(f"post_expired post_id={post_id}")
            continue

        if HOUSEKEEPING_WINDOWS_HOURS.REMINDER_1_START <= age_hours < HOUSEKEEPING_WINDOWS_HOURS.REMINDER_1_END:
            reminders_sent += 1
            _post_bot_send(
                f"⏰ Approval reminder (12h)\n\n"
                f"Post: {post_id}\nTopic: {topic}\nStatus: {status}\n\n"
                f"Please approve, edit, or reject in Telegram."
            )
            LOGGER.audit(f"reminder_12h post_id={post_id}")
            continue

        if HOUSEKEEPING_WINDOWS_HOURS.REMINDER_2_START <= age_hours < HOUSEKEEPING_WINDOWS_HOURS.REMINDER_2_END:
            reminders_sent += 1
            _post_bot_send(
                f"⏰ Approval reminder (24h)\n\n"
                f"Post: {post_id}\nTopic: {topic}\nStatus: {status}\n\n"
                f"Draft will expire automatically at 48h."
            )
            LOGGER.audit(f"reminder_24h post_id={post_id}")

    linkedin_refreshed = parse_iso8601(cfg.get("linkedin_token_refreshed_at"))
    if linkedin_refreshed is None:
        LOGGER.audit("invalid_config: linkedin_token_refreshed_at")
    else:
        token_days = days_since(now, linkedin_refreshed)
        if within_half_day_window(token_days, TOKEN_REMINDER_DAYS.LINKEDIN_WARNING):
            _post_bot_send(
                "🔑 LinkedIn token expires soon (~10 days left).\n"
                "Please refresh it to avoid posting failures."
            )
            LOGGER.audit("token_warning day=50")
        if within_half_day_window(token_days, TOKEN_REMINDER_DAYS.LINKEDIN_URGENT):
            _post_bot_send(
                "🚨 LinkedIn token expires in ~2 days.\n"
                "Refresh immediately to avoid posting failures."
            )
            LOGGER.audit("token_warning day=58")

    pat_created = parse_iso8601(cfg.get("pat_created_at"))
    if pat_created is None:
        LOGGER.audit("invalid_config: pat_created_at")
    else:
        pat_days = days_since(now, pat_created)
        if within_half_day_window(pat_days, TOKEN_REMINDER_DAYS.PAT_WARNING):
            _post_bot_send(
                "🔐 GitHub fine-grained PAT is nearing expiry (~10 days left).\n"
                "Rotate PAT and update GitHub/Worker secrets."
            )
            LOGGER.audit("pat_warning day=80")

    if changed_posts:
        write_json(posts_path, posts)
        if should_auto_push():
            try:
                if commit_and_push(root, ["posts.json"], "chore: expire stale pending drafts"):
                    LOGGER.info(f"Expired stale drafts: {expired_count}")
                else:
                    LOGGER.info("No git changes to commit")
            except Exception as exc:  # noqa: BLE001
                LOGGER.audit(f"git_push_failed: {exc}")
                LOGGER.error(traceback.format_exc())
                return 1
        else:
            LOGGER.info("Updated posts.json — commit manually or set GIT_PUSH=1")

    LOGGER.audit(f"tick={now.isoformat()} reminders_sent={reminders_sent} expired={expired_count}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
