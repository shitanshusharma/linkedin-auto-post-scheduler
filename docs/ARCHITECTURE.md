# Architecture

Last updated: 2026-03-29

This document is the practical architecture reference for this repository.
It intentionally combines high-level and low-level details in one place so the project stays easy to maintain.

## 1) Goal and Constraints

Goal: run a review-first LinkedIn posting system that is low-cost, auditable, and safe.

Core constraints:
- Human approval required before publish.
- Only one active draft at a time.
- No automatic retry after publish failures.
- Secrets never committed.
- Main state is file-based (`posts.json`, `topics.json`, `config.json`).

## 2) End-to-End Shape

There are three independent trigger paths:

1. Weekly generation (`.github/workflows/generate.yml`)
   - Generate one draft from an unused topic.
   - Validate and store it.
   - Send Telegram approval buttons.

2. Housekeeping (`.github/workflows/housekeeping.yml`)
   - Send reminders for stale drafts.
   - Expire old drafts.
   - Send token/PAT lifecycle reminders.

3. Webhook interaction (`worker/src/index.ts`)
   - Receive Telegram callbacks/messages.
   - Validate request and user.
   - Apply state transition (approve/edit/reject/retry).
   - Publish to LinkedIn when approved/retried.

## 3) Core Components

- `scripts/generate.py`
  - Topic selection, LLM generation, schema validation, Telegram draft dispatch.
- `scripts/housekeeping.py`
  - Reminder/expiry lifecycle checks and notifications.
- `worker/src/index.ts`
  - Telegram webhook handler, decision engine, GitHub Contents API writes, LinkedIn publish.
- `posts.json`
  - Source of truth for post state.
- `topics.json`
  - Backlog of unused/used topics.
- `config.json`
  - Token/PAT timestamps for lifecycle reminders.

## 4) Data Contracts

### 4.1 LLM output contract

Expected JSON shape:

```json
{
  "hook": "string, <= 150 chars",
  "body": "string, <= 1650 chars",
  "cta": "string, <= 200 chars",
  "risk_flags": ["string"]
}
```

Rules:
- Must be valid JSON with exactly these keys.
- Combined `hook + body + cta` must be <= 2000 chars.
- No raw HTML tags.

### 4.2 Stored post contract (`posts.json`)

Each post record contains:
- Identity and routing: `id`, `topic`, `approval_token`, `telegram_message_id`
- State: `status`
- Content: `content` (`hook/body/cta`), `composed_text`, `proposed_edit`
- Publish fields: `publish_attempted_at`, `posted_at`, `linkedin_post_id`, `error`
- Audit fields: `created_at`, `approved_at`

Allowed statuses:
- `pending`
- `editing`
- `confirming_edit`
- `approved`
- `rejected`
- `posted`
- `failed`
- `expired`

### 4.3 Topic/config contracts

- `topics.json`: list of `{ id, title, used }`
- `config.json`: token rotation timestamps, including:
  - `linkedin_token_refreshed_at`
  - `pat_created_at`

## 5) Decision Engine (State Machine)

Primary actions and expected transitions:
- `Approve` callback: `pending -> approved -> posted|failed`
- `Edit` callback: `pending -> editing`
- `Reject` callback: `pending -> rejected`
- Edited text (valid): `editing -> confirming_edit`
- Confirm edit: `confirming_edit -> pending` (updated content, re-send approval buttons)
- Re-enter edit: `confirming_edit -> editing`
- Retry callback: `failed -> posted|failed`
- `cancel` message while editing: `editing -> pending`

Guardrails:
- Action must match current status.
- Callback token must match post token.
- Telegram sender must match configured `TELEGRAM_USER_ID`.
- Only one active post is allowed (`pending`, `editing`, `confirming_edit` block new generation).

## 6) Publish Flow (LinkedIn)

On approve/retry:
1. Set `publish_attempted_at`.
2. Sanitize `composed_text`.
3. Call LinkedIn Posts API (`POST /rest/posts`) with:
   - Bearer token
   - `LinkedIn-Version` header
   - author URN (`LINKEDIN_PERSON_ID`)
4. Handle outcome:
   - `201`: mark `posted`, store `linkedin_post_id`.
   - non-`201` (`401`, `429`, timeout, etc.): mark/keep `failed`, store error, show Retry button.

Rule: do not auto-retry publish failures.

## 7) Webhook Security Model

Requests are validated in layers:
1. Cloudflare WAF allowlist for Telegram source IP ranges.
2. Verify `X-Telegram-Bot-Api-Secret-Token`.
3. Rate limit (KV-backed per-user counter).
4. Verify Telegram user ID.
5. Verify callback approval token (for callback actions).
6. Verify action is valid for current post status.

If GitHub write conflict (`409`) occurs:
- Re-fetch latest file/sha.
- Re-apply intended change.
- Retry once.

## 8) Reliability Rules

- `posts.json` is the source of truth for workflow state.
- Log-bot writes are best effort and must never block the main flow.
- Housekeeping sends:
  - Draft reminders around 12h and 24h.
  - Draft expiry around 48h.
  - Token/PAT warnings from `config.json` timestamps.
- If topic backlog is exhausted, generation is skipped and user is notified.

## 9) Secret Boundaries

- Local development: `.env` (untracked).
- GitHub Actions: generation/housekeeping secrets in repository settings.
- Cloudflare Worker: runtime secrets for webhook and LinkedIn publish.

Use least privilege (fine-grained PAT, minimal permissions, scoped secrets).

## 10) Design Principles

- Deterministic behavior over hidden automation.
- Human-in-the-loop publishing.
- Fast approvals, deliberate decisions.
- Security in layers.
- Simple storage and explicit state transitions.

## 11) Manual Replay Validation Checklist

Use this checklist in staging after deploying idempotency changes.

Prerequisites:
- Worker deployed with both KV bindings: `RATE_LIMIT_KV` and `IDEMPOTENCY_KV`.
- Telegram webhook configured with the same `TELEGRAM_WEBHOOK_SECRET` used by the Worker.
- At least one `pending` post exists in `posts.json`.

Set local variables:

```bash
export WORKER_URL="https://<your-worker>.workers.dev/webhook"
export TG_SECRET="<telegram_webhook_secret>"
```

### Test A: Bad secret is rejected

```bash
curl -i -X POST "$WORKER_URL" \
  -H "Content-Type: application/json" \
  -H "X-Telegram-Bot-Api-Secret-Token: wrong-secret" \
  -d '{"update_id":100001,"message":{"message_id":1,"chat":{"id":123},"from":{"id":123},"text":"hello"}}'
```

Expected:
- HTTP `401`.
- No post state change.
- No idempotency side-effect that blocks a later valid update.

### Test B: First valid callback is processed once

Use a real `post_id` + `approval_token` from a pending post.

```bash
curl -i -X POST "$WORKER_URL" \
  -H "Content-Type: application/json" \
  -H "X-Telegram-Bot-Api-Secret-Token: $TG_SECRET" \
  -d '{"update_id":100002,"callback_query":{"id":"cb-100002","from":{"id":<TELEGRAM_USER_ID>},"data":"a:<post_id>:<approval_token>","message":{"message_id":<telegram_message_id>,"chat":{"id":<TELEGRAM_CHAT_ID>}}}}'
```

Expected:
- HTTP `200`.
- Post transitions through approve/publish flow exactly once.

### Test C: Replay same callback is ignored

Repeat the exact same request from Test B (same `update_id` and callback payload).

Expected:
- HTTP `200`.
- No second publish attempt.
- No duplicate state transition in `posts.json`.

### Test D: Replay same edit message is ignored

If a post is in `editing`, send the same message update twice:

```bash
curl -i -X POST "$WORKER_URL" \
  -H "Content-Type: application/json" \
  -H "X-Telegram-Bot-Api-Secret-Token: $TG_SECRET" \
  -d '{"update_id":100003,"message":{"message_id":77,"chat":{"id":<TELEGRAM_CHAT_ID>},"from":{"id":<TELEGRAM_USER_ID>},"text":"Rewritten draft text"}}'
```

Send the same payload again.

Expected:
- First request applies transition (`editing` -> `confirming_edit`).
- Second request returns `200` but does not apply changes again.

### Test E: Approve race safety (quick smoke)

Send two approve callbacks for the same post in quick succession with different update IDs:
- One request should win transition to `approved`.
- The other should be blocked as action-not-allowed or no-op.
- Only one publish flow should proceed.
