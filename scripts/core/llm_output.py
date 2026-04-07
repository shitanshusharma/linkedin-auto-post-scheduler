"""Validate and parse LLM JSON output per docs/ARCHITECTURE.md (LLM output contract)."""

from __future__ import annotations

import json
import re
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

from core.constants import LLM_OUTPUT


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
        expected_from_percent = [1.0 + (pct / 100.0) for pct in percent_increases]
        close_match = any(
            abs(multiplier - expected) <= 0.15
            for multiplier in multipliers
            for expected in expected_from_percent
        )
        if not close_match:
            warnings.append("math_check: percentage change and x-multiplier appear inconsistent")

    return _dedupe_flags(warnings)


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
    return bool(re.search(r"(?m)^\s*[A-Za-z_][A-Za-z0-9_]*\s*=", body))


def _count_distinct_phrases(text: str, phrases: tuple[str, ...]) -> int:
    lowered = text.lower()
    return sum(1 for phrase in phrases if phrase in lowered)


def _contains_any_phrase(text: str, phrases: tuple[str, ...]) -> bool:
    lowered = text.lower()
    return any(phrase in lowered for phrase in phrases)


_TECHNICAL_CORE_CUES = (
    "algorithm", "signal", "metric", "trigger", "decision", "condition",
    "threshold", "latency", "throughput", "capacity", "utilization", "rate",
    "availability", "allocation", "requests", "drivers", "feedback loop",
    "control loop", "pipeline", "model", "cache", "queue", "optimization",
    "constraint", "input", "output", "demand", "supply",
)

_TECHNICAL_ADVANCED_CUES = (
    "threshold", "latency", "throughput", "feedback loop", "control loop",
    "optimization", "constraint", "utilization", "capacity", "allocation",
    "pipeline", "queue", "idempotency", "consistency", "replication", "model",
)

_TECHNICAL_TRIGGER_CUES = (
    "when", "if", "exceeds", "drops below", "crosses", "trigger",
    "triggers", "threshold",
)


def _has_moderate_technical_depth(body: str) -> bool:
    core_count = _count_distinct_phrases(body, _TECHNICAL_CORE_CUES)
    advanced_count = _count_distinct_phrases(body, _TECHNICAL_ADVANCED_CUES)
    has_trigger_logic = _contains_any_phrase(body, _TECHNICAL_TRIGGER_CUES)
    return core_count >= 3 and advanced_count >= 1 and has_trigger_logic


_BANNED_PHRASES = (
    "game-changer",
    "powerful tool",
    "embrace the power of",
)

_GENERIC_CTA_PATTERNS = ("can improve your approach",)


class LlmPostOutput(BaseModel):
    """Validated LLM output per docs/ARCHITECTURE.md §4.1."""

    model_config = ConfigDict(extra="forbid")

    hook: str = Field(max_length=LLM_OUTPUT.MAX_HOOK_CHARS)
    body: str = Field(max_length=LLM_OUTPUT.MAX_BODY_CHARS)
    cta: str = Field(max_length=LLM_OUTPUT.MAX_CTA_CHARS)
    risk_flags: list[str] = Field(default_factory=list)

    @field_validator("hook", "body", "cta")
    @classmethod
    def _must_be_non_empty(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("must be non-empty")
        return v

    @field_validator("hook", "body", "cta")
    @classmethod
    def _no_html_tags(cls, v: str) -> str:
        if _has_html_tags(v):
            raise ValueError("raw HTML tags not allowed")
        return v

    @field_validator("hook", "body")
    @classmethod
    def _no_banned_phrases(cls, v: str) -> str:
        if any(phrase in v.lower() for phrase in _BANNED_PHRASES):
            raise ValueError("copy must avoid cliche/template phrases; use specific wording")
        return v

    @field_validator("risk_flags")
    @classmethod
    def _dedupe_risk_flags(cls, v: list[str]) -> list[str]:
        return _dedupe_flags(v)

    @model_validator(mode="after")
    def _validate_content_rules(self) -> LlmPostOutput:
        if len(self.hook) + len(self.body) + len(self.cta) > LLM_OUTPUT.MAX_TOTAL_CHARS:
            raise ValueError("combined length > 2000")

        lowered_cta = self.cta.strip().lower()
        if lowered_cta.startswith("understanding "):
            raise ValueError("cta must be specific and practical, not a generic template")
        if any(p in lowered_cta for p in _GENERIC_CTA_PATTERNS):
            raise ValueError("cta must be specific and practical, not a generic template")
        if "?" in self.cta:
            raise ValueError("cta must be a statement and must not be phrased as a question")

        paragraphs = _paragraphs(self.body)
        if len(self.body.strip()) >= LLM_OUTPUT.LONG_BODY_REQUIRES_BREAK_CHARS and len(paragraphs) < 2:
            raise ValueError(
                "body must use short paragraphs/line breaks (single large block is not allowed)"
            )
        if any(len(p) > LLM_OUTPUT.MAX_LONG_PARAGRAPH_CHARS for p in paragraphs):
            raise ValueError("body paragraphs are too long; split into shorter chunks")

        if not _has_moderate_technical_depth(self.body):
            raise ValueError(
                "body must include concrete technical detail "
                "(multiple system cues plus trigger/decision logic)"
            )

        if _has_equation_style_line(self.body):
            raise ValueError("body must avoid hypothetical equation-style notation")

        example_count = _example_line_count(self.body)
        if example_count != 1:
            raise ValueError(
                f'body must contain exactly one line prefixed with "{LLM_OUTPUT.EXAMPLE_PREFIX}"'
            )

        ascii_lines = _ascii_diagram_lines(self.body)
        if len(ascii_lines) > LLM_OUTPUT.MAX_ASCII_LINES:
            raise ValueError(
                f"ASCII illustration must be at most {LLM_OUTPUT.MAX_ASCII_LINES} lines"
            )
        if any(len(line) > LLM_OUTPUT.MAX_ASCII_LINE_CHARS for line in ascii_lines):
            raise ValueError(
                f"ASCII illustration lines must be <= {LLM_OUTPUT.MAX_ASCII_LINE_CHARS} chars"
            )

        math_flags = _numeric_consistency_warning_flags_from_example(self.body)
        if math_flags:
            self.risk_flags = _dedupe_flags([*self.risk_flags, *math_flags])

        return self


def validation_error_message(exc: ValidationError) -> str:
    """Extract a human-readable error string from a Pydantic ValidationError."""
    return "; ".join(e["msg"].removeprefix("Value error, ") for e in exc.errors())


_ALIGNMENT_STOP_WORDS = frozenset({
    "a", "an", "the", "and", "or", "but", "in", "on", "at", "to", "for",
    "of", "with", "by", "from", "into", "as", "via", "per", "its",
    "it", "is", "are", "was", "were", "be", "been", "being",
    "have", "has", "had", "do", "does", "did",
    "will", "would", "could", "should", "may", "might", "can",
    "how", "what", "when", "where", "why", "who", "which", "that", "this",
    "not", "no", "nor",
    "using", "uses", "used",
    "based", "without",
})


def _extract_topic_keywords(topic_title: str) -> list[str]:
    """Extract meaningful lowercase keywords from a topic title."""
    tokens = re.findall(r"[a-zA-Z0-9]+", topic_title.lower())
    return [t for t in tokens if len(t) > 2 and t not in _ALIGNMENT_STOP_WORDS]


def check_topic_alignment(output: LlmPostOutput, topic_title: str) -> str | None:
    """Return an error message if content drifted from the topic, or None if aligned."""
    keywords = _extract_topic_keywords(topic_title)
    if len(keywords) < LLM_OUTPUT.TOPIC_ALIGNMENT_MIN_KEYWORDS:
        return None

    full_text = f"{output.hook} {output.body} {output.cta}".lower()
    matched = [kw for kw in keywords if kw in full_text]
    ratio = len(matched) / len(keywords)

    if ratio >= LLM_OUTPUT.TOPIC_ALIGNMENT_MIN_OVERLAP:
        return None

    missing = sorted(set(keywords) - set(matched))
    return (
        f"content does not align with topic — only {len(matched)}/{len(keywords)} "
        f"topic keywords found in post; missing: {', '.join(missing[:5])}"
    )
