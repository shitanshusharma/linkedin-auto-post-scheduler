"""
Weekly generation entrypoint (GitHub Actions).

Env (secrets / vars):
  GH_FINE_GRAINED_PAT — optional for future Contents API; checkout uses workspace files
  TELEGRAM_POST_BOT_TOKEN, TELEGRAM_CHAT_ID — Post Bot
  TELEGRAM_LOG_BOT_TOKEN, TELEGRAM_LOG_CHAT_ID — Log Bot (optional)
  WF_ACTION — generate | resend (from workflow_dispatch)
  WF_POST_ID — optional for resend
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

_SCRIPT_DIR = Path(__file__).resolve().parent
if str(_SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPT_DIR))

from lib.paths import repo_root
from lib.repo_json import read_json
from lib.telegram import log_bot_send

ACTIVE = frozenset({"pending", "editing", "confirming_edit"})


def main() -> int:
    try:
        from dotenv import load_dotenv

        load_dotenv(repo_root() / ".env")
    except ImportError:
        pass

    root = repo_root()
    posts_path = root / "posts.json"
    topics_path = root / "topics.json"

    posts = read_json(posts_path)
    if not isinstance(posts, list):
        print("posts.json must be a JSON array", file=sys.stderr)
        return 1

    action = os.environ.get("WF_ACTION") or "generate"
    if action == "resend":
        log_bot_send("[generate] resend not implemented yet")
        print("resend: TODO", flush=True)
        return 0

    for p in posts:
        if isinstance(p, dict) and p.get("status") in ACTIVE:
            pid = p.get("id", "?")
            log_bot_send(f"[generate] generation_skipped: active post exists ({pid})")
            print(f"Skip: active post {pid}", flush=True)
            return 0

    topics = read_json(topics_path)
    if not isinstance(topics, list) or not topics:
        log_bot_send("[generate] topic_backlog_exhausted")
        print("No topics", flush=True)
        return 0

    unused = [t for t in topics if isinstance(t, dict) and not t.get("used")]
    if not unused:
        log_bot_send("[generate] topic_backlog_exhausted")
        print("All topics used", flush=True)
        return 0

    log_bot_send("[generate] TODO: LLM generation — scaffold only")
    print("TODO: implement LLM + Post Storage Schema + sendMessage + commit", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
