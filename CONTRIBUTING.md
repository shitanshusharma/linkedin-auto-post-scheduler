# Contributing Guide

Thanks for contributing to this project.

## Before You Start

- Search existing issues and pull requests to avoid duplicates.
- For larger changes, open an issue first to align on scope.
- Keep changes focused and incremental.

## Development Setup

1. Fork the repository and create a branch from `main`.
2. Install dependencies:
   - `pip install -r requirements.txt`
   - `npm ci --prefix worker`
3. Create local env:
   - `copy .env.example .env`
   - fill required values in `.env`

## Branch and Commit Conventions

- Branch naming examples:
  - `feat/<short-description>`
  - `fix/<short-description>`
  - `docs/<short-description>`
- Commit message style follows conventional prefixes used in this repo:
  - `feat: ...`
  - `fix: ...`
  - `chore: ...`
  - `docs: ...`

## Quality Checks

Run this before opening a pull request:

- `python scripts/quality_gate.py`

The quality gate includes:
- Python compile checks for `scripts/`
- TypeScript type checks for `worker/`

## Pull Request Checklist

- Scope is clear and limited to one concern.
- New behavior is documented in `README.md` and/or `docs/ARCHITECTURE.md`.
- Security impact is considered (especially secrets, tokens, and webhook handling).
- Quality gate passes locally.

## Security-Sensitive Changes

If your change touches auth, secrets, webhook validation, or publishing logic:
- include a short risk analysis in the PR description
- describe rollback steps if applicable

## Code of Conduct

By participating, you agree to follow `CODE_OF_CONDUCT.md`.
