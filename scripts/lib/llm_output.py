"""Validate and parse LLM JSON output per low-level-design.md §3.3.1."""

from __future__ import annotations

import json
import re
from typing import Any, TypedDict

REQUIRED_KEYS = frozenset({"hook", "body", "cta", "risk_flags"})


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


def validate_llm_output(data: dict[str, Any]) -> tuple[bool, str]:
    """Return (ok, error_message)."""
    if set(data.keys()) != REQUIRED_KEYS:
        return False, f"keys must be exactly {sorted(REQUIRED_KEYS)}, got {sorted(data.keys())}"

    hook = data["hook"]
    body = data["body"]
    cta = data["cta"]
    risk_flags = data["risk_flags"]

    if not isinstance(hook, str) or not isinstance(body, str) or not isinstance(cta, str):
        return False, "hook, body, cta must be strings"
    if not isinstance(risk_flags, list) or not all(isinstance(x, str) for x in risk_flags):
        return False, "risk_flags must be an array of strings"

    if len(hook) > 150 or len(body) > 1650 or len(cta) > 200:
        return False, "length limits exceeded"
    if len(hook) + len(body) + len(cta) > 2000:
        return False, "combined length > 2000"
    for part in (hook, body, cta):
        if _has_html_tags(part):
            return False, "raw HTML tags not allowed"

    return True, ""
