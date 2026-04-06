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


def _dedupe_flags(flags: list[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for raw in flags:
        normalized = raw.strip()
        if not normalized:
            continue
        lowered = normalized.lower()
        if lowered in {"none", "n/a", "na"}:
            continue
        if lowered in seen:
            continue
        seen.add(lowered)
        out.append(normalized)
    return out


def _extract_example_line(body: str) -> str:
    prefix = LLM_OUTPUT.EXAMPLE_PREFIX.lower()
    for line in body.splitlines():
        stripped = line.strip()
        if stripped.lower().startswith(prefix):
            return stripped
    return ""


def _numeric_consistency_warning_flags_from_example(body: str) -> list[str]:
    """Non-blocking, domain-agnostic checks for obvious numeric inconsistencies."""
    line = _extract_example_line(body)
    if not line:
        return []

    lowered = line.lower()
    warnings: list[str] = []

    multipliers = [float(raw) for raw in re.findall(r"(\d+(?:\.\d+)?)\s*x\b", lowered)]
    percent_increases = [
        float(raw)
        for raw in re.findall(r"(\d+(?:\.\d+)?)\s*%\s*(?:increase|higher|up|more)", lowered)
    ]

    if any(value <= 0 for value in multipliers):
        warnings.append("math_check: multiplier must be greater than 0x")
    if any(value <= -100 for value in percent_increases):
        warnings.append("math_check: percentage change below -100% is not feasible")

    word_expectations: list[tuple[tuple[str, ...], float, str]] = [
        (("double", "doubled", "twice"), 2.0, "math_check: wording implies ~2.0x but multiplier differs"),
        (
            ("triple", "tripled", "three times"),
            3.0,
            "math_check: wording implies ~3.0x but multiplier differs",
        ),
        (("half", "halved"), 0.5, "math_check: wording implies ~0.5x but multiplier differs"),
    ]

    for tokens, expected, warning in word_expectations:
        if any(token in lowered for token in tokens) and multipliers:
            if all(abs(multiplier - expected) > 0.2 for multiplier in multipliers):
                warnings.append(warning)

    if multipliers and percent_increases:
        # "50% increase" should be close to 1.5x, etc.
        expected_from_percent = [1.0 + (pct / 100.0) for pct in percent_increases]
        close_match = any(
            abs(multiplier - expected) <= 0.15
            for multiplier in multipliers
            for expected in expected_from_percent
        )
        if not close_match:
            warnings.append("math_check: percentage change and x-multiplier appear inconsistent")

    return _dedupe_flags(warnings)


def to_llm_post_output(data: dict[str, Any]) -> LlmPostOutput:
    """Narrow a validated dict to LlmPostOutput. Call only after validate_llm_output passes."""
    base_flags = [str(x) for x in data["risk_flags"]]
    math_flags = _numeric_consistency_warning_flags_from_example(str(data["body"]))
    return {
        "hook": str(data["hook"]),
        "body": str(data["body"]),
        "cta": str(data["cta"]),
        "risk_flags": _dedupe_flags(base_flags + math_flags),
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


def _has_equation_style_line(body: str) -> bool:
    # Catch formula-like lines such as "x = a*b + c" while allowing normal prose.
    return bool(re.search(r"(?m)^\s*[A-Za-z_][A-Za-z0-9_]*\s*=", body))


def _contains_any_phrase(text: str, phrases: tuple[str, ...]) -> bool:
    lowered = text.lower()
    return any(phrase in lowered for phrase in phrases)


def _count_distinct_phrases(text: str, phrases: tuple[str, ...]) -> int:
    lowered = text.lower()
    return sum(1 for phrase in phrases if phrase in lowered)


def _has_moderate_technical_depth(body: str) -> bool:
    # Require multiple concrete technical signals, not just generic prose.
    core_cues = (
        "algorithm",
        "signal",
        "metric",
        "trigger",
        "decision",
        "condition",
        "threshold",
        "latency",
        "throughput",
        "capacity",
        "utilization",
        "rate",
        "availability",
        "allocation",
        "requests",
        "drivers",
        "feedback loop",
        "control loop",
        "pipeline",
        "model",
        "cache",
        "queue",
        "optimization",
        "constraint",
        "input",
        "output",
        "demand",
        "supply",
    )
    advanced_cues = (
        "threshold",
        "latency",
        "throughput",
        "feedback loop",
        "control loop",
        "optimization",
        "constraint",
        "utilization",
        "capacity",
        "allocation",
        "pipeline",
        "queue",
        "idempotency",
        "consistency",
        "replication",
        "model",
    )
    trigger_cues = (
        "when",
        "if",
        "exceeds",
        "drops below",
        "crosses",
        "trigger",
        "triggers",
        "threshold",
    )

    core_count = _count_distinct_phrases(body, core_cues)
    advanced_count = _count_distinct_phrases(body, advanced_cues)
    has_trigger_logic = _contains_any_phrase(body, trigger_cues)
    return core_count >= 3 and advanced_count >= 1 and has_trigger_logic


def _contains_banned_template_phrase(text: str) -> bool:
    banned = (
        "game-changer",
        "powerful tool",
        "embrace the power of",
    )
    lowered = text.lower()
    return any(phrase in lowered for phrase in banned)


def _is_generic_cta(cta: str) -> bool:
    lowered = cta.strip().lower()
    if lowered.startswith("understanding "):
        return True
    generic_patterns = ("can improve your approach",)
    return any(pattern in lowered for pattern in generic_patterns)


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
    if _contains_banned_template_phrase(hook) or _contains_banned_template_phrase(body):
        return False, "copy must avoid cliche/template phrases; use specific wording"
    if _is_generic_cta(cta):
        return False, "cta must be specific and practical, not a generic template"

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
    if not _has_moderate_technical_depth(body):
        return (
            False,
            "body must include concrete technical detail (multiple system cues plus trigger/decision logic)",
        )
    if _has_equation_style_line(body):
        return False, "body must avoid hypothetical equation-style notation"

    example_count = _example_line_count(body)
    if example_count != 1:
        return False, f'body must contain exactly one line prefixed with "{LLM_OUTPUT.EXAMPLE_PREFIX}"'

    ascii_lines = _ascii_diagram_lines(body)
    if len(ascii_lines) > LLM_OUTPUT.MAX_ASCII_LINES:
        return False, f"ASCII illustration must be at most {LLM_OUTPUT.MAX_ASCII_LINES} lines"
    if any(len(line) > LLM_OUTPUT.MAX_ASCII_LINE_CHARS for line in ascii_lines):
        return False, f"ASCII illustration lines must be <= {LLM_OUTPUT.MAX_ASCII_LINE_CHARS} chars"
    if "?" in cta:
        return False, "cta must be a statement and must not be phrased as a question"

    return True, ""

