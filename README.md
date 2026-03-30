# LinkedIn Auto Post Scheduler

![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)
![Python](https://img.shields.io/badge/Python-3.12-blue)
![Node](https://img.shields.io/badge/Node-22-green)
![CI](https://img.shields.io/badge/CI-GitHub_Actions-black)

Automates a review-first LinkedIn posting workflow:
- generates technical draft posts from curated topics
- sends drafts to Telegram for approve/edit/reject actions
- publishes approved posts to LinkedIn through a Cloudflare Worker webhook
- keeps repository state in JSON files for an auditable history

## Architecture

1. `scripts/generate.py` picks an unused topic, generates draft copy with GitHub Models, writes to `posts.json`, and sends a Telegram message with inline actions.
2. Telegram callback requests hit the Cloudflare Worker (`worker/src/index.ts`) at `/webhook`.
3. The Worker validates the caller and webhook secret, updates `posts.json` on the automation branch via the GitHub Contents API, and opens/reuses a PR to `main`.
4. `scripts/housekeeping.py` runs periodically for reminders and expiry handling.

Detailed behavior and data contracts are documented in `docs/ARCHITECTURE.md`.

## Repository Layout

- `scripts/` Python automation and quality checks
- `worker/` Cloudflare Worker webhook + Telegram/LinkedIn integrations
- `.github/workflows/` scheduled generation, housekeeping, and CI quality gate
- `topics.json` source topics
- `posts.json` post state machine records
- `config.json` token lifecycle reminder timestamps

## Prerequisites

- Python `3.12+`
- Node.js `22+`
- Git
- Cloudflare account (for Worker deploy)
- Telegram bot(s) and LinkedIn API credentials

## Quickstart (Local)

1. Clone and enter the repository.
2. Create local environment file:
   - `copy .env.example .env` (Windows PowerShell)
   - fill all required values in `.env`
3. Install Python dependencies:
   - `pip install -r requirements.txt`
4. Install Worker dependencies:
   - `npm ci --prefix worker`
5. (Optional) enable repo hooks:
   - `git config core.hooksPath .githooks`

## Local Commands

- Generate a new draft:
  - `python scripts/generate.py`
- Resend an existing pending draft to Telegram:
  - set `WF_ACTION=resend` and `WF_POST_ID=<post_id>` in your shell, then run `python scripts/generate.py`
- Run housekeeping reminders/expiry:
  - `python scripts/housekeeping.py`
- Recover or upsert a pending draft record:
  - `python scripts/recover_draft.py --help`
- Run quality checks:
  - `python scripts/quality_gate.py`
- Run Worker locally:
  - `npm run dev --prefix worker`

## Cloudflare Worker Setup

1. Configure KV binding in `worker/wrangler.toml` if needed.
2. Set Worker runtime secrets (one-time per environment), for example:
   - `wrangler secret put GH_FINE_GRAINED_PAT`
   - `wrangler secret put GH_REPO`
   - `wrangler secret put GH_STATE_BRANCH` (optional, default `bot/automation-state`)
   - `wrangler secret put GH_BASE_BRANCH` (optional, default `main`)
   - `wrangler secret put TELEGRAM_POST_BOT_TOKEN`
   - `wrangler secret put TELEGRAM_LOG_BOT_TOKEN`
   - `wrangler secret put TELEGRAM_LOG_CHAT_ID`
   - `wrangler secret put TELEGRAM_WEBHOOK_SECRET`
   - `wrangler secret put TELEGRAM_CHAT_ID`
   - `wrangler secret put TELEGRAM_USER_ID`
   - `wrangler secret put LINKEDIN_ACCESS_TOKEN`
   - `wrangler secret put LINKEDIN_PERSON_ID`
3. Deploy:
   - `npm run deploy --prefix worker`
4. Register Telegram webhook to your Worker:
   - `https://api.telegram.org/bot<TELEGRAM_POST_BOT_TOKEN>/setWebhook?url=<WORKER_URL>/webhook&secret_token=<TELEGRAM_WEBHOOK_SECRET>`

## GitHub Actions Workflows

| Workflow | File | Trigger | Purpose |
| --- | --- | --- | --- |
| Generate draft | `.github/workflows/generate.yml` | schedule + manual dispatch | Create or resend draft posts |
| Housekeeping | `.github/workflows/housekeeping.yml` | schedule + manual dispatch | Send reminders, expire stale drafts |
| Quality Gate | `.github/workflows/quality-gate.yml` | push + pull request | Python compile + TypeScript check |
| Secret Scan | `.github/workflows/secret-scan.yml` | push + pull request + manual | Detect accidentally committed credentials |

`generate` and `housekeeping` include a preflight secret check. In forks or fresh clones
without secrets configured, they skip gracefully with a warning instead of failing.

## Secret Boundaries

Use this split to decide where each value should live:

### Local-only (`.env`, never committed)

Use `.env.example` as the source of truth for local execution.

### GitHub Actions secrets (repo settings)

Required for scheduled/manual draft generation and housekeeping:
- `TELEGRAM_POST_BOT_TOKEN`
- `TELEGRAM_CHAT_ID`
- `TELEGRAM_LOG_BOT_TOKEN` (optional, recommended)
- `TELEGRAM_LOG_CHAT_ID` (optional, recommended)

Note: `GITHUB_TOKEN` is automatically provided by GitHub Actions.

### Cloudflare Worker runtime secrets

Required by webhook publish flow:
- `GH_FINE_GRAINED_PAT`
- `GH_REPO`
- `GH_STATE_BRANCH` (optional, default `bot/automation-state`)
- `GH_BASE_BRANCH` (optional, default `main`)
- `TELEGRAM_POST_BOT_TOKEN`
- `TELEGRAM_LOG_BOT_TOKEN` (optional)
- `TELEGRAM_LOG_CHAT_ID` (optional)
- `TELEGRAM_WEBHOOK_SECRET`
- `TELEGRAM_CHAT_ID`
- `TELEGRAM_USER_ID`
- `LINKEDIN_ACCESS_TOKEN`
- `LINKEDIN_PERSON_ID`

## Troubleshooting

- `401 Unauthorized` on Worker webhook:
  - verify `x-telegram-bot-api-secret-token` matches `TELEGRAM_WEBHOOK_SECRET`
- LinkedIn publish errors similar to `NONEXISTENT_VERSION`:
  - check and update `LINKEDIN_VERSION` in `worker/common/constants.ts`
- Telegram callbacks ignored:
  - verify `TELEGRAM_USER_ID` and `TELEGRAM_CHAT_ID` match your account/chat
- Worker cannot create/update PR:
  - ensure `GH_FINE_GRAINED_PAT` has repository `Contents (Read and write)` and `Pull requests (Read and write)`
- CI quality gate fails:
  - run `python scripts/quality_gate.py` locally and fix reported checks

## Security Notes

- `.env` must remain untracked; use GitHub/Cloudflare secret stores for runtime credentials.
- Rotate leaked credentials immediately (Telegram bot tokens, PATs, LinkedIn tokens).
- Secret scanning runs in CI via `.github/workflows/secret-scan.yml`.
- Replay/idempotency validation steps are documented in `docs/ARCHITECTURE.md` under "Manual Replay Validation Checklist".
- Review `SECURITY.md` for vulnerability reporting and response expectations.

## Contributing

See `CONTRIBUTING.md` for development flow and pull request requirements.

## License

Licensed under MIT. See `LICENSE`.
