export const POSTS_PATH = "posts.json";
export const CONFIG_PATH = "config.json";
export const DEFAULT_AUTOMATION_BRANCH = "bot/automation-state";
export const DEFAULT_BASE_BRANCH = "main";
export const MAX_TELEGRAM_POST_LENGTH = 2000;
export const RATE_LIMIT_MAX_REQUESTS_PER_MINUTE = 20;
export const IDEMPOTENCY_TTL_SECONDS = 60 * 60 * 24;
export const CALLBACK_DATA_MAX_BYTES = 64;
export const MAX_WEBHOOK_BODY_BYTES = 64 * 1024;
export const LINKEDIN_VERSION = "202603";
export const WORKER_SYNC_PR_TITLE = "chore(worker): sync automation state";
export const WORKER_SYNC_PR_BODY = "Automated PR for Worker state updates (`posts.json`).";
export const GIT_WRITE_TARGET_KEY = "git_write_target";
export const GIT_BASE_BRANCH_KEY = "git_base_branch";
export const GIT_AUTOMATION_BRANCH_KEY = "git_automation_branch";
export const GIT_WRITE_TARGETS = {
  MAIN: "main",
  BOT: "bot",
} as const;
export const POST_STATUSES = {
  PENDING: "pending",
  EDITING: "editing",
  CONFIRMING_EDIT: "confirming_edit",
  APPROVED: "approved",
  REJECTED: "rejected",
  POSTED: "posted",
  FAILED: "failed",
  EXPIRED: "expired",
} as const;

export const API_URLS = {
  TELEGRAM_BOT_API_BASE: "https://api.telegram.org/bot",
  GITHUB_REPO_API_BASE: "https://api.github.com/repos",
  LINKEDIN_POSTS: "https://api.linkedin.com/rest/posts",
} as const;

export const RESPONSE_MESSAGES = {
  CALLBACK_UNAUTHORIZED: "Unauthorized",
  CALLBACK_INVALID_ACTION: "Invalid action",
  CALLBACK_POST_NOT_FOUND: "Post not found",
  CALLBACK_INVALID_TOKEN: "Invalid token",
  CALLBACK_ACTION_NOT_ALLOWED: "Action not allowed",
  CALLBACK_PUBLISHING: "Publishing...",
  CALLBACK_EDIT_MODE_ENABLED: "Edit mode enabled",
  CALLBACK_REJECTED: "Rejected",
  CALLBACK_INVALID_EDIT_TEXT: "Invalid edited text",
  CALLBACK_EDIT_APPLIED: "Edit applied",
  CALLBACK_EDIT_REENTER: "Please edit again",
  CALLBACK_RETRYING_PUBLISH: "Retrying publish...",
  CALLBACK_RATE_LIMITED: "Rate limited. Try again later.",
  CALLBACK_TRY_AGAIN: "Try again.",
  MESSAGE_RATE_LIMITED: "Rate limited. Try again later.",
  MESSAGE_TRY_AGAIN: "Try again.",
  MESSAGE_EDIT_EMPTY: "Edit cannot be empty. Send text (1-2000 chars) or 'cancel'.",
  MESSAGE_EDIT_TOO_LONG: "Edit too long. Max 2000 characters. Send again or 'cancel'.",
  MESSAGE_EDIT_REENTER_PROMPT: "Send your corrected post text again.",
  HTTP_NOT_FOUND: "Not found",
  HTTP_UNAUTHORIZED: "Unauthorized",
  HTTP_BAD_REQUEST: "Bad Request",
  HTTP_PAYLOAD_TOO_LARGE: "Payload Too Large",
  HTTP_INTERNAL_SERVER_ERROR: "Internal Server Error",
} as const;

export const ERROR_CODES = {
  EMPTY_POST_AFTER_SANITIZATION: "empty_post_after_sanitization",
  POST_EXCEEDS_MAX_LENGTH: "post_exceeds_max_length",
  TIMEOUT: "timeout",
  NETWORK_ERROR: "network_error",
  LINKEDIN_REAUTH_REQUIRED: "linkedin_401_reauth_required",
  LINKEDIN_RATE_LIMITED: "linkedin_429_rate_limited",
  GITHUB_CONFLICT: "github_conflict",
  UNKNOWN_WORKER_ERROR: "unknown_worker_error",
} as const;

