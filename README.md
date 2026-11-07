# Ultrafine Sales Orders

Sales creates sales orders from customer POs, the accounts in-charge approves them, and approved orders are
queued for Tally. FastAPI + MySQL, server-rendered (Jinja2), served on port **1401**.

## Roles

| Role | Can do |
|---|---|
| Sales | Create, edit (draft or sent back), submit, withdraw and delete own orders. Sees only own orders. |
| Accounts in-charge | Approve or send back submitted orders (not their own). Manage customers, items, sales ledgers. |
| Admin | Everything above except creating orders, plus users, settings, audit log and the Tally queue. |

Order flow: `Draft -> Awaiting approval -> Approved` (or `Sent back -> edit -> Awaiting approval`).
Order numbers (`SO/26-27/00001`, `TSO/...` for trading) are allocated gap-free on first submit, per financial year.

Tax rule from the SOP: billing state equals the company state -> Output CGST + Output SGST (half rate each);
otherwise Output IGST at the full rate. The company has several plants, so each order names its supplying plant and the
plant's GST state is what the customer's billing state is compared with. Plants are master data (**Master data > Plants**,
seeded with the 13 states the company operates in); orders cannot be created until a plant is chosen, so tax is never guessed.

## Run

```powershell
.\venv\Scripts\python.exe -m alembic upgrade head      # create / update tables
.\venv\Scripts\python.exe -m app.cli seed-admin        # first admin from ADMIN_EMAIL / ADMIN_PASSWORD in .env
.\start.ps1                                            # start detached on http://127.0.0.1:1401  (stop: .\stop.ps1)
.\venv\Scripts\python.exe -m app.cli serve             # or run in the foreground
.\venv\Scripts\python.exe -m pytest                    # tests (uses TEST_DB_NAME, never the real DB)
```

First-time setup after signing in as admin: Plants (check the seeded list), Settings (tax ledger names), then Customers, Items and
Sales ledgers (names must match Tally exactly), then create users. Change the seeded admin password.

## Notifications, email and forgot password

- **Bell** (top right of every page) shows the unread count and the latest notifications; it refreshes every 30 seconds
  and pops a toast for new ones. The polling does not count as activity, so an abandoned tab still signs out on time.
  **Notifications** lists everything with search, filters, sorting, paging, download, mark read/unread and remove.
- **Who is told what:** approvers when an order is submitted; the creator when it is approved, sent back or deleted; creator
  and approver when it reaches Tally; admins when Tally delivery gives up; approvers a once-a-day reminder for orders waiting
  more than `PENDING_REMINDER_HOURS`; everyone about password changes, lockouts and changes to their access.
- **Settings** (`/account/notifications`): each person chooses in-app and/or email per category. Security notices are always on.
- **Email** goes from `MAIL_FROM` through the `SMTP_*` settings in `.env`. Every email is a row in the database, written in the
  same transaction as the event and sent by a background worker (retries with back-off, 6 tries). **Administration > Email
  log** shows queued, first/last attempt and sent times, errors, retry, and a "send me a test email" button.
  Check the mail login without sending anything: `python -m app.cli mail-check`.
- **Forgot password:** `/forgot-password` emails a one-time link (30 minutes, newest link only, stored as a hash). The answer is
  identical for unknown addresses, requests are throttled, the link is built from `APP_BASE_URL` (never from the request),
  and the stored email body is wiped once delivered. Admins can also email a link from **Users**.
- Set `APP_BASE_URL` to the address your staff really open (for example `http://192.168.1.20:1401`), or emailed links will not work for them.

## Tally integration

Approval writes the order and a `tally_outbox` row in **one transaction**, so nothing is lost if Tally is down.
A background worker delivers rows (SKIP LOCKED claim, exponential backoff, 8 attempts, then `dead` for an admin to
retry from **Tally** in the menu). With `TALLY_API_URL` empty, rows simply wait. When the API is known, only
`HttpTallyGateway.send` in `app/integrations/tally.py` needs adjusting (for example rendering the stored JSON
payload as Tally XML). An `Idempotency-Key` header is sent with every request.

## Security measures

- Passwords: argon2id, policy for new passwords, constant-time-ish login for unknown users, lockout after 5 failures
  (15 min), per-IP and per-account throttling, generic error messages, forced change on admin-issued passwords.
- Sessions: random 256-bit token in an HttpOnly, SameSite=Lax cookie; only its SHA-256 is stored; idle (30 min) and
  absolute (12 h) expiry; revoked on logout, password change, role change, deactivation.
- CSRF: signed double-submit token on every state-changing request plus Origin check; token rotates at login.
- Authorization on the server for every route; other users' orders return 404 (no id enumeration); approver cannot
  approve own order; at least one active admin is always kept.
- Strict CSP (no inline scripts or styles), HSTS in production, nosniff, frame denial, no-store caching, request
  size cap, trusted-host check, docs/OpenAPI disabled in production, generic error pages with a request id.
- Injection: SQLAlchemy parameterised queries, Jinja autoescape, strict numeric parsing (Decimal, no NaN/Infinity),
  open-redirect-safe `next`.
- Integrity: soft delete everywhere, snapshots of names/addresses on orders, optimistic locking plus row locks on
  every transition, DB check constraints, append-only audit log of logins and every change.
- Secrets live in `.env` (git-ignored), never in code.

## Before going live

- Serve behind HTTPS (reverse proxy), set `APP_ENV=production`, `COOKIE_SECURE=true`, real `ALLOWED_HOSTS`.
- Use a dedicated MySQL user with rights on `tally_so` only, not `root`.
- Back up the database regularly; restrict who can reach port 1401.
- Rate limiting is in-process (one worker). With several workers, add a shared limiter at the proxy.
