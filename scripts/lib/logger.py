"""Centralized logger used by scheduler scripts."""

from __future__ import annotations

import sys
from dataclasses import dataclass

from .telegram import log_bot_send


@dataclass(frozen=True)
class AppLogger:
    """Thin logger wrapper for stdout/stderr + audit bot."""

    scope: str

    def info(self, message: str) -> None:
        print(message, flush=True)

    def error(self, message: str) -> None:
        print(message, file=sys.stderr)

    def audit(self, message: str) -> None:
        log_bot_send(f"[{self.scope}] {message}")


def get_logger(scope: str) -> AppLogger:
    """Build a scoped logger."""
    return AppLogger(scope=scope)

