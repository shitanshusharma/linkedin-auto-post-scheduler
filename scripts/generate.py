"""
Weekly generation entrypoint (GitHub Actions).

Env:
  GITHUB_TOKEN — GitHub Models (models:read); set automatically in Actions
  TELEGRAM_POST_BOT_TOKEN, TELEGRAM_CHAT_ID
  TELEGRAM_LOG_BOT_TOKEN, TELEGRAM_LOG_CHAT_ID — optional Log Bot
  GITHUB_MODEL — optional, default openai/gpt-4.1-mini
  WF_ACTION — generate | resend
  WF_POST_ID — required for resend
"""
from __future__ import annotations

import os
import sys
import traceback
from pathlib import Path

from pydantic import ValidationError

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
from core.constants import ACTIVE_POST_STATUSES, ERROR_MESSAGES, GIT_ROUTING
from core.llm import ensure_linkedin_skill_ready, generate_post_json, generate_post_json_with_feedback
from core.llm_output import LlmPostOutput, check_topic_alignment, validation_error_message
from core.models import RepoConfig, Topic
from core.post_record import PostRecord, build_post, compose_text, new_approval_token, next_post_id
from integrations.git_push import commit_and_push, should_auto_push
from integrations.telegram import inline_approve_edit_reject, send_message

LOGGER = get_logger("generate")


def _read_repo_config(root: Path) -> RepoConfig:
    try:
        raw = read_json(root / GIT_ROUTING.CONFIG_PATH)
    except Exception:  # noqa: BLE001
        return RepoConfig()
    if not isinstance(raw, dict):
        return RepoConfig()
    return RepoConfig.model_validate(raw)


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
    feedback: str | None = None
    for attempt in range(1, 4):
        strict = attempt > 1
        try:
            if feedback and strict:
                data = generate_post_json_with_feedback(
                    token=token,
                    topic_title=topic_title,
                    feedback=feedback,
                    strict_retry=True,
                )
            else:
                data = generate_post_json(token=token, topic_title=topic_title, strict_retry=strict)
            output = LlmPostOutput.model_validate(data)
            alignment_err = check_topic_alignment(output, topic_title)
            if alignment_err:
                notes.append(f"attempt{attempt} alignment: {alignment_err}")
                feedback = alignment_err
                continue
            return output
        except ValidationError as exc:
            err = validation_error_message(exc)
            notes.append(f"attempt{attempt} validation: {err}")
            feedback = err
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


def _resend_pending_post(*, root: Path, posts_path: Path, posts: list[PostRecord]) -> int:
    post_id = os.environ.get("WF_POST_ID", "").strip()
    if not post_id:
        LOGGER.audit("resend_invalid: missing WF_POST_ID")
        LOGGER.error(ERROR_MESSAGES.WF_POST_ID_REQUIRED)
        return 1

    target: PostRecord | None = None
    for p in posts:
        if p.id == post_id:
            target = p
            break

    if target is None:
        LOGGER.audit(f"resend_invalid: post not found ({post_id})")
        LOGGER.error(f"Post not found: {post_id}")
        return 1

    if target.status != ACTIVE_POST_STATUSES.PENDING:
        LOGGER.audit(f"resend_invalid: status={target.status} post_id={post_id}")
        LOGGER.error(f"resend requires pending post; got status={target.status}")
        return 1

    approval_token = (target.approval_token or "").strip()
    topic_title = target.topic.strip()
    composed = target.composed_text.strip()
    risk_flags = target.risk_flags
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

    target.telegram_message_id = message_id
    write_json(posts_path, [p.model_dump() for p in posts])

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

    raw_posts = read_json(posts_path)
    if not isinstance(raw_posts, list):
        LOGGER.error(ERROR_MESSAGES.POSTS_JSON_ARRAY_REQUIRED)
        return 1

    action = os.environ.get("WF_ACTION") or "generate"
    posts = [PostRecord.model_validate(p) for p in raw_posts if isinstance(p, dict)]
    if action == "resend":
        return _resend_pending_post(root=root, posts_path=posts_path, posts=posts)
    if action != "generate":
        LOGGER.audit(f"invalid_action: {action}")
        LOGGER.error(f"Invalid WF_ACTION: {action}")
        return 1

    if not config.generation_enabled:
        LOGGER.audit("generation_skipped: generation disabled in config")
        LOGGER.info("Skip: generation is disabled by config")
        return 0

    if config.default_github_model and not os.environ.get("GITHUB_MODEL", "").strip():
        os.environ["GITHUB_MODEL"] = config.default_github_model

    if config.single_active_post:
        for p in posts:
            if p.status in ACTIVE_POST_STATUSES.ALL:
                LOGGER.audit(f"generation_skipped: active post exists ({p.id})")
                LOGGER.info(f"Skip: active post {p.id}")
                return 0
    else:
        LOGGER.audit("single_active_post_disabled: proceeding despite active posts")
        LOGGER.info("single_active_post disabled in config; allowing a new draft")

    raw_topics = read_json(topics_path)
    if not isinstance(raw_topics, list) or not raw_topics:
        LOGGER.audit("topic_backlog_exhausted")
        LOGGER.info("No topics")
        return 0

    topics = [Topic.model_validate(t) for t in raw_topics if isinstance(t, dict)]
    if not topics:
        LOGGER.audit("topic_backlog_exhausted")
        LOGGER.info("No topics")
        return 0

    chosen: Topic | None = None
    for t in topics:
        if not t.used:
            chosen = t
            break

    if chosen is None:
        LOGGER.audit("topic_backlog_exhausted")
        LOGGER.info("All topics used")
        return 0

    topic_title = chosen.title.strip()
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

    hook = llm.hook
    body = llm.body
    cta = llm.cta
    risk_flags = llm.risk_flags

    post_id = next_post_id(posts)
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

    chosen.used = True
    posts.append(new_post)
    write_json(posts_path, [p.model_dump() for p in posts])
    write_json(topics_path, [t.model_dump() for t in topics])

    LOGGER.audit(f"draft_generated post_id={post_id} topic={chosen.id}")

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
