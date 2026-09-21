# Phase 2 — Feature Completion Pass

Implemented on 18 September 2026, as a follow-up to `SECURITY_FIXES_APPLIED.md`
(Phase 1). This covers the feature gaps identified after that pass: seat
capacity, reviews, an itinerary editor, pagination, CSV export, three
pre-existing test-suite bugs, an orphaned database table, a new automated
test file, and a privacy policy page. Only Python (Flask), HTML and CSS were
used — no new third-party packages.

Every feature below was exercised against the real app (Flask's test client
against the live dev database, or a real HTTP request) — see "How it was
tested" under each section. The full test suite (`test_places.py`,
`test_bookings.py`, `test_notifications.py`, `test_reports.py` — 22 tests
total) passes.

---

## 1. Package seat capacity

**Problem before:** a package's booking overlap check only looked at one
traveller's own bookings — nothing stopped a limited-capacity trek (say, a
6-person trek) from being "confirmed" for 50 different travellers on the
same departure date.

**What it does now:** an admin can optionally set a package's **seats per
departure date** (Manage Packages → Edit details). If set, a booking that
would push the total travellers already booked for that exact package +
date over the limit is rejected with a clear message, before it's ever
created.

`app.py`:
```python
def seats_booked_for_date(package_id, travel_date):
    row = query(
        """
        SELECT COALESCE(SUM(travellers), 0) AS total FROM bookings
        WHERE package_id = %s AND travel_date = %s AND status != 'Cancelled'
        """,
        (package_id, travel_date), fetch="one",
    )
    return row["total"]
```
`book_package()`'s validation chain gained one more branch, checked after
the existing overlap check:
```python
elif package["max_seats"] is not None and (
    seats_booked_for_date(package["id"], travel_date) + travellers > package["max_seats"]
):
    seats_left = max(package["max_seats"] - seats_booked_for_date(package["id"], travel_date), 0)
    flash(f"Only {seats_left} seat(s) left on '{package['name']}' for {travel_date}. ...", "error")
```
`max_seats` is `NULL` by default (unlimited) — every existing package keeps
behaving exactly as before until an admin opts a package into a limit.

**Files touched:** `app.py` (new column, helper, validation branch, admin
add/update forms), `templates/admin_packages.html` (seats field),
`templates/book_package.html` / `package_detail.html` (seat-limit note for
travellers).

**How it was tested:** `test_bookings.py::SeatCapacityTests` — books up to
the limit (succeeds), a booking that would exceed it (rejected, correct
seats-left message, no extra row created), and confirms an unlimited
package (`max_seats = NULL`) still accepts a large booking.

---

## 2. Reviews & ratings

**Problem before:** no way for a traveller to rate a completed trip, and no
social proof anywhere on a package's page.

**What it does now:** once a booking is `Completed`, its traveller can
leave a 1–5 star rating with an optional comment from **My Bookings**.
Submitting again on the same booking edits the review instead of creating a
second one (enforced by a `UNIQUE(booking_id)` key, not just app logic).
Reviews show on the package's public page (with the average rating) and as
a small badge on the packages grid. Admins can moderate (remove) any review
from Manage Packages.

```sql
CREATE TABLE IF NOT EXISTS package_reviews (
    id INT AUTO_INCREMENT PRIMARY KEY,
    package_id INT NOT NULL,
    user_id INT NOT NULL,
    booking_id INT NOT NULL,
    rating TINYINT NOT NULL,
    comment TEXT NULL,
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    UNIQUE KEY uniq_booking_review (booking_id),
    FOREIGN KEY (package_id) REFERENCES packages(id) ON DELETE CASCADE,
    FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE,
    FOREIGN KEY (booking_id) REFERENCES bookings(id) ON DELETE CASCADE
)
```

New routes: `POST /my_bookings/<id>/review` (submit/edit — blocked unless
the booking is `Completed`, rating must be 1–5) and
`POST /admin/reviews/<id>/delete` (admin moderation).

**Files touched:** `app.py`, `templates/my_bookings.html` (review form),
`templates/package_detail.html` (review list + average), `templates/packages.html`
(rating badge), `templates/admin_packages.html` (moderation panel).

**How it was tested:** `test_bookings.py::ReviewTests` — a review on a
non-completed booking is rejected; a review on a completed booking is
accepted and a second submission edits it in place rather than duplicating;
an out-of-range rating (e.g. 9) is rejected.

---

## 3. Admin itinerary editor (packages)

**Problem before:** the day-by-day itinerary tables
(`package_itinerary_days` / `package_itinerary_activities` /
`package_itinerary_extras`) already existed and were seeded with real
content, but had no admin UI — the only way to change one was to run SQL by
hand.

**What it does now:** Manage Packages gained an "Day-by-day itinerary"
panel per package (same disclosure style as the existing photo gallery):
add or remove a day (bounded to the package's duration), add or remove a
timed activity within a day, and edit the packing list / "things to know"
notes.

**Scope note:** this was built for **packages only**, not destinations
(`place_itinerary_*`) — the two share the same table shape, so extending
this to Manage Places would mean repeating the same pattern with `place_id`
instead of `package_id`. Left as a follow-up since packages were the higher
priority (they're what's actually booked and paid for).

**Files touched:** `app.py` (5 new routes + `get_editable_itinerary()`
helper), `templates/admin_packages.html`.

**How it was tested:** exercised via the Flask test client against the live
database — added a day, added an activity to it, removed both, and updated
the packing list on a real package; confirmed each change round-tripped
through `admin_packages()`'s own read query.

---

## 4. Pagination on admin lists

**Problem before:** Manage Packages, Manage Bookings, Manage Places and
Manage Travellers all loaded every row in one query — fine at today's data
volume, slow once any of them passes ~100 rows (flagged in
`SYSTEM_AUDIT.md` as F-07).

**What it does now:** all four pages now show 25 rows at a time (`PAGE_SIZE
= 25` in `app.py`), with a Previous/Next control that preserves whatever
search/filter is active. Manage Bookings' existing "group by status"
display groups within the current page rather than the whole table; its
site-wide summary cards (Pending count, etc.) are unaffected since those
run their own un-paginated `COUNT(*)` query.

**How it was tested:** loaded all four pages via the test client and
confirmed each still renders (200) with the new query shape; manually
checked the generated SQL includes `LIMIT %s OFFSET %s` with the right
bind values.

---

## 5. CSV export from Reports

**Problem before:** Reports charted the numbers but had no way to hand raw
figures to a funder or a board update (F-08).

**What it does now:** a "Download CSV" button next to the existing period
filter on `/admin/reports`, exporting exactly the bookings the current
period covers (one row per booking: reference, traveller, package, travel
date, traveller count, amount, margin, status, payment status, booked-on
date).

```python
@app.route("/admin/reports/export.csv")
def export_reports_csv():
    ...
    output = StringIO()
    writer = csv.writer(output)
    writer.writerow([...header...])
    for booking in bookings:
        writer.writerow([...])
    return Response(output.getvalue(), mimetype="text/csv",
                     headers={"Content-Disposition": f"attachment; filename={filename}"})
```
The period-to-date-range logic (`report_period_bounds()`) was pulled out of
`admin_reports()` into its own function so the page and its export can
never disagree about what "Last 30 days" means.

**How it was tested:** downloaded the CSV via the test client for
`period=all` and confirmed the header row and real booking rows came back
correctly formatted.

---

## 6. Three pre-existing test failures, fixed

All three were confirmed (via `git stash`, or by reading the code) to
predate this session — not caused by the security/feature work above, but
fixed here since a red test suite hides real regressions.

- **`test_places.py`** — `explore_places()` reads `session["user_id"]`,
  but the test's fake `login()` helper only ever set `username`/`user`.
  Real logins always set all three together, so the fix was to the test
  helper, not the route.
- **`test_notifications.py`** — two things had drifted: (1) every POST now
  goes through the new site-wide CSRF check from Phase 1, which these
  mocked-query tests never primed with a token, so they got a 400 before
  reaching the route logic at all; (2) `prepare_notifications()` (the
  before_request hook) has grown many more `ensure_*()` migration calls
  since this file was written, and the tests only mocked one of them, so
  the rest ran for real against a mocked `execute`/`query` and polluted
  call-count assertions. Fixed by priming a CSRF token before each POST and
  mocking every `ensure_*()` helper the hook now calls — exactly the fix
  `SYSTEM_AUDIT.md` (P-07) already recommended.
- **`test_reports.py`** — two similar cases: a test rendered
  `admin_dashboard.html` directly with only one of the ~16 variables the
  template now needs (crashing on the rest as `Undefined`), and another's
  mocked booking rows were missing `payment_status`/`cost_price`, which
  `admin_reports()` now reads for the paid-value/margin totals. Fixed by
  supplying the full variable set / the missing keys.

**How it was tested:** `python -m unittest discover -p "test_*.py"` — all
22 tests pass.

---

## 7. Orphaned `places` MySQL table dropped

The `SYSTEM_AUDIT.md` finding (P-06) — a 6-row table left over from an
earlier, abandoned version of the destinations feature, unrelated to the
`explore_places` table the app actually uses. Re-confirmed no code
references it (the one `FROM places` hit in `place_store.py` is a *SQLite*
query against a legacy local file, not this MySQL table) before dropping
it, and asked for explicit confirmation before running the `DROP TABLE`
against the live database. Confirmed and dropped.

---

## 8. New automated tests: `test_bookings.py`

**Problem before:** the booking, refund and payment flows had no
regression coverage at all (only `test_places.py` existed).

**What it adds:** 6 tests covering the two features built in this session
that touch money/inventory directly — the ones most worth protecting from a
silent regression:
- `SeatCapacityTests` (3 tests) — capacity respected, capacity enforced,
  unlimited packages unaffected.
- `ReviewTests` (3 tests) — completion-gated, edit-in-place on resubmit,
  rating range validated.

Follows the same pattern as `test_places.py`: runs against the real dev
database with disposable rows created in `setUp` and deleted in `tearDown`
(cascading deletes clean up bookings/reviews automatically).

---

## 9. Privacy policy page

**Problem before:** GoGather collects phone numbers, payment references and
booking history with no privacy policy anywhere (flagged as a
launch-readiness gap).

**What it does now:** a real `/privacy` page (not filler text) describing
what GoGather actually collects (account details, booking/payment
references, profile photo, reviews, session cookie), why, who can see it,
retention, and the security measures already in place (password hashing,
CSRF protection, login rate-limiting). Linked from both the login and
register pages.

---

## What's still not done, and why

These were on the original list but need something this session doesn't
have access to — infrastructure, credentials, or a business decision — so
they weren't faked:

- **Real SMS delivery for the password-reset OTP** — needs a paid SMS
  gateway account (Twilio, Sparrow SMS, etc.) and its API key. The OTP is
  still generated correctly; it's just shown on-page instead of texted,
  exactly as documented in `SMALL_FEATURES_ADDED.md`.
- **Booking/refund email notifications** — needs an SMTP provider or
  transactional email service (SendGrid, etc.) and credentials.
- **Real hosting, HTTPS, automated backups, error monitoring** — these are
  infrastructure and vendor decisions (which host, which backup tool, which
  monitoring service) that need you to choose and provision, not code
  changes.

---

## Algorithms and techniques used, in plain terms

Since these are the parts of the work that aren't just "add a form and a
database column":

- **Constant-time comparison for CSRF/session tokens** (`secrets.compare_digest`,
  already used for the old Manage Places token, kept for the new
  site-wide one) — a normal `==` string comparison can leak *how much* of
  the token was correct through tiny timing differences, letting an
  attacker guess it one byte at a time. `compare_digest` always takes the
  same amount of time no matter how wrong the guess is, closing that gap.
- **Synchronizer token pattern** for CSRF protection generally: a random,
  unguessable value is stored server-side in the session and must be echoed
  back by the form. An attacker's page on another site can trick a
  browser into *sending* a request, but can't *read* the victim's session
  to learn the token to include, so the forged request gets rejected.
- **Fixed-window rate limiting** for login throttling: a counter per
  username+IP that resets after a time window, rather than tracking every
  individual attempt's timestamp — the simplest form of brute-force
  slowdown, trading perfect precision for a few lines of code.
- **Capacity/overbooking check as an aggregate guard**: seat capacity isn't
  tracked as a running "seats remaining" counter (which could drift out of
  sync); it's recomputed on demand as `SUM(travellers) WHERE ... AND status
  != 'Cancelled'`, so it's always derived from the actual booking rows and
  can't disagree with them.
- **Upsert via a UNIQUE constraint** (`UNIQUE(booking_id)` on reviews,
  `ON DUPLICATE KEY UPDATE` on itinerary extras): instead of an app-level
  "check if it exists, then decide insert vs. update" race, the database
  itself enforces that a second attempt lands on the same row.
- **Offset-based pagination** (`LIMIT %s OFFSET %s` plus a separate
  `COUNT(*)` for the page total): the simplest pagination strategy — not
  the most efficient at huge scale (an OFFSET of 100,000 still has to skip
  100,000 rows), but the right tradeoff here since these admin lists are
  nowhere near that size, and it's a two-line change instead of a
  keyset-pagination rewrite.
