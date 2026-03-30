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
from core.constants import ACTIVE_POST_STATUSES, ERROR_MESSAGES, FEATURE_FLAGS, GIT_ROUTING
from core.llm import ensure_linkedin_skill_ready, generate_post_json
from core.llm_output import LlmPostOutput, to_llm_post_output, validate_llm_output
from core.post_record import build_post, compose_text, new_approval_token, next_post_id
from integrations.git_push import commit_and_push, should_auto_push
from integrations.telegram import inline_approve_edit_reject, send_message

LOGGER = get_logger("generate")


def _read_repo_config(root: Path) -> dict:
    try:
        raw = read_json(root / GIT_ROUTING.CONFIG_PATH)
    except Exception:  # noqa: BLE001
        return {}
    return raw if isinstance(raw, dict) else {}


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


def _config_string(config: dict, key: str) -> str | None:
    value = config.get(key)
    if not isinstance(value, str):
        return None
    cleaned = value.strip()
    return cleaned or None


def _draft_message(topic: str, composed: str, risk_flags: list[str]) -> str:
    flags = ", ".join(risk_flags) if risk_flags else "None"
    return (
        f"📝 New LinkedIn Draft\n\n"
        f"Topic: {topic}\n\n"
        f"---\n{composed}\n---\n\n"
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
    LOGGER.audit(f"llm_output_invalid: {detail}")
    LOGGER.error(f"LLM validation failed: {detail}")
    return None


def _require_telegram_env() -> tuple[str, str] | None:
    post_token = os.environ.get("TELEGRAM_POST_BOT_TOKEN", "").strip()
    chat_id = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
    if not post_token or not chat_id:
        LOGGER.error(ERROR_MESSAGES.TELEGRAM_ENV_REQUIRED)
        return None
    return post_token, chat_id


def _resend_pending_post(*, root: Path, posts_path: Path, posts: list[dict]) -> int:
    post_id = os.environ.get("WF_POST_ID", "").strip()
    if not post_id:
        LOGGER.audit("resend_invalid: missing WF_POST_ID")
        LOGGER.error(ERROR_MESSAGES.WF_POST_ID_REQUIRED)
        return 1

    target: dict | None = None
    for p in posts:
        if p.get("id") == post_id:
            target = p
            break

    if target is None:
        LOGGER.audit(f"resend_invalid: post not found ({post_id})")
        LOGGER.error(f"Post not found: {post_id}")
        return 1

    status = str(target.get("status", ""))
    if status != ACTIVE_POST_STATUSES.PENDING:
        LOGGER.audit(f"resend_invalid: status={status} post_id={post_id}")
        LOGGER.error(f"resend requires pending post; got status={status}")
        return 1

    approval_token = str(target.get("approval_token", "")).strip()
    topic_title = str(target.get("topic", "")).strip()
    composed = str(target.get("composed_text", "")).strip()
    raw_flags = target.get("risk_flags")
    risk_flags = [x for x in raw_flags if isinstance(x, str)] if isinstance(raw_flags, list) else []
    if not approval_token:
        LOGGER.audit(f"resend_invalid: missing approval_token post_id={post_id}")
        LOGGER.error(f"Post missing approval_token: {post_id}")
        return 1
    if not composed:
        LOGGER.audit(f"resend_invalid: missing composed_text post_id={post_id}")
        LOGGER.error(f"Post missing composed_text: {post_id}")
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
        LOGGER.audit(f"callback_data_invalid: {exc}")
        LOGGER.error(str(exc))
        return 1

    try:
        tg = send_message(
            post_token,
            chat_id,
            _draft_message(topic_title, composed, risk_flags),
            reply_markup=markup,
        )
    except Exception as exc:  # noqa: BLE001
        LOGGER.audit(f"telegram_send_failed: {exc}")
        LOGGER.error(traceback.format_exc())
        return 1

    try:
        message_id = int(tg["result"]["message_id"])
    except (KeyError, TypeError, ValueError) as exc:
        LOGGER.audit(f"telegram_bad_response: {exc}")
        LOGGER.error(ERROR_MESSAGES.TELEGRAM_RESPONSE_MISSING_MESSAGE_ID)
        return 1

    target["telegram_message_id"] = message_id
    write_json(posts_path, posts)

    LOGGER.audit(f"draft_resent post_id={post_id}")
    if should_auto_push():
        try:
            if commit_and_push(
                root,
                ["posts.json"],
                f"chore: resend draft {post_id}",
            ):
                LOGGER.info(f"Pushed resent draft {post_id}")
            else:
                LOGGER.info("No git changes to commit")
        except Exception as exc:  # noqa: BLE001
            LOGGER.audit(f"git_push_failed: {exc}")
            LOGGER.error(traceback.format_exc())
            return 1
    else:
        LOGGER.info("Wrote posts.json for resend — commit manually or set GIT_PUSH=1")
    return 0


def main() -> int:
    if load_dotenv is not None:
        load_dotenv(repo_root() / ".env")

    root = repo_root()
    config = _read_repo_config(root)
    posts_path = root / "posts.json"
    topics_path = root / "topics.json"

    posts = read_json(posts_path)
    if not isinstance(posts, list):
        LOGGER.error(ERROR_MESSAGES.POSTS_JSON_ARRAY_REQUIRED)
        return 1

    action = os.environ.get("WF_ACTION") or "generate"
    post_dicts = [p for p in posts if isinstance(p, dict)]
    if action == "resend":
        return _resend_pending_post(root=root, posts_path=posts_path, posts=post_dicts)
    if action != "generate":
        LOGGER.audit(f"invalid_action: {action}")
        LOGGER.error(f"Invalid WF_ACTION: {action}")
        return 1

    if not _config_bool(config, FEATURE_FLAGS.GENERATION_ENABLED_KEY, FEATURE_FLAGS.DEFAULT_TRUE):
        LOGGER.audit("generation_skipped: generation disabled in config")
        LOGGER.info("Skip: generation is disabled by config")
        return 0

    configured_model = _config_string(config, FEATURE_FLAGS.DEFAULT_GITHUB_MODEL_KEY)
    if configured_model and not os.environ.get("GITHUB_MODEL", "").strip():
        os.environ["GITHUB_MODEL"] = configured_model

    if _config_bool(config, FEATURE_FLAGS.SINGLE_ACTIVE_POST_KEY, FEATURE_FLAGS.DEFAULT_TRUE):
        for p in posts:
            if isinstance(p, dict) and p.get("status") in ACTIVE_POST_STATUSES.ALL:
                pid = p.get("id", "?")
                LOGGER.audit(f"generation_skipped: active post exists ({pid})")
                LOGGER.info(f"Skip: active post {pid}")
                return 0
    else:
        LOGGER.audit("single_active_post_disabled: proceeding despite active posts")
        LOGGER.info("single_active_post disabled in config; allowing a new draft")

    topics = read_json(topics_path)
    if not isinstance(topics, list) or not topics:
        LOGGER.audit("topic_backlog_exhausted")
        LOGGER.info("No topics")
        return 0

    chosen: dict | None = None
    for t in topics:
        if isinstance(t, dict) and not t.get("used"):
            chosen = t
            break

    if chosen is None:
        LOGGER.audit("topic_backlog_exhausted")
        LOGGER.info("All topics used")
        return 0

    topic_title = str(chosen.get("title", "")).strip()
    if not topic_title:
        LOGGER.audit("llm_output_invalid: empty topic title")
        return 0

    try:
        ensure_linkedin_skill_ready()
    except RuntimeError as exc:
        LOGGER.audit(f"skill_gate_failed: {exc}")
        LOGGER.error(f"Mandatory linkedin-posts skill check failed: {exc}")
        return 1

    if not os.environ.get("GITHUB_TOKEN", "").strip():
        LOGGER.error(ERROR_MESSAGES.GITHUB_TOKEN_REQUIRED)
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
        LOGGER.audit(f"callback_data_invalid: {exc}")
        LOGGER.error(str(exc))
        return 1
    try:
        tg = send_message(
            post_token,
            chat_id,
            text,
            reply_markup=markup,
        )
    except Exception as exc:  # noqa: BLE001
        LOGGER.audit(f"telegram_send_failed: {exc}")
        LOGGER.error(traceback.format_exc())
        return 1

    try:
        message_id = int(tg["result"]["message_id"])
    except (KeyError, TypeError, ValueError) as exc:
        LOGGER.audit(f"telegram_bad_response: {exc}")
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

    LOGGER.audit(f"draft_generated post_id={post_id} topic={chosen.get('id', '?')}")

    if should_auto_push():
        try:
            if commit_and_push(
                root,
                ["posts.json", "topics.json"],
                f"chore: add draft {post_id}",
            ):
                LOGGER.info(f"Pushed draft {post_id}")
            else:
                LOGGER.info("No git changes to commit")
        except Exception as exc:  # noqa: BLE001
            LOGGER.audit(f"git_push_failed: {exc}")
            LOGGER.error(traceback.format_exc())
            return 1
    else:
        LOGGER.info("Wrote posts.json and topics.json — commit manually or set GIT_PUSH=1")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
