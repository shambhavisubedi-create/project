# GoGather — System Audit

Prepared alongside the places/packages MySQL migration, from a source review of
this codebase plus queries against the dev MySQL database. Not exhaustive — a
deeper pass (dependency audit, load testing) would surface more.

Scope: `app.py`, `db.py`, `place_store.py`, `templates/`, the MySQL schema.

---

## Part 1 — Problems found

Ranked by how much damage they could do, not by how easy they are to fix.

### P-01 — Critical: the session-signing key is a hardcoded placeholder string
**Where:** `app.py` — `app.secret_key = "any-random-string-goes-here"`

Flask uses this key to sign the session cookie that stores `user_id` and role.
Because the real value sits in the source file instead of the environment,
anyone who can read the code can forge a valid cookie with `user=1` and become
an admin with no password.

**Fix:** generate a long random value, read it via
`os.environ.get("SECRET_KEY")`, and keep it out of version control the same
way the Stripe keys already are.

### P-02 — Critical: CSRF protection exists for one form, and no others
**Where:** `app.py` — only `/admin/places` checks a token

Manage Places checks a `places_csrf` token before saving. Every other
state-changing action — adding or editing a package, approving or rejecting a
booking or travel group, cancelling a booking, joining a group, updating a
profile, uploading an avatar — accepts a plain POST with nothing checked. A
page on another site can submit one of these on behalf of whoever is
currently logged in.

**Fix:** write one small CSRF helper (or add Flask-WTF) and apply it to every
form that changes data, not just Manage Places.

### P-03 — Moderate: login has no limit on failed attempts
**Where:** `app.py` — `/login`

There's no lockout, delay, or CAPTCHA after repeated wrong passwords, so an
account (including the admin account) can be brute-forced at whatever rate
the network allows.

**Fix:** throttle by username and IP — Flask-Limiter is a small addition for
this.

### P-04 — Moderate: the dev debugger is hardcoded on
**Where:** `app.py` — `app.run(debug=True)`

Harmless on your own machine. If this app is ever started this same way
somewhere reachable beyond localhost, Werkzeug's interactive debugger is a
known path to running arbitrary code on the server.

**Fix:** drive it from an environment variable, defaulting to `False`.

### P-05 — Minor: no dependency manifest
**Where:** project root — no `requirements.txt`

There's no pinned list of Python packages, so setting this project up on
another machine means installing things by trial and error until nothing
crashes.

**Fix:** `pip freeze > requirements.txt` once, then keep it current as you
add packages.

### P-06 — Minor: an orphaned "places" table was already sitting in the database
**Where:** MySQL — table `places` (id, name, description, icon)

Found while building the new destinations table for this migration. Six
rows, a completely different shape from anything the app reads today —
leftover from an earlier, abandoned version of this same feature. Kept out of
the way by naming the new table `explore_places` instead of reusing the name.

**Fix:** once you've confirmed nothing external depends on it, drop it.

### P-07 — Minor: two test files are currently red
**Where:** `test_notifications.py`, `test_reports.py` — 7 failing tests

Not caused by this migration — confirmed by running them with the new
package/place code disabled and watching them fail the same way. The real
cause: newer schema-migration helpers (avatar column, booking payment
columns, group columns) run inside the same request hook these tests mock,
and the tests were never updated to expect that.

**Fix:** have those tests mock the `ensure_*_columns()` helpers directly
instead of mocking `query`/`execute` underneath them.

---

## Part 2 — Features worth adding

Not bugs — gaps. Grouped by who feels them: the traveller booking a trip, the
admin running the place day to day, or anyone trusting the platform with an
account and a payment.

### Trust & safety
- **F-01 — Forgot-password flow.** Right now the only recovery path for a
  locked-out user is editing the database by hand.
- **F-02 — Admin action log.** Approvals overwrite a status field with no
  record of who approved it or when — add a small audit trail for
  accountability.
- **F-03 — Email on booking decisions.** The only signal a traveller gets
  today is the in-app bell. Most people expect an email when a trip is
  confirmed or paid.

### Traveller experience
- **F-04 — Photo galleries.** Packages and destinations each store one
  `image` column — a real listing needs several photos per place.
- **F-05 — Reviews & ratings.** No way for a traveller who finished a trip to
  rate it, and no social proof for the next person deciding.
- **F-06 — Wishlist.** No way to save a package or destination to come back
  to without booking it right away.

### Admin operations
- **F-07 — Pagination on admin lists.** Bookings, packages, places, and
  travellers all load every row in one query — fine now, slow once any list
  passes ~100 rows.
- **F-08 — Export from Reports.** The reports page charts the numbers but
  there's no CSV or PDF export for a funder or a board update.
- **F-09 — A role between Traveller and Admin.** Only two fixed roles exist.
  A "support" role — can view bookings, can't change prices — matters once
  the team grows past one admin.
- **F-10 — A real "trip is over" status for travel groups.** Right now a
  group only ever has Pending / Confirmed / Rejected. Once a group is
  Confirmed, it stays labeled Confirmed forever, even a year after the trip
  happened — there's no way to look at the group list and tell which trips
  already happened and which are still upcoming. (Bookings already do this
  a little better — an admin can mark one "Completed," and the traveller's
  own bookings page splits into Upcoming/Past. Groups never got that.)

---

## Part 3 — Roadmap to completion

Theory only — nothing below has been built yet. Phased so each phase is safe
to ship before starting the next one. Item codes (P-xx / F-xx) point back to
Part 1 / Part 2 above.

### Phase 1 — Lock the doors
*Do this before anyone besides you uses the live app, even for a demo.*

1. Move the session secret key out of the source code and into an
   environment variable (**P-01**).
2. Add CSRF protection to every form that changes data, not just Manage
   Places (**P-02**).
3. Add a login attempt limit (**P-03**).
4. Make debug mode environment-controlled, off by default (**P-04**).

Nothing here changes what the app *does* for a user — it just closes the
doors that are currently unlocked. Low risk, should go first.

### Phase 2 — Make the data trustworthy
*Fix the gaps that make "what's the real state of this booking/group?" a
guessing game.*

1. **Trip completion status (F-10).** Give travel groups the same
   Upcoming/Past split bookings already have — either a real status change
   once the travel date passes, or at minimum a computed "Past" badge shown
   next to the group everywhere it's listed (traveller's groups page, admin's
   groups page). This is the piece that answers "has this group finished?"
   without you having to check the date by hand.
2. **Admin action log (F-02).** Record who approved/rejected a booking or
   group, and when — not just the current status.
3. Housekeeping while the schema is already in motion: drop the orphaned
   `places` table (**P-06**), add `requirements.txt` (**P-05**), fix the two
   red test files (**P-07**).

### Phase 3 — Round out the traveller experience
*The features a traveller would expect from any real booking site.*

1. Forgot-password flow (**F-01**) — do this early; it's a support burden
   every day it's missing.
2. Email notification on booking/group decisions (**F-03**).
3. Photo galleries for packages and destinations (**F-04**).
4. Reviews & ratings (**F-05**).
5. Wishlist / saved packages (**F-06**).

### Phase 4 — Scale the admin side
*Matters once there's real data volume or more than one admin.*

1. Pagination on admin lists — bookings, packages, places, travellers
   (**F-07**).
2. CSV/PDF export from Reports (**F-08**).
3. A role between Traveller and Admin (**F-09**).

### Phase 5 — Ready for real users
*Not in Part 1/2 above because these are launch-readiness gaps, not bugs in
what exists — things every production app needs regardless of feature set.*

1. Real hosting (not the Flask dev server) behind HTTPS, with the domain
   pointed at it.
2. Environment-based config for all secrets (Stripe, eSewa, DB, mail) —
   builds on Phase 1's secret-key fix.
3. Automated MySQL backups, and a tested restore process.
4. Basic monitoring/error alerting, so a crash doesn't get discovered by a
   traveller before it gets discovered by you.
5. A privacy policy and terms of use — you're already collecting phone
   numbers and payment info.

Phases 1–2 are about safety and correctness; 3–4 are about feature
completeness; 5 is about being ready to actually operate with paying
travellers. Nothing here has to happen in exactly this order except Phase 1
going first.
