# Phase 3: Deployment readiness and easier interaction

Plain Python / Flask / HTML / CSS only. No new pip packages (email uses the
standard library's `smtplib`). To deploy, follow `DEPLOYMENT.md`.

## Security problems found and fixed

| Problem | Fix |
|---|---|
| **Forgot-password showed the OTP on screen.** Knowing someone's phone number was enough to take over their account. | The code is now emailed to the address on the account, never shown. Same response whether or not the account exists. Guesses are rate-limited; requesting a new code retires old ones. Accepts phone or email. |
| Open redirect: wishlist/review forms redirected to any `next` value. | `safe_next()` only allows same-site paths. |
| No limit on sign-ups or reset attempts. | `rate_limited()` sliding window on register, forgot-password, OTP check, change-password, support tickets. |
| Session cookie flags, security headers, cached account pages. | `HttpOnly`, `SameSite=Lax`, `Secure` (production), HSTS, `nosniff`, `X-Frame-Options`, `Referrer-Policy`, `no-store` on logged-in pages, 8 MB request cap. |
| Behind nginx every visitor looked like one IP. | `TRUST_PROXY=1` enables `ProxyFix`. |

## Deployment features

- `/healthz` (200/503 by database reachability) and `/robots.txt`, favicon.
- Friendly error pages (400/403/404/405/413/429/500) instead of stack traces; full tracebacks go to `logs/gogather.log` (rotating).
- `schema.sql`: base schema so a brand-new server can be created (before, only the app's later add-on tables were self-created).
- `manage.py`: `check` (go-live preflight), `init-db`, `create-admin`, `send-reminders`, `backup` (retention).
- `wsgi.py` for gunicorn/waitress; `DEPLOYMENT.md` with nginx/HTTPS/systemd/cron/monitoring/checklist.
- `mailer.py` + `notify(..., email_subject=...)`: emails for booking decisions, payments, refunds, trip reminders, support replies. A mail failure never breaks the booking or payment.
- Trip reminders: once per confirmed booking (`bookings.reminder_sent_at`), via cron or lazily once an hour.
- `run_schema_migrations()`: one function for every self-migrating table/column (also fixes the test-drift problem).

## Easier for travellers

- **Save without leaving the page:** hearts update in place with a toast and a live count badge on the sidebar Wishlist link (works without JavaScript too).
- **Explore → Save → Book:** destination pages list the packages that visit them, with Book buttons; wishlist cards have *Book now* / *Find packages*.
- **Dashboard "next step" card** and a Save → Book → Pay → Travel progress strip, driven by real data (fixes the dead "View" link on featured packages).
- **Search box in the top bar** for destinations and packages together.
- **Package filters:** price range, trip length, sort (price, duration, top rated).
- **Help Centre:** FAQ, contact form (optionally tied to a booking), and replies tracked per request; admins get a Support inbox with an open-count badge.
- **Change password** on the profile page.
- **Double-click protection:** submit buttons lock while a form is sending (prevents duplicate bookings/payments/tickets); back-to-top button.

## Tests

- `test_deployment_features.py` (27 tests) covers all of the above, including no OTP leak, open redirect, brute-force limit and one-reminder-per-booking.
- Fixed 3 failing booking tests (they never sent the required `accept_policy`) and made `test_notifications.py` patch `run_schema_migrations` instead of a list of helpers that goes stale.
- 49 tests pass in total. The heart toggle, badge, filters, search and button lock were also driven in a real headless Chrome.

## Not done (needs decisions or external accounts)

Real SMTP credentials, live eSewa/Stripe credentials, hosting/HTTPS/domain, off-server backup storage, SMS, staff/support role, package comparison, English/Nepali, Content-Security-Policy (the templates use inline styles and scripts), seat-capacity row locking.
