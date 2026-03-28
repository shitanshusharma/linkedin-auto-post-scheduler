"""Centralized constants for scheduler scripts (plain values, no imports)."""


class ACTIVE_POST_STATUSES:
    PENDING = "pending"
    EDITING = "editing"
    CONFIRMING_EDIT = "confirming_edit"
    ALL = (PENDING, EDITING, CONFIRMING_EDIT)


class POST_STATUSES:
    EXPIRED = "expired"


class HOUSEKEEPING_WINDOWS_HOURS:
    EXPIRY = 48.0
    REMINDER_1_START = 12.0
    REMINDER_1_END = 24.0
    REMINDER_2_START = 24.0
    REMINDER_2_END = 36.0


class TOKEN_REMINDER_DAYS:
    LINKEDIN_WARNING = 50
    LINKEDIN_URGENT = 58
    PAT_WARNING = 80


class URLS:
    TELEGRAM_BOT_API_BASE = "https://api.telegram.org/bot"
    GITHUB_MODELS_CHAT_COMPLETIONS = "https://models.github.ai/inference/chat/completions"


class ERROR_MESSAGES:
    TELEGRAM_ENV_REQUIRED = "TELEGRAM_POST_BOT_TOKEN and TELEGRAM_CHAT_ID are required"
    WF_POST_ID_REQUIRED = "WF_POST_ID is required for resend action"
    POSTS_JSON_ARRAY_REQUIRED = "posts.json must be a JSON array"
    CONFIG_JSON_OBJECT_REQUIRED = "config.json must be an object"
    POSTS_JSON_ARRAY_REQUIRED_HOUSEKEEPING = "posts.json must be an array"
    GITHUB_TOKEN_REQUIRED = "GITHUB_TOKEN is required for GitHub Models"
    TELEGRAM_RESPONSE_MISSING_MESSAGE_ID = "Telegram send response missing message_id"


HALF_DAY_WINDOW = 0.5
MAX_CALLBACK_DATA_BYTES = 64

