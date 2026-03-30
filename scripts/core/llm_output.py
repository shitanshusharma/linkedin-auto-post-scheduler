"""Validate and parse LLM JSON output per docs/ARCHITECTURE.md (LLM output contract)."""

from __future__ import annotations

import json
import re
from typing import Any, TypedDict

from core.constants import LLM_OUTPUT


class LlmPostOutput(TypedDict):
    """Structured LLM output after validation (§3.3.1)."""

    hook: str
    body: str
    cta: str
    risk_flags: list[str]


def to_llm_post_output(data: dict[str, Any]) -> LlmPostOutput:
    """Narrow a validated dict to LlmPostOutput. Call only after validate_llm_output passes."""
    return {
        "hook": str(data["hook"]),
        "body": str(data["body"]),
        "cta": str(data["cta"]),
        "risk_flags": [str(x) for x in data["risk_flags"]],
    }


def extract_json_object(raw: str) -> dict[str, Any]:
    """Strip optional markdown fences and parse JSON object."""
    text = raw.strip()
    if text.startswith("```"):
        lines = text.split("\n")
        if lines[0].startswith("```"):
            lines = lines[1:]
        while lines and lines[-1].strip() == "":
            lines.pop()
        if lines and lines[-1].strip().startswith("```"):
            lines = lines[:-1]
        text = "\n".join(lines)
    data = json.loads(text)
    if not isinstance(data, dict):
        raise ValueError("root must be a JSON object")
    return data


def _has_html_tags(s: str) -> bool:
    return bool(re.search(r"<[a-zA-Z/][^>]*>", s))


def _paragraphs(text: str) -> list[str]:
    return [part.strip() for part in re.split(r"\n\s*\n", text.strip()) if part.strip()]


def _example_line_count(body: str) -> int:
    prefix = LLM_OUTPUT.EXAMPLE_PREFIX.lower()
    return sum(1 for line in body.splitlines() if line.strip().lower().startswith(prefix))


def _ascii_diagram_lines(body: str) -> list[str]:
    lines: list[str] = []
    for raw_line in body.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        if "->" in line or "<-" in line:
            lines.append(line)
            continue
        bracket_or_pipe_count = sum(ch in line for ch in "[]|")
        if bracket_or_pipe_count >= 2:
            lines.append(line)
    return lines


def validate_llm_output(data: dict[str, Any]) -> tuple[bool, str]:
    """Return (ok, error_message)."""
    if set(data.keys()) != LLM_OUTPUT.REQUIRED_KEYS:
        return False, f"keys must be exactly {sorted(LLM_OUTPUT.REQUIRED_KEYS)}, got {sorted(data.keys())}"

    hook = data["hook"]
    body = data["body"]
    cta = data["cta"]
    risk_flags = data["risk_flags"]

    if not isinstance(hook, str) or not isinstance(body, str) or not isinstance(cta, str):
        return False, "hook, body, cta must be strings"
    if not isinstance(risk_flags, list) or not all(isinstance(x, str) for x in risk_flags):
        return False, "risk_flags must be an array of strings"
    if not hook.strip() or not body.strip() or not cta.strip():
        return False, "hook, body, cta must be non-empty strings"

    if (
        len(hook) > LLM_OUTPUT.MAX_HOOK_CHARS
        or len(body) > LLM_OUTPUT.MAX_BODY_CHARS
        or len(cta) > LLM_OUTPUT.MAX_CTA_CHARS
    ):
        return False, "length limits exceeded"
    if len(hook) + len(body) + len(cta) > LLM_OUTPUT.MAX_TOTAL_CHARS:
        return False, "combined length > 2000"
    for part in (hook, body, cta):
        if _has_html_tags(part):
            return False, "raw HTML tags not allowed"

    paragraphs = _paragraphs(body)
    if len(body.strip()) >= LLM_OUTPUT.LONG_BODY_REQUIRES_BREAK_CHARS and len(paragraphs) < 2:
        return False, "body must use short paragraphs/line breaks (single large block is not allowed)"
    if any(len(paragraph) > LLM_OUTPUT.MAX_LONG_PARAGRAPH_CHARS for paragraph in paragraphs):
        return False, "body paragraphs are too long; split into shorter chunks"

    example_count = _example_line_count(body)
    if example_count != 1:
        return False, f'body must contain exactly one line prefixed with "{LLM_OUTPUT.EXAMPLE_PREFIX}"'

    ascii_lines = _ascii_diagram_lines(body)
    if len(ascii_lines) > LLM_OUTPUT.MAX_ASCII_LINES:
        return False, f"ASCII illustration must be at most {LLM_OUTPUT.MAX_ASCII_LINES} lines"
    if any(len(line) > LLM_OUTPUT.MAX_ASCII_LINE_CHARS for line in ascii_lines):
        return False, f"ASCII illustration lines must be <= {LLM_OUTPUT.MAX_ASCII_LINE_CHARS} chars"

    return True, ""

