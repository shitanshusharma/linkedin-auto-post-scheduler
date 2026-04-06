"""Pydantic models for config.json and topics.json."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, field_validator

from core.constants import (
    GIT_AUTOMATION,
    GIT_ROUTING,
    HOUSEKEEPING_WINDOWS_HOURS,
    TOKEN_REMINDER_DAYS,
)


class RepoConfig(BaseModel):
    """Typed representation of config.json with defaults from constants."""

    model_config = ConfigDict(extra="allow")

    linkedin_token_refreshed_at: str | None = None
    pat_created_at: str | None = None
    git_write_target: str = GIT_ROUTING.DEFAULT_MODE
    git_base_branch: str = GIT_AUTOMATION.DEFAULT_BASE_BRANCH
    git_automation_branch: str = GIT_AUTOMATION.DEFAULT_AUTOMATION_BRANCH
    generation_enabled: bool = True
    housekeeping_enabled: bool = True
    single_active_post: bool = True
    default_github_model: str | None = None
    housekeeping_expiry_hours: float = HOUSEKEEPING_WINDOWS_HOURS.EXPIRY
    housekeeping_reminder_1_hours: float = HOUSEKEEPING_WINDOWS_HOURS.REMINDER_1_START
    housekeeping_reminder_2_hours: float = HOUSEKEEPING_WINDOWS_HOURS.REMINDER_2_START
    linkedin_warning_days: int = TOKEN_REMINDER_DAYS.LINKEDIN_WARNING
    linkedin_urgent_days: int = TOKEN_REMINDER_DAYS.LINKEDIN_URGENT
    pat_warning_days: int = TOKEN_REMINDER_DAYS.PAT_WARNING

    @field_validator("default_github_model", mode="before")
    @classmethod
    def _empty_string_to_none(cls, v: object) -> object:
        if isinstance(v, str) and not v.strip():
            return None
        return v


class Topic(BaseModel):
    """A single topic entry from topics.json."""

    model_config = ConfigDict(extra="allow")

    id: str
    title: str
    used: bool = False
