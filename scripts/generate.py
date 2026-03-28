"""
Weekly generation entrypoint (GitHub Actions).

Env:
  GITHUB_TOKEN — GitHub Models (models:read); set automatically in Actions
  TELEGRAM_POST_BOT_TOKEN, TELEGRAM_CHAT_ID
  TELEGRAM_LOG_BOT_TOKEN, TELEGRAM_LOG_CHAT_ID — optional Log Bot
  GITHUB_MODEL — optional, default openai/gpt-4o-mini
  WF_ACTION — generate | resend
  WF_POST_ID — required for resend
"""
from __future__ import annotations

import os
import sys
import traceback
from pathlib import Path

_SCRIPT_DIR = Path(__file__).resolve().parent
if str(_SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPT_DIR))

from lib.git_push import commit_and_push, should_auto_push
from lib.llm import generate_post_json
from lib.llm_output import LlmPostOutput, to_llm_post_output, validate_llm_output
from lib.paths import repo_root
from lib.post_record import build_post, compose_text, new_approval_token, next_post_id
from lib.repo_json import read_json, write_json
from lib.telegram import inline_approve_edit_reject, log_bot_send, send_message

ACTIVE = frozenset({"pending", "editing", "confirming_edit"})


def _draft_message(topic: str, composed: str, risk_flags: list[str]) -> str:
    preview = composed[:500] + ("..." if len(composed) > 500 else "")
    flags = ", ".join(risk_flags) if risk_flags else "None"
    return (
        f"📝 New LinkedIn Draft\n\n"
        f"Topic: {topic}\n\n"
        f"---\n{preview}\n---\n\n"
        f"Risk Flags: {flags}"
    )


def _run_llm(topic_title: str) -> LlmPostOutput | None:
    token = os.environ.get("GITHUB_TOKEN", "").strip()
    if not token:
        return None

    notes: list[str] = []
    for attempt, strict in enumerate((False, True), start=1):
        try:
            data = generate_post_json(token=token, topic_title=topic_title, strict_retry=strict)
            ok, err = validate_llm_output(data)
            if ok:
                return to_llm_post_output(data)
            notes.append(f"attempt{attempt} validation: {err}")
        except Exception as exc:  # noqa: BLE001
            notes.append(f"attempt{attempt} parse: {type(exc).__name__}: {exc}")
    detail = " | ".join(notes)
    log_bot_send(f"[generate] llm_output_invalid: {detail}")
    print(f"LLM validation failed: {detail}", file=sys.stderr)
    return None


def _require_telegram_env() -> tuple[str, str] | None:
    post_token = os.environ.get("TELEGRAM_POST_BOT_TOKEN", "").strip()
    chat_id = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
    if not post_token or not chat_id:
        print("TELEGRAM_POST_BOT_TOKEN and TELEGRAM_CHAT_ID are required", file=sys.stderr)
        return None
    return post_token, chat_id


def _resend_pending_post(*, root: Path, posts_path: Path, posts: list[dict]) -> int:
    post_id = os.environ.get("WF_POST_ID", "").strip()
    if not post_id:
        log_bot_send("[generate] resend_invalid: missing WF_POST_ID")
        print("WF_POST_ID is required for resend action", file=sys.stderr)
        return 1

    target: dict | None = None
    for p in posts:
        if p.get("id") == post_id:
            target = p
            break

    if target is None:
        log_bot_send(f"[generate] resend_invalid: post not found ({post_id})")
        print(f"Post not found: {post_id}", file=sys.stderr)
        return 1

    status = str(target.get("status", ""))
    if status != "pending":
        log_bot_send(f"[generate] resend_invalid: status={status} post_id={post_id}")
        print(f"resend requires pending post; got status={status}", file=sys.stderr)
        return 1

    approval_token = str(target.get("approval_token", "")).strip()
    topic_title = str(target.get("topic", "")).strip()
    composed = str(target.get("composed_text", "")).strip()
    raw_flags = target.get("risk_flags")
    risk_flags = [x for x in raw_flags if isinstance(x, str)] if isinstance(raw_flags, list) else []
    if not approval_token:
        log_bot_send(f"[generate] resend_invalid: missing approval_token post_id={post_id}")
        print(f"Post missing approval_token: {post_id}", file=sys.stderr)
        return 1
    if not composed:
        log_bot_send(f"[generate] resend_invalid: missing composed_text post_id={post_id}")
        print(f"Post missing composed_text: {post_id}", file=sys.stderr)
        return 1
    if not topic_title:
        topic_title = "Unknown"

    telegram_env = _require_telegram_env()
    if telegram_env is None:
        return 1
    post_token, chat_id = telegram_env

    try:
        markup = inline_approve_edit_reject(post_id, approval_token)
    except ValueError as exc:
        log_bot_send(f"[generate] callback_data_invalid: {exc}")
        print(str(exc), file=sys.stderr)
        return 1

    try:
        tg = send_message(
            post_token,
            chat_id,
            _draft_message(topic_title, composed, risk_flags),
            reply_markup=markup,
        )
    except Exception as exc:  # noqa: BLE001
        log_bot_send(f"[generate] telegram_send_failed: {exc}")
        print(traceback.format_exc(), file=sys.stderr)
        return 1

    try:
        message_id = int(tg["result"]["message_id"])
    except (KeyError, TypeError, ValueError) as exc:
        log_bot_send(f"[generate] telegram_bad_response: {exc}")
        print("Telegram send response missing message_id", file=sys.stderr)
        return 1

    target["telegram_message_id"] = message_id
    write_json(posts_path, posts)

    log_bot_send(f"[generate] draft_resent post_id={post_id}")
    if should_auto_push():
        try:
            if commit_and_push(
                root,
                ["posts.json"],
                f"chore: resend draft {post_id}",
            ):
                print(f"Pushed resent draft {post_id}", flush=True)
            else:
                print("No git changes to commit", flush=True)
        except Exception as exc:  # noqa: BLE001
            log_bot_send(f"[generate] git_push_failed: {exc}")
            print(traceback.format_exc(), file=sys.stderr)
            return 1
    else:
        print("Wrote posts.json for resend — commit manually or set GIT_PUSH=1", flush=True)
    return 0


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
    post_dicts = [p for p in posts if isinstance(p, dict)]
    if action == "resend":
        return _resend_pending_post(root=root, posts_path=posts_path, posts=post_dicts)
    if action != "generate":
        log_bot_send(f"[generate] invalid_action: {action}")
        print(f"Invalid WF_ACTION: {action}", file=sys.stderr)
        return 1

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

    chosen: dict | None = None
    for t in topics:
        if isinstance(t, dict) and not t.get("used"):
            chosen = t
            break

    if chosen is None:
        log_bot_send("[generate] topic_backlog_exhausted")
        print("All topics used", flush=True)
        return 0

    topic_title = str(chosen.get("title", "")).strip()
    if not topic_title:
        log_bot_send("[generate] llm_output_invalid: empty topic title")
        return 0

    if not os.environ.get("GITHUB_TOKEN", "").strip():
        print("GITHUB_TOKEN is required for GitHub Models", file=sys.stderr)
        return 1

    llm = _run_llm(topic_title)
    if llm is None:
        return 0

    hook = llm["hook"]
    body = llm["body"]
    cta = llm["cta"]
    risk_flags = llm["risk_flags"]

    post_id = next_post_id(post_dicts)
    approval_token = new_approval_token()
    composed = compose_text(hook, body, cta)

    telegram_env = _require_telegram_env()
    if telegram_env is None:
        return 1
    post_token, chat_id = telegram_env

    text = _draft_message(topic_title, composed, risk_flags)
    try:
        markup = inline_approve_edit_reject(post_id, approval_token)
    except ValueError as exc:
        log_bot_send(f"[generate] callback_data_invalid: {exc}")
        print(str(exc), file=sys.stderr)
        return 1
    try:
        tg = send_message(
            post_token,
            chat_id,
            text,
            reply_markup=markup,
        )
    except Exception as exc:  # noqa: BLE001
        log_bot_send(f"[generate] telegram_send_failed: {exc}")
        print(traceback.format_exc(), file=sys.stderr)
        return 1

    try:
        message_id = int(tg["result"]["message_id"])
    except (KeyError, TypeError, ValueError) as exc:
        log_bot_send(f"[generate] telegram_bad_response: {exc}")
        return 1

    new_post = build_post(
        post_id=post_id,
        topic_title=topic_title,
        hook=hook,
        body=body,
        cta=cta,
        risk_flags=risk_flags,
        approval_token=approval_token,
        telegram_message_id=message_id,
    )

    chosen["used"] = True
    post_dicts.append(new_post)
    write_json(posts_path, post_dicts)
    write_json(topics_path, topics)

    log_bot_send(f"[generate] draft_generated post_id={post_id} topic={chosen.get('id', '?')}")

    if should_auto_push():
        try:
            if commit_and_push(
                root,
                ["posts.json", "topics.json"],
                f"chore: add draft {post_id}",
            ):
                print(f"Pushed draft {post_id}", flush=True)
            else:
                print("No git changes to commit", flush=True)
        except Exception as exc:  # noqa: BLE001
            log_bot_send(f"[generate] git_push_failed: {exc}")
            print(traceback.format_exc(), file=sys.stderr)
            return 1
    else:
        print("Wrote posts.json and topics.json — commit manually or set GIT_PUSH=1", flush=True)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
