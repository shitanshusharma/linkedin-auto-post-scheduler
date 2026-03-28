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
from typing import Any

_SCRIPT_DIR = Path(__file__).resolve().parent
if str(_SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPT_DIR))

from lib.git_push import commit_and_push, should_auto_push
from lib.paths import repo_root
from lib.repo_json import read_json, write_json
from lib.telegram import log_bot_send, send_message

ACTIVE = frozenset({"pending", "editing", "confirming_edit"})


def _parse_iso8601(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip()
    if text.endswith("Z"):
        text = f"{text[:-1]}+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _post_bot_send(text: str) -> None:
    token = os.environ.get("TELEGRAM_POST_BOT_TOKEN", "").strip()
    chat_id = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
    if not token or not chat_id:
        return
    try:
        send_message(token, chat_id, text)
    except Exception as exc:  # noqa: BLE001
        log_bot_send(f"[housekeeping] post_bot_send_failed: {exc}")


def _days_since(now: datetime, instant: datetime) -> float:
    return (now - instant).total_seconds() / 86400.0


def _hours_since(now: datetime, instant: datetime) -> float:
    return (now - instant).total_seconds() / 3600.0


def _within_half_day_window(days_elapsed: float, threshold_days: int) -> bool:
    return threshold_days <= days_elapsed < (threshold_days + 0.5)


def main() -> int:
    try:
        from dotenv import load_dotenv

        load_dotenv(repo_root() / ".env")
    except ImportError:
        pass

    root = repo_root()
    config_path = root / "config.json"
    posts_path = root / "posts.json"

    cfg = read_json(config_path)
    posts = read_json(posts_path)
    if not isinstance(cfg, dict):
        print("config.json must be an object", file=sys.stderr)
        return 1
    if not isinstance(posts, list):
        print("posts.json must be an array", file=sys.stderr)
        return 1

    now = datetime.now(timezone.utc)
    changed_posts = False
    reminders_sent = 0
    expired_count = 0

    for post in posts:
        if not isinstance(post, dict):
            continue
        status = str(post.get("status", ""))
        if status not in ACTIVE:
            continue

        post_id = str(post.get("id", "?"))
        topic = str(post.get("topic", "Unknown"))
        created_at = _parse_iso8601(post.get("created_at"))
        if created_at is None:
            log_bot_send(f"[housekeeping] invalid_created_at post_id={post_id}")
            continue

        age_hours = _hours_since(now, created_at)
        if age_hours >= 48.0:
            post["status"] = "expired"
            post["error"] = "expired_after_48h"
            changed_posts = True
            expired_count += 1
            _post_bot_send(
                f"⌛ Draft expired after 48h without approval.\n\n"
                f"Post: {post_id}\nTopic: {topic}"
            )
            log_bot_send(f"[housekeeping] post_expired post_id={post_id}")
            continue

        if 12.0 <= age_hours < 24.0:
            reminders_sent += 1
            _post_bot_send(
                f"⏰ Approval reminder (12h)\n\n"
                f"Post: {post_id}\nTopic: {topic}\nStatus: {status}\n\n"
                f"Please approve, edit, or reject in Telegram."
            )
            log_bot_send(f"[housekeeping] reminder_12h post_id={post_id}")
            continue

        if 24.0 <= age_hours < 36.0:
            reminders_sent += 1
            _post_bot_send(
                f"⏰ Approval reminder (24h)\n\n"
                f"Post: {post_id}\nTopic: {topic}\nStatus: {status}\n\n"
                f"Draft will expire automatically at 48h."
            )
            log_bot_send(f"[housekeeping] reminder_24h post_id={post_id}")

    linkedin_refreshed = _parse_iso8601(cfg.get("linkedin_token_refreshed_at"))
    if linkedin_refreshed is None:
        log_bot_send("[housekeeping] invalid_config: linkedin_token_refreshed_at")
    else:
        token_days = _days_since(now, linkedin_refreshed)
        if _within_half_day_window(token_days, 50):
            _post_bot_send(
                "🔑 LinkedIn token expires soon (~10 days left).\n"
                "Please refresh it to avoid posting failures."
            )
            log_bot_send("[housekeeping] token_warning day=50")
        if _within_half_day_window(token_days, 58):
            _post_bot_send(
                "🚨 LinkedIn token expires in ~2 days.\n"
                "Refresh immediately to avoid posting failures."
            )
            log_bot_send("[housekeeping] token_warning day=58")

    pat_created = _parse_iso8601(cfg.get("pat_created_at"))
    if pat_created is None:
        log_bot_send("[housekeeping] invalid_config: pat_created_at")
    else:
        pat_days = _days_since(now, pat_created)
        if _within_half_day_window(pat_days, 80):
            _post_bot_send(
                "🔐 GitHub fine-grained PAT is nearing expiry (~10 days left).\n"
                "Rotate PAT and update GitHub/Worker secrets."
            )
            log_bot_send("[housekeeping] pat_warning day=80")

    if changed_posts:
        write_json(posts_path, posts)
        if should_auto_push():
            try:
                if commit_and_push(root, ["posts.json"], "chore: expire stale pending drafts"):
                    print(f"Expired stale drafts: {expired_count}", flush=True)
                else:
                    print("No git changes to commit", flush=True)
            except Exception as exc:  # noqa: BLE001
                log_bot_send(f"[housekeeping] git_push_failed: {exc}")
                print(traceback.format_exc(), file=sys.stderr)
                return 1
        else:
            print("Updated posts.json — commit manually or set GIT_PUSH=1", flush=True)

    log_bot_send(
        f"[housekeeping] tick={now.isoformat()} reminders_sent={reminders_sent} "
        f"expired={expired_count}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
