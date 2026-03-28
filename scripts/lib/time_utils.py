"""Reusable datetime utilities for script workflows."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from .constants import HALF_DAY_WINDOW


def parse_iso8601(value: Any) -> datetime | None:
    """Parse an ISO-8601 timestamp into UTC datetime."""
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip()
    if text.endswith("Z"):
        text = f"{text[:-1]}+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def hours_since(now: datetime, instant: datetime) -> float:
    """Return elapsed hours between two UTC datetimes."""
    return (now - instant).total_seconds() / 3600.0


def days_since(now: datetime, instant: datetime) -> float:
    """Return elapsed days between two UTC datetimes."""
    return (now - instant).total_seconds() / 86400.0


def within_half_day_window(days_elapsed: float, threshold_days: int) -> bool:
    """Return True if elapsed days are within threshold .. threshold+12h."""
    return threshold_days <= days_elapsed < (threshold_days + HALF_DAY_WINDOW)

