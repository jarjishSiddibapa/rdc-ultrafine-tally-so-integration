# Security

## Reporting a problem

Please do not open a public issue for a security problem. Use GitHub's **Report a vulnerability** button on the Security tab of this repository, or email jarjishsiddibapa@gmail.com with steps to reproduce. You will get a reply within a few days.

## What is protected, and how

- Passwords: argon2id, a password policy, lockout after repeated failures, per-IP and per-account throttling, generic error messages.
- Sessions: random tokens in HttpOnly, SameSite cookies; only a SHA-256 hash is stored; idle and absolute expiry; revoked on logout, password change and role change.
- CSRF: signed double-submit token plus an Origin check on every state-changing request.
- Authorisation: checked on the server for every route; other users' orders return 404.
- Injection and output: parameterised SQLAlchemy queries, Jinja autoescaping, strict numeric parsing.
- Headers: a strict Content Security Policy, HSTS in production, no-store caching, frame denial, request size cap, trusted-host check.
- Data integrity: soft deletes, optimistic locking and row locks on every state change, and an append-only audit log.
- Secrets live in `.env`, which is git-ignored. This repository contains no real credentials, customers or GSTINs; demo data is fictional.

The detailed list and the go-live checklist are in [docs/USER_GUIDE.md](docs/USER_GUIDE.md#security-measures).
