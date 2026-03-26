"""
12h housekeeping entrypoint (GitHub Actions).

Env: same Telegram log vars as generate.py; reads config.json + posts.json.
"""
from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path

_SCRIPT_DIR = Path(__file__).resolve().parent
if str(_SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPT_DIR))

from lib.paths import repo_root
from lib.repo_json import read_json
from lib.telegram import log_bot_send


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
    log_bot_send(f"[housekeeping] tick at {now.isoformat()} — reminders/expiry TODO")

    # TODO: pending age → 12h/24h Post Bot reminders, 48h → expired
    # TODO: linkedin_token_refreshed_at + 60d → day 50/58; pat_created_at + 90d → day 80
    _ = cfg
    _ = posts
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
