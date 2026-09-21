# Security Fixes Applied — Phase 1 (Lock the Doors)

Implemented on 18 September 2026, covering the four critical/moderate items
from Part 1 of `SYSTEM_AUDIT.md` (P-01 through P-04) plus the housekeeping
item P-05. Only Python (Flask), HTML and CSS were used — no new third-party
packages (no Flask-WTF, no Flask-Limiter). Everything here is a small,
self-contained helper, following the same style already used in this
codebase (e.g. the original `places_csrf` token, `ensure_*` migration
helpers).

Every fix was tested against the running app (real HTTP requests with
`curl`), not just read by eye — see "How it was tested" under each section.

---

## 1. Session secret key moved out of the source code (P-01)

**Problem before:** [app.py](app.py) had
`app.secret_key = "any-random-string-goes-here"` — a literal string sitting
in version control. Flask uses this key to cryptographically sign the
session cookie that stores `user_id` and `role`. Anyone who could read the
source (anyone with repo access, now or in the future) could forge a cookie
claiming to be `user_id=1, role=1` and log in as an admin with no password
at all.

**What it does now:** the key is read from the environment instead, and the
app refuses to start at all if it's missing — it doesn't fall back to
anything insecure.

```python
app.secret_key = os.environ.get("SECRET_KEY")
if not app.secret_key:
    raise RuntimeError(
        "SECRET_KEY is not set. Add SECRET_KEY=<a long random value> to your "
        ".env file (see .env.example) before starting the app."
    )
```

- `.env` (already git-ignored, confirmed via `.gitignore`) now has a real
  generated value:
  `SECRET_KEY=b6d7e80aa3d51915451ab45c7872c4b6dd9d8bf9ccee77a29fbe6f4b66b02bc0`
- `.env.example` *(new file)* — a template with every variable the app
  reads (DB, secret key, debug flag, eSewa, Stripe), with blanks instead of
  real values, safe to commit.
- **Anyone else setting this project up** generates their own with:
  `python -c "import secrets; print(secrets.token_hex(32))"`

**Consequence to be aware of:** changing the secret key invalidates every
currently logged-in session (this happened once, tonight, when this was
first set) — nobody's data was affected, they just had to log in again.

**How it was tested:** started the app with `.env`'s `SECRET_KEY` present —
boots and serves `/login` normally. Temporarily removing the variable and
starting again raises the `RuntimeError` above instead of starting insecurely
(reverted after confirming).

---

## 2. CSRF protection extended to every form, not just Manage Places (P-02)

**Problem before:** only `/admin/places` checked a token
(`session["places_csrf"]`) before saving. Every other state-changing
action — booking a package, cancelling a booking, requesting a refund,
approving/rejecting a booking or travel group, joining or leaving a group,
sending a group chat message, updating a profile, adding/removing a photo,
toggling the wishlist, marking notifications read — accepted a plain POST
with nothing checked. A malicious page on another site could submit any of
these on behalf of whoever was currently logged in, just by getting them to
visit it.

**What it does now:** one CSRF token per login session (not one per form),
checked automatically on every POST request site-wide.

`app.py` — the token itself, exposed to every template, generated on first
use of the session:

```python
def csrf_token():
    return session.get("csrf_token", "")


app.jinja_env.globals["csrf_token"] = csrf_token


@app.before_request
def ensure_csrf_token():
    session.setdefault("csrf_token", secrets.token_hex(32))


@app.before_request
def check_csrf():
    if request.method == "POST" and request.endpoint not in CSRF_EXEMPT_ENDPOINTS:
        expected = session.get("csrf_token", "")
        submitted = request.form.get("csrf_token", "")
        if not expected or not secrets.compare_digest(expected, submitted):
            abort(400)
```

`CSRF_EXEMPT_ENDPOINTS` has exactly one entry: `stripe_webhook`. Stripe
calls that route server-to-server (never from a browser with our session
cookie), and it's already authenticated a different way — by verifying
Stripe's own request signature (`stripe.Webhook.construct_event(...)`).
Nothing else is exempt.

**Every POST form in the app** now carries the token as a hidden field:

```html
<input type="hidden" name="csrf_token" value="{{ csrf_token() }}">
```

This was added to 47 forms across 21 template files (every POST form
except the one in `esewa_redirect.html`, which submits to eSewa's own
payment gateway, not to this app, so it has nothing to do with our CSRF
protection). The old `/admin/places`-only token (`session["places_csrf"]`,
checked by hand inside `manage_places()`) was removed and replaced by the
same global mechanism everything else now uses — one less special case in
the codebase, not an extra one.

**Files touched:**
- `app.py` — new CSRF helper (above), removed the old ad-hoc
  `places_csrf` logic from `manage_places()`.
- `templates/manage_places.html` — its two existing tokens switched from
  `{{ session.places_csrf }}` to `{{ csrf_token() }}`; its two other forms
  (add/remove a destination photo), which had never been protected, got the
  hidden field added.
- 20 more template files, one hidden input per POST form: `admin_bookings.html`,
  `admin_packages.html`, `admin_refunds.html`, `admin_travel_groups.html`,
  `book_package.html`, `explore_places.html`, `forgot_password.html`,
  `group_chat.html`, `login.html`, `manage_travellers.html`,
  `my_bookings.html`, `notifications.html`, `package_detail.html`,
  `packages.html`, `place_detail.html`, `profile.html`, `register.html`,
  `travel_groups.html`, `verify_otp.html`, `wishlist.html`.
- `test_places.py` — one line changed (`session['places_csrf']` →
  `session['csrf_token']`) so its existing CSRF test still reads the token
  from the right place.

**How it was tested**, against the real running app with `curl`:
1. `POST /login` with no `csrf_token` field at all → **400 Bad Request**
   (rejected before the login logic even runs).
2. `GET /login`, extracted the token from the rendered page's hidden input,
   `POST /login` with that exact token → **200**, request goes through
   normally (username/password logic runs as before).
3. Re-ran the pre-existing `test_places.py` suite (the only automated test
   file this project has) — its CSRF-specific test
   (`test_validation_and_csrf`, which posts a wrong token and expects 400)
   still passes.

---

## 3. Login attempts are throttled (P-03)

**Problem before:** `/login` had no limit on repeated wrong passwords — an
account, including the admin account, could be brute-forced as fast as the
network allowed.

**What it does now:** after 5 failed attempts for the same
username-and-IP combination within a short window, that combination is
locked out for 5 minutes.

```python
LOGIN_ATTEMPT_LIMIT = 5
LOGIN_LOCKOUT_SECONDS = 300

_login_attempts = {}


def _login_throttle_key(username):
    return f"{username.strip().lower()}:{request.remote_addr}"


def _register_failed_login(key):
    entry = _login_attempts.setdefault(key, {"count": 0, "locked_until": 0})
    entry["count"] += 1
    if entry["count"] >= LOGIN_ATTEMPT_LIMIT:
        entry["locked_until"] = time.time() + LOGIN_LOCKOUT_SECONDS
        entry["count"] = 0
```

Keyed by **username + IP together**, not just one or the other:
- One person mistyping their own password repeatedly doesn't lock out
  everyone else on the same office/home network.
- Guessing through many different usernames from one IP is still slowed
  down, one username at a time.

A successful login clears that key's attempt count immediately. No
new table was added — this lives in memory for the lifetime of the running
app, which is enough for a single-process deployment (the same way this
project already runs). If this app is ever run with multiple worker
processes, this would need to move into the database instead, the same way
`refund_requests` or `admin_activity_log` already do — noted here so it
isn't forgotten.

**How it was tested:** scripted 6 wrong-password attempts in a row against
a live running instance. Attempts 1–5 each returned "Invalid username or
password."; attempt 6 returned "Too many failed login attempts. Please try
again in 5 minute(s)." instead of even checking the password.

---

## 4. Debug mode is off unless explicitly turned on (P-04)

**Problem before:** [app.py](app.py) had `app.run(debug=True)` hardcoded.
Flask's debug mode includes Werkzeug's interactive in-browser debugger,
which lets anyone who can reach it run arbitrary Python on the server — a
known, serious risk if this app is ever reachable from outside your own
machine while running this way.

**What it does now:**

```python
app.run(debug=os.environ.get("FLASK_DEBUG", "0") == "1")
```

Off by default. `.env` sets `FLASK_DEBUG=1` so local development keeps
working exactly as before (auto-reload, in-browser tracebacks); anywhere
else, leaving it unset means debug mode simply isn't available.

**How it was tested:** started the app with `.env`'s `FLASK_DEBUG=1` —
console showed `Debug mode: on`, same as before this change.

---

## 5. Dependency manifest added (P-05)

**Problem before:** no `requirements.txt` existed for this project at all,
including for dependencies that were already in use (Flask, Stripe,
eSewa's `requests`, MySQL, `reportlab` for e-tickets) — setting this project
up on another machine meant installing packages by trial and error.

**What it does now:** `requirements.txt` *(new file)*, pinned to the
versions actually installed and working in this environment:

```
Flask==3.1.3
python-dotenv==1.0.1
requests==2.34.2
stripe==15.6.1
mysql-connector-python==9.6.0
reportlab==5.0.1
Werkzeug==3.1.8
```

Install with `pip install -r requirements.txt`.

---

## Files touched, all changes

**Backend**
- `app.py` — secret key, CSRF helper + global check, login throttling,
  debug-mode flag.
- `test_places.py` — one line, updated to the new session key name.

**New files**
- `.env.example` — safe-to-commit template of every environment variable
  the app reads.
- `requirements.txt` — pinned dependency list.
- `SECURITY_FIXES_APPLIED.md` — this file.

**Changed**
- `.env` — added `SECRET_KEY` and `FLASK_DEBUG` (already git-ignored, not
  committed).
- 21 template files — hidden `csrf_token` field added to every POST form
  (listed in full under section 2 above).

**Not touched:** anything unrelated to these five items. In particular, the
booking/refund/package/group logic itself, the database schema, and every
GET-only page are unchanged.

---

## What's still open from the audit (not done here, out of scope for this pass)

Everything below is still exactly as `SYSTEM_AUDIT.md` and
`SMALL_FEATURES_ADDED.md` (§8, "Roadmap") describe it — not touched in this
round:

- The orphaned `places` MySQL table (P-06) — safe to `DROP` once confirmed
  nothing external reads it.
- `test_notifications.py` / `test_reports.py` (P-07) — pre-existing
  failures, unrelated to security.
- One more pre-existing, unrelated issue found *while testing this work*:
  `test_places.py`'s `test_add_edit_delete_and_traveller_visibility` now
  fails with `KeyError: 'user_id'` inside `explore_places()`
  ([app.py](app.py), the `wishlisted_place_ids=get_wishlisted_place_ids(session["user_id"])`
  line). Confirmed via `git stash` that this predates this session — it's
  part of the not-yet-committed wishlist feature, which assumes a logged-in
  traveller always has `user_id` in session, but this test's fake login
  helper only sets `username`/`user`. Not a security issue and not
  something this pass touched; flagging it the same way the wishlist
  session's own write-up flagged `test_reports.py`'s pre-existing failure.
- Reviews & ratings, package seat capacity, an admin itinerary editor, and
  real SMS/email delivery — all still not built, as previously discussed.

Phase 1 of the roadmap (P-01 through P-04) is now complete. P-05 was
included too since it was a quick, low-risk addition alongside it.
