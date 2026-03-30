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
    FEATURE_FLAGS,
    HOUSEKEEPING_WINDOWS_HOURS,
    POST_STATUSES,
    TOKEN_REMINDER_DAYS,
)
from integrations.git_push import commit_and_push, should_auto_push
from integrations.telegram import send_message

LOGGER = get_logger("housekeeping")


def _config_bool(config: dict, key: str, default: bool) -> bool:
    value = config.get(key)
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in {"1", "true", "yes", "on"}:
            return True
        if lowered in {"0", "false", "no", "off"}:
            return False
    return default


def _config_float(config: dict, key: str, default: float) -> float:
    value = config.get(key)
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value.strip())
        except ValueError:
            return default
    return default


def _config_int(config: dict, key: str, default: int) -> int:
    value = config.get(key)
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    if isinstance(value, str):
        try:
            return int(value.strip())
        except ValueError:
            return default
    return default


def _hours_label(hours: float) -> str:
    if float(hours).is_integer():
        return str(int(hours))
    return str(hours)


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

    if not _config_bool(cfg, FEATURE_FLAGS.HOUSEKEEPING_ENABLED_KEY, FEATURE_FLAGS.DEFAULT_TRUE):
        LOGGER.audit("housekeeping_skipped: housekeeping disabled in config")
        LOGGER.info("Skip: housekeeping is disabled by config")
        return 0

    expiry_hours = _config_float(cfg, HOUSEKEEPING_WINDOWS_HOURS.EXPIRY_KEY, HOUSEKEEPING_WINDOWS_HOURS.EXPIRY)
    reminder_1_hours = _config_float(
        cfg,
        HOUSEKEEPING_WINDOWS_HOURS.REMINDER_1_KEY,
        HOUSEKEEPING_WINDOWS_HOURS.REMINDER_1_START,
    )
    reminder_2_hours = _config_float(
        cfg,
        HOUSEKEEPING_WINDOWS_HOURS.REMINDER_2_KEY,
        HOUSEKEEPING_WINDOWS_HOURS.REMINDER_2_START,
    )
    reminder_span = HOUSEKEEPING_WINDOWS_HOURS.REMINDER_1_END - HOUSEKEEPING_WINDOWS_HOURS.REMINDER_1_START
    reminder_1_end = reminder_1_hours + reminder_span
    reminder_2_end = reminder_2_hours + reminder_span
    linkedin_warning_days = _config_int(
        cfg,
        TOKEN_REMINDER_DAYS.LINKEDIN_WARNING_KEY,
        TOKEN_REMINDER_DAYS.LINKEDIN_WARNING,
    )
    linkedin_urgent_days = _config_int(
        cfg,
        TOKEN_REMINDER_DAYS.LINKEDIN_URGENT_KEY,
        TOKEN_REMINDER_DAYS.LINKEDIN_URGENT,
    )
    pat_warning_days = _config_int(cfg, TOKEN_REMINDER_DAYS.PAT_WARNING_KEY, TOKEN_REMINDER_DAYS.PAT_WARNING)

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
        if age_hours >= expiry_hours:
            post["status"] = POST_STATUSES.EXPIRED
            post["error"] = f"expired_after_{_hours_label(expiry_hours)}h"
            changed_posts = True
            expired_count += 1
            _post_bot_send(
                f"⌛ Draft expired after {_hours_label(expiry_hours)}h without approval.\n\n"
                f"Post: {post_id}\nTopic: {topic}"
            )
            LOGGER.audit(f"post_expired post_id={post_id}")
            continue

        if reminder_1_hours <= age_hours < reminder_1_end:
            reminders_sent += 1
            _post_bot_send(
                f"⏰ Approval reminder ({_hours_label(reminder_1_hours)}h)\n\n"
                f"Post: {post_id}\nTopic: {topic}\nStatus: {status}\n\n"
                f"Please approve, edit, or reject in Telegram."
            )
            LOGGER.audit(f"reminder_{_hours_label(reminder_1_hours)}h post_id={post_id}")
            continue

        if reminder_2_hours <= age_hours < reminder_2_end:
            reminders_sent += 1
            _post_bot_send(
                f"⏰ Approval reminder ({_hours_label(reminder_2_hours)}h)\n\n"
                f"Post: {post_id}\nTopic: {topic}\nStatus: {status}\n\n"
                f"Draft will expire automatically at {_hours_label(expiry_hours)}h."
            )
            LOGGER.audit(f"reminder_{_hours_label(reminder_2_hours)}h post_id={post_id}")

    linkedin_refreshed = parse_iso8601(cfg.get("linkedin_token_refreshed_at"))
    if linkedin_refreshed is None:
        LOGGER.audit("invalid_config: linkedin_token_refreshed_at")
    else:
        token_days = days_since(now, linkedin_refreshed)
        if within_half_day_window(token_days, linkedin_warning_days):
            _post_bot_send(
                "🔑 LinkedIn token expires soon (~10 days left).\n"
                "Please refresh it to avoid posting failures."
            )
            LOGGER.audit(f"token_warning day={linkedin_warning_days}")
        if within_half_day_window(token_days, linkedin_urgent_days):
            _post_bot_send(
                "🚨 LinkedIn token expires in ~2 days.\n"
                "Refresh immediately to avoid posting failures."
            )
            LOGGER.audit(f"token_warning day={linkedin_urgent_days}")

    pat_created = parse_iso8601(cfg.get("pat_created_at"))
    if pat_created is None:
        LOGGER.audit("invalid_config: pat_created_at")
    else:
        pat_days = days_since(now, pat_created)
        if within_half_day_window(pat_days, pat_warning_days):
            _post_bot_send(
                "🔐 GitHub fine-grained PAT is nearing expiry (~10 days left).\n"
                "Rotate PAT and update GitHub/Worker secrets."
            )
            LOGGER.audit(f"pat_warning day={pat_warning_days}")

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
