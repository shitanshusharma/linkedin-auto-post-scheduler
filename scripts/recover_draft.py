"""
Idempotent recovery: align posts.json with a Telegram draft when generation
partially failed (e.g. message sent but git push failed).

Does not call Telegram or LLM — you supply post_id, telegram_message_id, approval_token,
and content from the draft / callback_data.

Safe to re-run: if a matching pending post already exists, exits 0 without changes.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

try:
    from dotenv import load_dotenv
except ImportError:
    load_dotenv = None

_SCRIPT_DIR = Path(__file__).resolve().parent
if str(_SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPT_DIR))

from lib.git_push import commit_and_push, should_auto_push
from lib.paths import repo_root
from lib.post_record import build_post
from lib.repo_json import read_json, write_json


def _normalize_post(p: dict) -> dict:
    """Stable subset for equality (id + pending payload)."""
    keys = (
        "id",
        "topic",
        "status",
        "approval_token",
        "telegram_message_id",
        "content",
        "composed_text",
        "risk_flags",
    )
    return {k: p.get(k) for k in keys if k in p}


def main() -> int:
    if load_dotenv is not None:
        load_dotenv(repo_root() / ".env")

    parser = argparse.ArgumentParser(description="Recover / upsert a pending draft in posts.json")
    parser.add_argument("--from-json", type=Path, default=None, help="Path to JSON with all fields")
    parser.add_argument("--post-id", default=None)
    parser.add_argument("--telegram-message-id", type=int, default=None)
    parser.add_argument("--approval-token", default=None)
    parser.add_argument("--topic", default=None)
    parser.add_argument("--hook", default=None)
    parser.add_argument("--body", default=None)
    parser.add_argument("--cta", default=None)
    parser.add_argument("--risk-flags", default="", help="Comma-separated; empty for none")
    args = parser.parse_args()

    root = repo_root()
    posts_path = root / "posts.json"

    if args.from_json:
        payload = json.loads(args.from_json.read_text(encoding="utf-8"))
    else:
        required = (
            "post_id",
            "telegram_message_id",
            "approval_token",
            "topic",
            "hook",
            "body",
            "cta",
        )
        missing = [
            n
            for n, v in (
                ("post_id", args.post_id),
                ("telegram_message_id", args.telegram_message_id),
                ("approval_token", args.approval_token),
                ("topic", args.topic),
                ("hook", args.hook),
                ("body", args.body),
                ("cta", args.cta),
            )
            if v is None or (isinstance(v, str) and not str(v).strip())
        ]
        if missing:
            print(f"Missing or empty: {', '.join(missing)} (or use --from-json)", file=sys.stderr)
            return 1
        risk: list[str] = []
        if args.risk_flags.strip():
            risk = [x.strip() for x in args.risk_flags.split(",") if x.strip()]
        payload = {
            "post_id": args.post_id,
            "telegram_message_id": args.telegram_message_id,
            "approval_token": args.approval_token,
            "topic": args.topic,
            "hook": args.hook,
            "body": args.body,
            "cta": args.cta,
            "risk_flags": risk,
        }

    post_id = str(payload["post_id"]).strip()
    telegram_message_id = int(payload["telegram_message_id"])
    approval_token = str(payload["approval_token"]).strip()
    topic_title = str(payload["topic"]).strip()
    hook = str(payload["hook"])
    body = str(payload["body"])
    cta = str(payload["cta"])
    risk_flags = payload.get("risk_flags") or []
    if not isinstance(risk_flags, list):
        print("risk_flags must be a list", file=sys.stderr)
        return 1
    risk_flags = [str(x) for x in risk_flags]

    if not post_id or not topic_title:
        print("post_id and topic are required", file=sys.stderr)
        return 1

    posts = read_json(posts_path)
    if not isinstance(posts, list):
        print("posts.json must be a JSON array", file=sys.stderr)
        return 1

    existing: dict | None = None
    existing_index: int | None = None
    for i, p in enumerate(posts):
        if isinstance(p, dict) and p.get("id") == post_id:
            existing = p
            existing_index = i
            break

    new_post = build_post(
        post_id=post_id,
        topic_title=topic_title,
        hook=hook,
        body=body,
        cta=cta,
        risk_flags=risk_flags,
        approval_token=approval_token,
        telegram_message_id=telegram_message_id,
    )

    if existing is not None and existing.get("status") != "pending":
        print(
            f"Post {post_id} exists with status {existing.get('status')} — refuse to overwrite",
            file=sys.stderr,
        )
        return 1

    if existing is not None and _normalize_post(existing) == _normalize_post(new_post):
        print(f"Already in sync (idempotent): {post_id}", flush=True)
        return 0

    if existing is None:
        posts.append(new_post)
        print(f"Inserted pending post {post_id}", flush=True)
    else:
        assert existing_index is not None
        posts[existing_index] = new_post
        print(f"Updated pending post {post_id}", flush=True)

    write_json(posts_path, posts)

    if should_auto_push():
        try:
            commit_and_push(root, ["posts.json"], f"chore: recover draft {post_id}")
        except Exception as exc:  # noqa: BLE001
            print(f"git push failed: {exc}", file=sys.stderr)
            return 1
    else:
        print("Wrote posts.json — commit manually or set GIT_PUSH=1", flush=True)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
