"""GitHub Models chat completions (see https://models.github.ai/inference/chat/completions)."""

from __future__ import annotations

import json
import logging
import os
import time
from typing import Any

import requests

from common.paths import repo_root
from core.constants import LLM_PROMPTS, LLM_RUNTIME, URLS
from core.llm_output import extract_json_object

_logger = logging.getLogger(__name__)

REQUIRED_SKILL_NAME = LLM_RUNTIME.REQUIRED_SKILL_NAME
REQUIRED_SKILL_PATH = repo_root() / LLM_RUNTIME.SKILLS_DIR / REQUIRED_SKILL_NAME / "SKILL.md"
SKILLS_LOCK_PATH = repo_root() / LLM_RUNTIME.SKILLS_LOCK_FILE


def _assert_base_system_prompt_contract() -> None:
    """Guard against accidental prompt drift in copy constraints."""
    for snippet in LLM_PROMPTS.REQUIRED_SNIPPETS:
        if snippet not in LLM_PROMPTS.BASE_SYSTEM_PROMPT:
            raise RuntimeError(f"BASE_SYSTEM_PROMPT missing required rule: {snippet}")

    for snippet in LLM_PROMPTS.FORBIDDEN_SNIPPETS:
        if snippet in LLM_PROMPTS.BASE_SYSTEM_PROMPT:
            raise RuntimeError(f"BASE_SYSTEM_PROMPT contains forbidden legacy rule: {snippet}")


_assert_base_system_prompt_contract()


def _load_required_skill_text() -> str:
    if not SKILLS_LOCK_PATH.exists():
        raise RuntimeError("skills-lock.json not found; mandatory linkedin-posts skill cannot be verified")
    try:
        lock = json.loads(SKILLS_LOCK_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"invalid skills-lock.json: {exc}") from exc

    skills = lock.get("skills")
    if not isinstance(skills, dict) or REQUIRED_SKILL_NAME not in skills:
        raise RuntimeError("mandatory linkedin-posts skill entry missing in skills-lock.json")

    if not REQUIRED_SKILL_PATH.exists():
        raise RuntimeError(f"mandatory skill file missing: {REQUIRED_SKILL_PATH}")

    try:
        skill_text = REQUIRED_SKILL_PATH.read_text(encoding="utf-8").strip()
    except OSError as exc:
        raise RuntimeError(f"failed reading mandatory skill file: {exc}") from exc

    if not skill_text:
        raise RuntimeError("mandatory linkedin-posts skill file is empty")

    return skill_text


def ensure_linkedin_skill_ready() -> None:
    """Fail fast if linkedin-posts skill is unavailable."""
    _load_required_skill_text()


def _system_prompt() -> str:
    skill_text = _load_required_skill_text()
    return (
        f"{LLM_PROMPTS.BASE_SYSTEM_PROMPT}\n\n"
        "MANDATORY SKILL (must be followed for every generated post):\n"
        "----- BEGIN linkedin-posts SKILL -----\n"
        f"{skill_text}\n"
        "----- END linkedin-posts SKILL -----\n\n"
        "Use the skill guidance as mandatory quality rules while still returning only the required JSON schema."
    )


def _headers(token: str) -> dict[str, str]:
    return {
        "Accept": "application/vnd.github+json",
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
        "X-GitHub-Api-Version": "2022-11-28",
    }


def _omit_temperature_for_github_model(model: str) -> bool:
    """OpenAI GPT-5 reasoning models reject explicit ``temperature``; the API returns 400."""
    m = model.strip().lower()
    if not m.startswith("openai/gpt-5"):
        return False
    # Catalog: ``openai/gpt-5-chat`` behaves as a chat model and accepts sampling params.
    if m.startswith("openai/gpt-5-chat"):
        return False
    return True


_INFERENCE_ERROR_BODY_MAX_CHARS = 2000


def _response_body_preview(r: requests.Response) -> str:
    """Return truncated response text for logs and exception messages."""
    try:
        text = (r.text or "").strip()
    except (OSError, UnicodeDecodeError):
        return ""
    if len(text) > _INFERENCE_ERROR_BODY_MAX_CHARS:
        return text[:_INFERENCE_ERROR_BODY_MAX_CHARS] + "…"
    return text


def _raise_inference_http_error(r: requests.Response) -> None:
    """Raise HTTPError including provider JSON body (``raise_for_status`` omits it)."""
    preview = _response_body_preview(r)
    base = f"{r.status_code} {r.reason} for {r.url}"
    message = f"{base} | {preview}" if preview else base
    _logger.error("GitHub Models request failed: %s", message)
    raise requests.HTTPError(message, response=r)


def chat_completion(
    *,
    token: str,
    user_content: str,
    model: str | None = None,
    temperature: float = LLM_RUNTIME.DEFAULT_TEMPERATURE,
) -> str:
    """Return assistant message content string (may be JSON).

    Retries automatically on 429 (rate-limit) with exponential backoff,
    respecting the ``Retry-After`` header when present.
    """
    m = model or os.environ.get("GITHUB_MODEL", LLM_RUNTIME.DEFAULT_GITHUB_MODEL)
    payload: dict[str, Any] = {
        "model": m,
        "messages": [
            {"role": "system", "content": _system_prompt()},
            {"role": "user", "content": user_content},
        ],
    }
    if not _omit_temperature_for_github_model(m):
        payload["temperature"] = temperature

    last_exc: requests.HTTPError | None = None
    for attempt in range(LLM_RUNTIME.RATE_LIMIT_MAX_RETRIES + 1):
        r = requests.post(
            URLS.GITHUB_MODELS_CHAT_COMPLETIONS,
            headers=_headers(token),
            json=payload,
            timeout=120,
        )
        if r.status_code != 429:
            if r.ok:
                break
            _raise_inference_http_error(r)

        last_exc = requests.HTTPError(response=r)
        if attempt >= LLM_RUNTIME.RATE_LIMIT_MAX_RETRIES:
            break

        retry_after = r.headers.get("Retry-After")
        if retry_after:
            try:
                wait = max(float(retry_after), 1.0)
            except (ValueError, TypeError):
                wait = LLM_RUNTIME.RATE_LIMIT_BACKOFF_SECONDS[attempt]
        else:
            wait = LLM_RUNTIME.RATE_LIMIT_BACKOFF_SECONDS[attempt]

        _logger.warning("Rate-limited (429); retrying in %.0fs (attempt %d/%d)", wait, attempt + 1, LLM_RUNTIME.RATE_LIMIT_MAX_RETRIES)
        time.sleep(wait)
    else:
        if last_exc is not None:
            raise last_exc
        r.raise_for_status()

    data = r.json()
    choices = data.get("choices") or []
    if not choices:
        raise ValueError("no choices in model response")
    msg = choices[0].get("message") or {}
    content = msg.get("content")
    if not isinstance(content, str) or not content.strip():
        raise ValueError("empty assistant content")
    return content


def generate_post_json(*, token: str, topic_title: str, strict_retry: bool = False) -> dict[str, Any]:
    """Call model and return parsed JSON dict; raises on HTTP/parse errors."""
    user = LLM_PROMPTS.USER_PROMPT_TEMPLATE.format(topic_title=topic_title)
    if strict_retry:
        user += LLM_RUNTIME.STRICT_RETRY_SUFFIX
    return _generate_post_json_with_user_prompt(token=token, user_content=user, strict_retry=strict_retry)


def _generate_post_json_with_user_prompt(*, token: str, user_content: str, strict_retry: bool) -> dict[str, Any]:
    raw = chat_completion(
        token=token,
        user_content=user_content,
        temperature=LLM_RUNTIME.STRICT_RETRY_TEMPERATURE if strict_retry else LLM_RUNTIME.DEFAULT_TEMPERATURE,
    )
    return extract_json_object(raw)


def generate_post_json_with_feedback(
    *, token: str, topic_title: str, feedback: str | None, strict_retry: bool = True
) -> dict[str, Any]:
    """Call model with optional validator feedback to improve compliance."""
    user = LLM_PROMPTS.USER_PROMPT_TEMPLATE.format(topic_title=topic_title)
    if strict_retry:
        user += LLM_RUNTIME.STRICT_RETRY_SUFFIX
    if feedback:
        user += f"{LLM_RUNTIME.VALIDATION_FEEDBACK_PREFIX}- {feedback}\n"
    return _generate_post_json_with_user_prompt(token=token, user_content=user, strict_retry=strict_retry)

