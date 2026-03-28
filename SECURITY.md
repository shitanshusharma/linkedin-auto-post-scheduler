# Security Policy

## Supported Versions

Security updates are applied to the latest code on `main`.

## Reporting a Vulnerability

If you discover a security issue, do not open a public issue with exploit details.

Preferred reporting path:
- Use GitHub's private vulnerability reporting (Security Advisories) for this repository.

If private reporting is unavailable:
- Open a minimal public issue that says you found a security concern.
- Avoid sharing payloads, tokens, stack traces with secrets, or reproduction steps publicly.
- Maintainers will follow up through a private channel.

## What to Include

Please include:
- affected component/file(s)
- impact summary
- step-by-step reproduction
- proof-of-concept (if safe)
- suggested mitigation (optional)

## Response Expectations

- Initial triage target: within 72 hours.
- We will confirm receipt, assess severity, and share remediation status.
- We will coordinate disclosure timing after a fix is available.

## Secret Exposure Guidance

If credentials are exposed (for example bot tokens, PATs, or API tokens):
- rotate affected secrets immediately
- invalidate old webhook secrets
- review commit history and workflow logs for additional leakage
