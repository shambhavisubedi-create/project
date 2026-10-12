# Small features added — code reference

This documents the three "small" features added in this session: **Forgot/Reset
Password**, **Wishlist**, and **PDF E-Ticket** — plus the earlier
**auto-complete booking status** change they build on top of. For each
feature: what problem it solves, the exact code added, which file it lives
in, and how to test it.

New Python dependency: `reportlab` (installed via `pip install reportlab`,
used only by the e-ticket feature). No `requirements.txt` exists in this
project yet, so it isn't pinned anywhere — see the note at the end.

---

## 0. Auto-complete past bookings (context for the "Completed" status)

**Problem:** Bookings sat at `Confirmed` forever unless an admin remembered
to manually flip them to `Completed` after the trip ended.

**Fix:** A booking whose trip has already ended flips to `Completed`
automatically, the moment anyone next loads a page that reads bookings.

`app.py` — new function, placed just above `has_overlapping_booking`:

```python
def auto_complete_past_bookings():
    """A Confirmed booking whose trip has already ended flips to Completed on
    its own, the moment anyone next loads a page that reads bookings --
    nobody has to remember to close it out by hand. Logged under a 'System'
    actor (rather than log_admin_activity's session-based one) since this can
    run from a traveller's own page load, not just an admin's."""
    due = query(
        """
        SELECT b.id, b.user_id, b.package_name
        FROM bookings b
        LEFT JOIN packages p ON p.id = b.package_id
        WHERE b.status = 'Confirmed'
          AND DATE_ADD(b.travel_date, INTERVAL COALESCE(p.duration_days, 1) - 1 DAY) < CURDATE()
        """
    )
    for booking in due:
        execute("UPDATE bookings SET status = 'Completed' WHERE id = %s", (booking["id"],))
        notify(
            booking["user_id"],
            f"Your booking for '{booking['package_name']}' is now marked completed. We hope you had a great trip!",
            url_for("my_bookings"),
        )
        execute(
            """
            INSERT INTO admin_activity_log
                (admin_id, admin_username, action, entity_type, entity_id, entity_label, old_value, new_value, notes)
            VALUES (NULL, 'System', 'booking.status_updated', 'booking', %s, %s, 'Confirmed', 'Completed', 'Auto-completed: travel date has passed')
            """,
            (str(booking["id"]), booking["package_name"]),
        )
```

Called at the top of three routes (right after the login/role check):
`admin_dashboard()`, `admin_bookings()`, `my_bookings()`.

**Caveat:** this is *lazy*, not a real scheduler — it only runs when someone
loads one of those three pages. See "Not built" at the end for the
cron/APScheduler alternative.

---

## 1. Forgot / Reset Password

**Problem:** No account-recovery path existed at all — losing your password
meant losing the account.

**Design constraint:** this app has no email service wired up (no SMTP,
no SendGrid, nothing), so a real "check your inbox" flow can't deliver
anything. Rather than fake it, the reset link is generated exactly the way
it would be for email delivery (a one-time token with an expiry, stored in
the database) — it's just handed back directly on the page instead of
mailed, with a note explaining that. Swap that hand-back for an actual
`send_mail(...)` call before this goes to production.

### New table

`app.py`, new migration function alongside the other `ensure_*` functions:

```python
# One row per password-reset link that's ever been issued -- a token is only
# usable once (used_at) and only for a short window (expires_at), so a stale
# or already-used link in someone's browser history can't reset a password.
def ensure_password_reset_table():
    execute(
        """
        CREATE TABLE IF NOT EXISTS password_reset_tokens (
            id INT AUTO_INCREMENT PRIMARY KEY,
            user_id INT NOT NULL,
            token VARCHAR(64) NOT NULL UNIQUE,
            expires_at TIMESTAMP NOT NULL,
            used_at TIMESTAMP NULL,
            created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
        )
        """
    )
```

Called directly inside both new routes below (not gated behind the
`before_request` login check, since these pages are used by people who
*aren't* logged in), and also in the `if __name__ == "__main__":` startup
block for good measure.

### Routes

`app.py`, added right after `register()`:

```python
RESET_TOKEN_VALID_MINUTES = 30


@app.route("/forgot-password", methods=["GET", "POST"])
def forgot_password():

    ensure_password_reset_table()

    reset_link = None

    if request.method == "POST":

        identifier = request.form.get("username", "").strip()

        user = query(
            "SELECT id FROM users WHERE username = %s OR email = %s LIMIT 1",
            (identifier, identifier), fetch="one",
        )

        if user:

            token = secrets.token_urlsafe(32)

            execute(
                f"""
                INSERT INTO password_reset_tokens (user_id, token, expires_at)
                VALUES (%s, %s, NOW() + INTERVAL {RESET_TOKEN_VALID_MINUTES} MINUTE)
                """,
                (user["id"], token),
            )

            reset_link = url_for("reset_password", token=token, _external=True)

        # Same message either way -- otherwise this form could be used to
        # check which usernames/emails are registered.
        flash("If that account exists, a reset link has been generated below.", "success")

    return render_template("forgot_password.html", reset_link=reset_link)


@app.route("/reset-password/<token>", methods=["GET", "POST"])
def reset_password(token):

    ensure_password_reset_table()

    reset_request = query(
        """
        SELECT id, user_id FROM password_reset_tokens
        WHERE token = %s AND used_at IS NULL AND expires_at > NOW()
        """,
        (token,), fetch="one",
    )

    if not reset_request:
        flash("This reset link is invalid or has expired. Please request a new one.", "error")
        return redirect(url_for("forgot_password"))

    error = None

    if request.method == "POST":

        password = request.form.get("password", "")
        confirm_password = request.form.get("confirm_password", "")

        if password != confirm_password:

            error = "Password and Confirm Password don't match."

        elif len(password) < 8:

            error = "Password must be at least 8 characters long."

        else:

            execute(
                "UPDATE users SET password_hash = %s WHERE id = %s",
                (generate_password_hash(password), reset_request["user_id"]),
            )
            execute(
                "UPDATE password_reset_tokens SET used_at = NOW() WHERE id = %s",
                (reset_request["id"],),
            )

            flash("Your password has been reset. You can now log in.", "success")
            return redirect(url_for("login"))

    return render_template("reset_password.html", error=error)
```

Why the token is checked the way it is:
- `used_at IS NULL` → a link can't be replayed after it's been used once.
- `expires_at > NOW()` → a 30-minutes-old link stops working even if unused.
- The *same* flash message is shown whether or not the account exists, so
  the form can't be used to fish for which usernames/emails are registered.

### New templates

- `templates/forgot_password.html` — reuses the login page's visual style
  (split-free single card). Shows the generated `reset_link` directly with
  an explanatory note when a matching account was found.
- `templates/reset_password.html` — same style, a simple new-password +
  confirm form.

### Template edits

`templates/login.html`:
1. Added a "Forgot password?" link under the password field:
   ```html
   <p class="subtitle" style="margin:-6px 0 16px; text-align:right">
     <a href="{{ url_for('forgot_password') }}">Forgot password?</a>
   </p>
   ```
2. **Bug fix found while testing:** this page never rendered flashed
   messages at all (only a local `error` variable) — so the "Your password
   has been reset" success flash would have silently vanished after
   redirecting here. Added:
   ```html
   {% with messages=get_flashed_messages(with_categories=true) %}
     {% for c,m in messages %}
       <div class="{{ 'error-box' if c=='error' else 'flash' }}">{{ m }}</div>
     {% endfor %}
   {% endwith %}
   ```
   plus a `.flash { background:#e5f3ec; ... }` CSS rule to match the
   `.error-box` styling already there.

### How to test it

1. Go to `/login` → click "Forgot password?".
2. Enter a username or email → the page shows a reset link directly (no
   email is sent).
3. Open that link → set a new password (min 8 characters, must match
   confirm) → redirected to `/login` with a success message.
4. Try opening the same reset link again → rejected as "invalid or has
   expired" (it's single-use).

---

## 2. Wishlist

**Problem:** No way to save a package to look at again later without
booking it immediately.

### New table

```python
# A traveller's saved-for-later packages. The unique key means "save" is
# really a toggle -- trying to insert the same pair twice just fails, so the
# route can tell add and remove apart by whether a row already exists.
def ensure_wishlist_table():
    execute(
        """
        CREATE TABLE IF NOT EXISTS wishlist_items (
            id INT AUTO_INCREMENT PRIMARY KEY,
            user_id INT NOT NULL,
            package_id INT NOT NULL,
            created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
            UNIQUE KEY uniq_user_package (user_id, package_id),
            FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE,
            FOREIGN KEY (package_id) REFERENCES packages(id) ON DELETE CASCADE
        )
        """
    )
```

Added to the existing `@app.before_request` migration list (runs once per
app lifetime, gated on being logged in — fine here since wishlist always
requires login) and to the `__main__` startup block.

### Routes

Added after `package_detail()` in `app.py`:

```python
def get_wishlisted_package_ids(user_id):
    rows = query("SELECT package_id FROM wishlist_items WHERE user_id = %s", (user_id,))
    return {row["package_id"] for row in rows}


@app.route("/wishlist")
def wishlist():
    if "username" not in session:
        return redirect(url_for("login"))
    if session.get("user") != 0:
        return redirect(url_for("admin_dashboard"))

    package_rows = query(
        """
        SELECT p.id, p.name, p.duration_days, p.hotel_type, p.transport_mode, p.price,
               p.tag, p.summary, p.places_covered, p.activities, p.includes
        FROM wishlist_items w
        JOIN packages p ON p.id = w.package_id
        WHERE w.user_id = %s
        ORDER BY w.created_at DESC
        """,
        (session["user_id"],),
    )
    for package in package_rows:
        apply_package_details(package)

    return render_template("wishlist.html", packages=package_rows)


@app.route("/wishlist/<int:package_id>/toggle", methods=["POST"])
def toggle_wishlist(package_id):
    if "username" not in session:
        return redirect(url_for("login"))
    if session.get("user") != 0:
        return redirect(url_for("admin_dashboard"))

    existing = query(
        "SELECT id FROM wishlist_items WHERE user_id = %s AND package_id = %s",
        (session["user_id"], package_id), fetch="one",
    )

    if existing:
        execute("DELETE FROM wishlist_items WHERE id = %s", (existing["id"],))
        flash("Removed from your wishlist.", "success")
    else:
        execute(
            "INSERT INTO wishlist_items (user_id, package_id) VALUES (%s, %s)",
            (session["user_id"], package_id),
        )
        flash("Added to your wishlist.", "success")

    return redirect(request.form.get("next") or url_for("packages"))
```

`packages()` and `package_detail()` were each given one extra line to pass
`wishlisted_ids=get_wishlisted_package_ids(session["user_id"])` into their
templates, so the heart button knows whether to render filled or empty.

### New / edited templates

- **`templates/wishlist.html`** (new) — clone of the `packages.html` card
  grid, no filters, each card has a filled-heart "remove" button and a
  "View package details" link. Empty state points back to `/packages`.
- **`templates/packages.html`** — each card gets an absolutely-positioned
  heart button, top-right:
  ```html
  <form class="wish-form" method="post" action="{{ url_for('toggle_wishlist', package_id=package.id) }}">
    <input type="hidden" name="next" value="{{ request.full_path }}">
    <button class="wish-btn {{ 'saved' if package.id in wishlisted_ids else '' }}" type="submit"
            title="{{ 'Remove from wishlist' if package.id in wishlisted_ids else 'Save to wishlist' }}">
      {{ '♥' if package.id in wishlisted_ids else '♡' }}
    </button>
  </form>
  ```
  plus `.wish-form` / `.wish-btn` / `.wish-btn.saved` CSS.
- **`templates/package_detail.html`** — same idea as a full-width button
  under "Book this package":
  ```html
  <form method="post" action="{{ url_for('toggle_wishlist', package_id=package.id) }}">
    <input type="hidden" name="next" value="{{ request.full_path }}">
    <button class="wish-btn {{ 'saved' if package.id in wishlisted_ids else '' }}" type="submit">
      {{ '♥ Saved to wishlist' if package.id in wishlisted_ids else '♡ Save to wishlist' }}
    </button>
  </form>
  ```
  This page previously had **no flash-message rendering at all**, so a
  `flash()` from the toggle route would have sat unseen until some other
  page happened to display it. Added a flash block + `.flash` CSS here too.
- **`templates/_sidebar_traveller.html`** — new "Wishlist" nav link between
  "Book Package" and the rest, with a heart-outline SVG icon.

### How it works as a toggle

`wishlist_items` has `UNIQUE KEY uniq_user_package (user_id, package_id)`.
`toggle_wishlist` looks the row up first: if it exists, delete it (remove);
if not, insert it (save). The `next` hidden field carries the current page
back through the redirect, so clicking the heart on `/packages` or
`/packages/<id>` returns you to the same page instead of always bouncing to
`/packages`.

### How to test it

1. On `/packages`, click the ♡ on any card → it fills in (♥) and you land
   back on the same filtered/searched view.
2. Visit `/wishlist` → the saved package appears.
3. Click ♥ again (either on `/packages` or `/wishlist`) → it's removed.

---

## 3. PDF E-Ticket

**Problem:** No downloadable proof of booking for a traveller who's already
paid.

**Gate:** only offered for a booking that is `Confirmed` **or**
`Completed` **and** `payment_status == 'Paid'` — an unpaid booking doesn't
get a ticket, paid-but-still-pending doesn't either (nothing to confirm
yet).

### New dependency

```
pip install reportlab
```
Pure-Python wheel, no compiler needed. Not yet pinned in a
`requirements.txt` — see the closing note.

### Code

Added to `app.py` right after `cancel_booking()`:

```python
def build_ticket_pdf(booking):
    buffer = BytesIO()
    doc = pdf_canvas.Canvas(buffer, pagesize=A4)
    width, height = A4
    margin = 50

    doc.setFillColorRGB(0.663, 0.271, 0.361)
    doc.rect(0, height - 110, width, 110, fill=1, stroke=0)
    doc.setFillColorRGB(1, 1, 1)
    doc.setFont("Helvetica-Bold", 26)
    doc.drawString(margin, height - 65, "GoGather")
    doc.setFont("Helvetica", 13)
    doc.drawString(margin, height - 90, "E-Ticket / Booking Confirmation")

    y = height - 150
    doc.setFillColorRGB(0.17, 0.14, 0.13)
    doc.setFont("Helvetica-Bold", 18)
    doc.drawString(margin, y, booking["package_name"])
    y -= 34

    fields = [
        ("Booking Reference", f"#{booking['id']}"),
        ("Traveller", booking["full_name"]),
        ("Travel Date", booking["travel_date"].strftime("%d %B %Y")),
        ("Travellers", str(booking["travellers"])),
        ("Contact Phone", booking["phone"]),
        ("Amount Paid", format_npr(booking["price"])),
        ("Booking Status", booking["status"]),
    ]
    for label, value in fields:
        doc.setFont("Helvetica-Bold", 11)
        doc.drawString(margin, y, f"{label}:")
        doc.setFont("Helvetica", 11)
        doc.drawString(margin + 160, y, str(value))
        y -= 24

    y -= 10
    doc.setStrokeColorRGB(0.87, 0.81, 0.75)
    doc.line(margin, y, width - margin, y)
    y -= 24
    doc.setFont("Helvetica-Oblique", 9)
    doc.setFillColorRGB(0.51, 0.45, 0.42)
    doc.drawString(margin, y, "This e-ticket confirms your booking with GoGather. Please carry a valid ID during your trip.")
    y -= 14
    doc.drawString(margin, y, f"Generated on {date.today().strftime('%d %B %Y')}")

    doc.showPage()
    doc.save()
    buffer.seek(0)
    return buffer


@app.route("/my_bookings/<int:booking_id>/ticket")
def booking_ticket(booking_id):

    if "username" not in session:
        return redirect(url_for("login"))
    if session.get("user") != 0:
        return redirect(url_for("admin_dashboard"))

    booking = query(
        """
        SELECT b.id, b.package_name, b.travel_date, b.travellers, b.phone, b.price,
               b.status, b.payment_status, u.full_name
        FROM bookings b
        JOIN users u ON u.id = b.user_id
        WHERE b.id = %s AND b.user_id = %s
        """,
        (booking_id, session["user_id"]), fetch="one",
    )

    if not booking or booking["status"] not in ("Confirmed", "Completed") or booking["payment_status"] != "Paid":
        flash("An e-ticket is only available for a paid, confirmed booking.", "error")
        return redirect(url_for("my_bookings"))

    return send_file(
        build_ticket_pdf(booking),
        mimetype="application/pdf",
        as_attachment=True,
        download_name=f"GoGather-eTicket-{booking_id}.pdf",
    )
```

Note the ownership check: `WHERE b.id = %s AND b.user_id = %s` — a
traveller can only ever generate a ticket for their *own* booking, even if
they guess another booking's ID.

New imports this required, at the top of `app.py`:

```python
from flask import Flask, abort, flash, render_template, request, redirect, url_for, session, send_file
from io import BytesIO
from reportlab.lib.pagesizes import A4
from reportlab.pdfgen import canvas as pdf_canvas
```
(`canvas` was aliased to `pdf_canvas` to avoid clashing with anything else
named `canvas` elsewhere.)

### Template edits

`templates/my_bookings.html` — added a "Download e-ticket" button in two
places:

1. Upcoming trips, when `status == 'Confirmed'` and already `Paid` (next to
   the existing "Paid" badge, replacing the pay-now buttons that no longer
   apply):
   ```html
   {% if booking.payment_status == 'Paid' %}
     <span class="status paid">Paid</span>
     <div class="actions">
       <a class="pay pay-esewa" href="{{ url_for('booking_ticket', booking_id=booking.id) }}">Download e-ticket</a>
     </div>
   {% else %}
     ...existing pay-with-eSewa/Stripe buttons...
   {% endif %}
   ```
2. Travel history section, for any past booking that ended up
   `Confirmed`/`Completed` and `Paid`:
   ```html
   {% if booking.status in ('Confirmed', 'Completed') and booking.payment_status == 'Paid' %}
     <div class="actions">
       <a class="pay pay-esewa" href="{{ url_for('booking_ticket', booking_id=booking.id) }}">Download e-ticket</a>
     </div>
   {% endif %}
   ```

### How to test it

1. Find a booking in `my_bookings` that's `Confirmed`/`Completed` and shows
   the "Paid" badge.
2. Click "Download e-ticket" → downloads `GoGather-eTicket-<id>.pdf`.
3. Try hitting `/my_bookings/<some-other-users-booking-id>/ticket` directly
   → redirected back with "An e-ticket is only available for a paid,
   confirmed booking." (booking not found for this user, or not paid).

---

## Verification performed this session

All three features were exercised against the live `gogather` MySQL
database using Flask's test client (not the browser, since no admin/test
credentials were available):

- `auto_complete_past_bookings`: confirmed the SQL correctly picked up a
  booking whose trip had ended (Chitwan Wildlife Safari, ended 2026-09-09)
  and correctly left a future-dated one alone (Everest Base Camp Trek,
  starts 2026-09-18).
- Forgot/reset password: full round trip — request link → reset password →
  login page shows the success flash → re-using the same link is rejected
  as expired/used.
- Wishlist: add → appears on `/wishlist` → remove → wishlist empty again
  (no leftover rows).
- E-ticket: downloaded a real PDF (2189 bytes) for a paid, confirmed
  booking.

**Side effect to know about:** the reset-password round-trip test actually
changed the real password for the `dilasha` test account (created earlier
today) to `newtestpass123`, since there was no throwaway account to use
instead. That account was not otherwise touched by the wishlist/e-ticket
tests (both left the database in its original state).

---

## Not built (mentioned, not implemented)

- **Real-time/scheduled completion** — `auto_complete_past_bookings` only
  runs on a page load. A true always-on version would need `APScheduler`
  (a background job inside the Flask process) or an OS-level cron hitting a
  dedicated route.
- **Actual email delivery** for the reset link — currently shown on-page
  instead.
- **`requirements.txt`** — this project has none at all, not even for the
  pre-existing Flask/MySQL/Stripe/dotenv/requests dependencies, so
  `reportlab` wasn't added to one either. Say the word if you want one
  generated from what's actually imported.

---

## 4. Update: Forgot/Reset Password switched from link to phone OTP

Section 1 above (link-based reset) was replaced, not kept alongside. The
`/reset-password/<token>` route and `reset_password.html` template
described there **no longer exist** — here's what replaced them.

**Why:** a link-based reset has the same fundamental limitation as email —
there's no real delivery channel in this app. Switching to a phone number
doesn't fix that by itself (there's no SMS gateway either), but it does
make the flow shaped the way it will actually work once a real SMS
provider (Twilio, Sparrow SMS, etc.) is wired in — swap one line and it's
live.

### Route changes in `app.py`

`/forgot-password` now takes a **phone number** instead of a
username/email, generates a 6-digit numeric OTP, and redirects to a new
`/verify-otp` page instead of rendering a clickable link:

```python
OTP_VALID_MINUTES = 10


@app.route("/forgot-password", methods=["GET", "POST"])
def forgot_password():

    ensure_password_reset_table()

    if request.method == "POST":

        phone = request.form.get("phone", "").strip()

        user = query("SELECT id FROM users WHERE phone = %s LIMIT 1", (phone,), fetch="one")

        if user:

            otp_code = f"{secrets.randbelow(1000000):06d}"

            execute(
                f"""
                INSERT INTO password_reset_tokens (user_id, token, expires_at)
                VALUES (%s, %s, NOW() + INTERVAL {OTP_VALID_MINUTES} MINUTE)
                """,
                (user["id"], otp_code),
            )

            flash(
                f"Your OTP is {otp_code}. No SMS gateway is wired up yet, so it's shown here "
                f"instead of being texted to {phone} -- it expires in {OTP_VALID_MINUTES} minutes.",
                "success",
            )
            return redirect(url_for("verify_otp", phone=phone))

        # Same message either way -- otherwise this form could be used to
        # check which phone numbers are registered.
        flash("If that phone number is registered, an OTP has been generated.", "success")

    return render_template("forgot_password.html")


@app.route("/verify-otp", methods=["GET", "POST"])
def verify_otp():

    ensure_password_reset_table()

    phone = request.values.get("phone", "").strip()
    error = None

    if request.method == "POST":

        otp_code = request.form.get("otp", "").strip()
        password = request.form.get("password", "")
        confirm_password = request.form.get("confirm_password", "")

        user = query("SELECT id FROM users WHERE phone = %s LIMIT 1", (phone,), fetch="one")

        reset_request = query(
            """
            SELECT id, user_id FROM password_reset_tokens
            WHERE user_id = %s AND token = %s AND used_at IS NULL AND expires_at > NOW()
            ORDER BY id DESC LIMIT 1
            """,
            (user["id"] if user else 0, otp_code), fetch="one",
        )

        if not reset_request:

            error = "That code is invalid or has expired. Please request a new one."

        elif password != confirm_password:

            error = "Password and Confirm Password don't match."

        elif len(password) < 8:

            error = "Password must be at least 8 characters long."

        else:

            execute(
                "UPDATE users SET password_hash = %s WHERE id = %s",
                (generate_password_hash(password), reset_request["user_id"]),
            )
            execute(
                "UPDATE password_reset_tokens SET used_at = NOW() WHERE id = %s",
                (reset_request["id"],),
            )

            flash("Your password has been reset. You can now log in.", "success")
            return redirect(url_for("login"))

    return render_template("verify_otp.html", error=error, phone=phone)
```

`user["id"] if user else 0` deliberately keeps the error message identical
whether the phone number doesn't exist or the code is simply wrong — an
unregistered number can't be distinguished from a wrong OTP by the
response.

### Table change

`ensure_password_reset_table()` now also strips the old `UNIQUE` constraint
on `token` that made sense for random link tokens but not for a 6-digit
OTP (two users could legitimately be issued the same code at once):

```python
def ensure_password_reset_table():
    execute(
        """
        CREATE TABLE IF NOT EXISTS password_reset_tokens (
            id INT AUTO_INCREMENT PRIMARY KEY,
            user_id INT NOT NULL,
            token VARCHAR(64) NOT NULL,
            expires_at TIMESTAMP NOT NULL,
            used_at TIMESTAMP NULL,
            created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
        )
        """
    )

    # An earlier version of this table (link-based reset) made `token`
    # globally unique. A 6-digit OTP has a much smaller space, so two users
    # could legitimately be issued the same code at the same time -- drop
    # that constraint if it's still there from before.
    existing_indexes = {row["Key_name"] for row in query("SHOW INDEX FROM password_reset_tokens")}
    if "token" in existing_indexes:
        execute("ALTER TABLE password_reset_tokens DROP INDEX token")
```

### Templates

- `forgot_password.html` — phone input instead of username/email, dropped
  the link-display box entirely.
- `verify_otp.html` (new, replaces the deleted `reset_password.html`) —
  read-only phone field (carried through from the redirect), OTP field,
  new password, confirm password.

### Verified against the live database

- Correct OTP → password changes → success flash shows on the login page.
- Wrong OTP → rejected with "invalid or has expired."
- Reusing the same correct OTP a second time → also rejected (single-use).
- An unregistered phone number → generic message, no OTP leaked.

**Side effect:** this testing changed `dilasha`'s password again, to
`anothertest123`.

---

## 5. Fix: broken "Restore" button on Profile for corrupted avatar history

**Problem seen:** the Profile page's "Your previous photos" section showed
a broken-image thumbnail (browser's alt-text placeholder) still paired
with a working "Restore" button. Digging into it, that history row pointed
to `static/uploads/avatars/avatar_29_1789272834292.jpg` — a file that
exists but is only 13 bytes and literally contains the text
`fakejpegbytes`, left over from an earlier upload test rather than a real
photo.

**Fix** in the `profile()` route in `app.py` — filter `avatar_history` down
to rows whose file both exists on disk and is bigger than 512 bytes (a
real JPEG/PNG/WEBP is never that small) before handing it to the template:

```python
avatar_history = query(
    "SELECT id, path, created_at FROM avatar_history WHERE user_id = %s ORDER BY created_at DESC",
    (session["user_id"],),
)

# A history row whose file is missing, or too small to be a real photo
# (a handful of bytes -- a leftover from testing, not an actual upload),
# can't be shown or restored -- skip it instead of rendering a broken
# thumbnail with a Restore button that would just restore garbage.
avatar_history = [
    photo for photo in avatar_history
    if (Path(app.static_folder) / photo["path"]).is_file()
    and (Path(app.static_folder) / photo["path"]).stat().st_size > 512
]

return render_template("profile.html", user=user, avatar_history=avatar_history)
```

This is a code-level filter, not a one-off database delete, so it also
catches any future case where a history row's file goes missing or gets
corrupted. Verified directly against the `orii` account: the fake entry
disappeared, the real current photo still showed correctly.

---

## 6. Day-by-day itineraries for packages and places (MySQL-backed)

**Problem:** packages only had duration/places/activities as unordered
lists (`places_covered`, `activities`, `includes` JSON columns) — no actual
day-by-day plan. Places had none of that at all beyond a free-text
description. All itinerary content lives in MySQL, not hardcoded in a
template or Python literal used at render time (it's only Python literals
*once*, at the moment the table is first created, exactly like this app
already does for package tag/summary/places/activities — see
`backfill_package_details()` in `app.py`).

### New tables

`app.py`, added just before `ensure_group_messages_table()`:

```python
def ensure_itinerary_tables():
    existed = bool(query("SHOW TABLES LIKE 'package_itinerary'"))
    execute(
        """
        CREATE TABLE IF NOT EXISTS package_itinerary (
            id INT AUTO_INCREMENT PRIMARY KEY,
            package_id INT NOT NULL,
            day_number INT NOT NULL,
            title VARCHAR(150) NOT NULL,
            description TEXT NOT NULL,
            UNIQUE KEY uniq_package_day (package_id, day_number),
            FOREIGN KEY (package_id) REFERENCES packages(id) ON DELETE CASCADE
        )
        """
    )
    execute(
        """
        CREATE TABLE IF NOT EXISTS place_itinerary (
            id INT AUTO_INCREMENT PRIMARY KEY,
            place_id INT NOT NULL,
            day_number INT NOT NULL,
            title VARCHAR(150) NOT NULL,
            description TEXT NOT NULL,
            UNIQUE KEY uniq_place_day (place_id, day_number),
            FOREIGN KEY (place_id) REFERENCES explore_places(id) ON DELETE CASCADE
        )
        """
    )
    if not existed:
        backfill_package_itinerary()
        backfill_place_itinerary()
```

`UNIQUE KEY (package_id/place_id, day_number)` means a day can't be
duplicated for the same trip — matches the same defensive-schema instinct
as `wishlist_items`' unique `(user_id, package_id)`.

Wired into both migration lists (the `before_request` hook and the
`__main__` startup block), same as every other `ensure_*` function.

### Seed content (real Nepal itineraries, not filler)

`backfill_package_itinerary()` and `backfill_place_itinerary()` insert one
row per day for every package and place that ships with this app, keyed by
**package name** / **place slug** (so it only ever fills in real rows,
never invents new packages or places — same pattern as
`backfill_package_details()`). Day counts match each package's
`duration_days` exactly, and follow real Everest/Annapurna-region trekking
itineraries where relevant (e.g. the 14-day Everest Base Camp Trek follows
the standard Lukla → Namche → Tengboche → Dingboche → Lobuche → Gorak Shep
→ Kala Patthar → return structure that's actually used on that trek):

| Package | Days seeded |
|---|---|
| Pokhara & Kathmandu Heritage Tour | 5 |
| Chitwan Wildlife Safari | 3 |
| Everest Base Camp Trek | 14 |
| Lumbini Buddhist Pilgrimage | 2 |
| Nagarkot Sunrise Getaway | 2 |
| Bandipur Heritage Weekend | 2 |
| Rara Lake Wilderness Trek | 7 |
| Upper Mustang Jeep Expedition | 8 |
| Janakpur Mithila Culture Tour | 2 |
| Mustang and Manag | 7 |

Places get a shorter **suggested** itinerary rather than their full "ideal
stay" range (e.g. Everest Region's `days` field says "12–16 days" for a
full trek, but its seeded itinerary is a realistic 3-day highlights
version — a taste of the region, not a duplicate of the package):

| Place (slug) | Days seeded |
|---|---|
| kathmandu-valley | 2 |
| pokhara | 3 |
| chitwan | 2 |
| lumbini | 2 |
| everest-region | 3 |
| nagarkot | 1 |
| bandipur | 1 |
| mustang | 3 |
| rara-lake | 3 |
| janakpur | 1 |

Full content for one package, as an example of the shape (`app.py`, inside
`backfill_package_itinerary()`):

```python
"Chitwan Wildlife Safari": [
    ("Arrival in Sauraha", "Transfer into Chitwan, check-in, a short village walk, and an evening Tharu cultural dance performance."),
    ("Jungle safari day", "A morning jeep safari through Chitwan National Park in search of rhinos and wildlife, then an afternoon canoe ride down the Rapti River."),
    ("Nature walk & departure", "A final guided nature walk near the park, breakfast, and transfer back."),
],
```

Each `(title, description)` tuple becomes one row, numbered 1..N by its
position in the list via `enumerate(days, start=1)`.

### Routes

`package_detail()` and `place_detail()` each got one extra query, passed
into their template as `itinerary`:

```python
itinerary = query(
    "SELECT day_number, title, description FROM package_itinerary WHERE package_id = %s ORDER BY day_number",
    (package_id,),
)
```
```python
itinerary = query(
    "SELECT day_number, title, description FROM place_itinerary WHERE place_id = %s ORDER BY day_number",
    (place["id"],),
)
```

### Aesthetic: a vertical timeline

Both `package_detail.html` and `place_detail.html` got the same timeline
CSS (a numbered wine-colored circle per day, connected by a vertical line,
with the day's title and description beside it):

```css
.timeline { position: relative; margin-top: 6px }
.timeline-item { position: relative; display: grid; grid-template-columns: 44px 1fr; gap: 16px; padding-bottom: 26px }
.timeline-item:last-child { padding-bottom: 0 }
.timeline-item:not(:last-child)::before {
  content: ""; position: absolute; left: 21px; top: 44px; bottom: -4px;
  width: 2px; background: var(--line)
}
.timeline-day {
  width: 44px; height: 44px; border-radius: 999px; background: var(--wine); color: #fff;
  display: flex; flex-direction: column; align-items: center; justify-content: center;
  font-family: Georgia,serif; font-weight: 700; line-height: 1; z-index: 1
}
.timeline-day small { font-family: Segoe UI,Arial; font-weight: 400; font-size: 9px; text-transform: uppercase; letter-spacing: .5px; opacity: .85 }
.timeline-body h3 { margin: 6px 0 6px; font-size: 17px; color: var(--ink); font-family: Georgia,serif }
.timeline-body p { margin: 0; color: var(--muted); font-size: 14px; line-height: 1.6 }
```

Markup (identical shape in both templates, just different section title —
"Day-by-day itinerary" on a package, "Suggested itinerary" on a place):

```html
{% if itinerary %}
<section class="panel">
  <h2>Day-by-day itinerary</h2>
  <div class="timeline">
    {% for day in itinerary %}
      <div class="timeline-item">
        <div class="timeline-day">{{ day.day_number }}<small>Day</small></div>
        <div class="timeline-body">
          <h3>{{ day.title }}</h3>
          <p>{{ day.description }}</p>
        </div>
      </div>
    {% endfor %}
  </div>
</section>
{% endif %}
```

Placed right after the photo gallery, before "Places covered" on a
package's page; right after "Why visit," before "Highlights" on a place's
page.

### Verified against the live database

- `SHOW TABLES LIKE '%itinerary%'` confirms both tables exist.
- `SELECT package_id, COUNT(*) FROM package_itinerary GROUP BY package_id`
  returned exactly 5, 3, 14, 2, 2, 2, 7, 8, 2, 7 — matching every package's
  `duration_days` one-for-one.
- `SELECT place_id, COUNT(*) FROM place_itinerary GROUP BY place_id`
  returned 2, 3, 2, 2, 3, 1, 1, 3, 3, 1 for the 10 places.
- Rendered `/packages/3` (Everest Base Camp Trek): 14 timeline entries,
  titles read correctly in order ("Fly to Lukla, trek to Phakding" →
  "Trek to Namche Bazaar" → "Acclimatisation day in Namche" → ...).
- Rendered `/explore_places/rara-lake`: 3 timeline entries, correct order.

### Not built here

- No admin UI to edit itinerary days yet — they're only editable by hand in
  MySQL (or by re-running the backfill after dropping the tables). Adding
  an edit screen under Manage Packages / Manage Places would be the
  natural next step, following the same pattern as the existing photo
  management screens.

---

## 7. Existing feature deep-dive: Cancellation & Refunds

This wasn't built this session — it already existed in the codebase — but
is documented here in full since it was asked for. It's the mechanism that
turns "traveller wants their money back" into a tracked, auditable process
instead of an instant status flip.

### The policy

```python
REFUND_STATUSES = ("Requested", "Approved", "Refund Pending", "Refunded", "Rejected")
OPEN_REFUND_STATUSES = ("Requested", "Approved", "Refund Pending")

# Cancellation policy: a traveller cancelling a paid booking forfeits this
# percentage of what they paid -- the rest is what actually gets requested
# as a refund. Flat and automatic (not admin-adjustable, not tiered by how
# close to the travel date it is).
CANCELLATION_FEE_PERCENT = 20
```

A flat 20% cancellation fee on any paid booking a traveller cancels. Not
tiered by notice period (cancelling the day before costs the same
percentage as cancelling months ahead) and not currently admin-configurable
without editing this constant.

### The table

```python
def ensure_refund_requests_table():
    execute(
        """
        CREATE TABLE IF NOT EXISTS refund_requests (
            id INT AUTO_INCREMENT PRIMARY KEY,
            booking_id INT NOT NULL,
            user_id INT NOT NULL,
            reason TEXT NOT NULL,
            amount DECIMAL(10, 2) NOT NULL,
            status VARCHAR(20) NOT NULL DEFAULT 'Requested',
            admin_notes TEXT NULL,
            refund_reference VARCHAR(150) NULL,
            decided_by INT NULL,
            created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
            FOREIGN KEY (booking_id) REFERENCES bookings(id) ON DELETE CASCADE,
            FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE,
            FOREIGN KEY (decided_by) REFERENCES users(id) ON DELETE SET NULL
        )
        """
    )
    columns = {row["Field"] for row in query("DESCRIBE refund_requests")}
    if "cancellation_fee" not in columns:
        # amount already existed as "how much to refund" -- this new column
        # records what was withheld, purely so it can be shown/audited. It
        # doesn't change what "amount" means to any existing code.
        execute("ALTER TABLE refund_requests ADD COLUMN cancellation_fee DECIMAL(10, 2) NOT NULL DEFAULT 0 AFTER amount")
```

`amount` is what actually gets refunded (price minus the fee); the fee
itself is recorded separately so it can be reported on without recomputing
it from history.

### Helper lookups

```python
def get_open_refund_request(booking_id):
    placeholders = ",".join(["%s"] * len(OPEN_REFUND_STATUSES))
    return query(
        f"SELECT * FROM refund_requests WHERE booking_id = %s AND status IN ({placeholders}) "
        "ORDER BY id DESC LIMIT 1",
        (booking_id, *OPEN_REFUND_STATUSES), fetch="one",
    )


def get_latest_refund_request(booking_id):
    return query(
        "SELECT * FROM refund_requests WHERE booking_id = %s ORDER BY id DESC LIMIT 1",
        (booking_id,), fetch="one",
    )
```

`get_open_refund_request` is the guard used in two places: it stops a
traveller filing a second cancellation request while one is already in
flight, and it stops an admin changing a booking's status directly (via
Manage Bookings) while a refund is mid-process — the refund flow owns the
booking's status once a refund request exists, so the two paths can't
fight over it.

### Traveller side: `cancel_booking()`

One route, but the behaviour branches on the booking's current state —
this is where "cancel" for an unpaid booking and "cancel" for a paid one
diverge:

```python
@app.route("/my_bookings/<int:booking_id>/cancel", methods=["POST"])
def cancel_booking(booking_id):

    if "user_id" not in session or session.get("user") != 0:
        return redirect(url_for("login"))

    booking = query(
        "SELECT id, status, payment_status, package_name, price FROM bookings WHERE id = %s AND user_id = %s",
        (booking_id, session["user_id"]), fetch="one",
    )

    if booking is None:
        flash("Booking not found.", "error")

    elif booking["payment_status"] == "Paid":
        # A paid booking can still be cancelled, but it means giving the
        # traveller their money back -- that goes through a trackable refund
        # request instead of flipping straight to Cancelled.
        if booking["status"] != "Confirmed":
            flash("This booking can no longer be cancelled.", "error")
        elif get_open_refund_request(booking_id):
            flash("You already have a cancellation request in progress for this booking.", "error")
        else:
            reason = request.form.get("reason", "").strip()
            if not reason:
                flash("Please tell us why you'd like to cancel this booking so it can be reviewed.", "error")
            else:
                cancellation_fee = round(float(booking["price"]) * CANCELLATION_FEE_PERCENT / 100, 2)
                refund_amount = round(float(booking["price"]) - cancellation_fee, 2)
                execute(
                    "INSERT INTO refund_requests (booking_id, user_id, reason, amount, cancellation_fee) VALUES (%s, %s, %s, %s, %s)",
                    (booking_id, session["user_id"], reason[:2000], refund_amount, cancellation_fee),
                )
                execute(
                    "UPDATE bookings SET status = 'Cancel Requested' WHERE id = %s AND user_id = %s",
                    (booking_id, session["user_id"]),
                )
                notify_admins(
                    f"{session['username']} requested cancellation and a refund for booking #{booking_id} "
                    f"('{booking['package_name']}').",
                    url_for("admin_refunds"),
                )
                notify(
                    session["user_id"],
                    f"Your cancellation and refund request for booking #{booking_id} has been submitted. "
                    f"A {CANCELLATION_FEE_PERCENT}% cancellation fee applies -- you'll be refunded {format_npr(refund_amount)} of your {format_npr(booking['price'])} payment.",
                    url_for("my_bookings"),
                )
                flash(
                    f"Your cancellation and refund request has been submitted. A {CANCELLATION_FEE_PERCENT}% "
                    f"cancellation fee applies, so {format_npr(refund_amount)} of your {format_npr(booking['price'])} "
                    "payment will be refunded once approved.",
                    "success",
                )

    elif booking["status"] == "Cancelled":
        flash("This booking has already been cancelled.", "error")

    elif booking["status"] == "Cancel Requested":
        flash("You've already requested cancellation for this booking.", "error")

    elif booking["status"] == "Pending":
        # Nothing has been confirmed yet, so the traveller can withdraw the
        # request immediately -- there's nothing for admin to undo.
        execute(
            "UPDATE bookings SET status = 'Cancelled' WHERE id = %s AND user_id = %s",
            (booking_id, session["user_id"]),
        )
        notify_admins(f"{session['username']} cancelled pending booking #{booking_id}.", url_for("admin_bookings"))
        notify(session["user_id"], f"Your booking #{booking_id} has been cancelled.", url_for("my_bookings"))
        flash("Your booking has been cancelled.", "success")

    else:
        execute(
            "UPDATE bookings SET status = 'Cancel Requested' WHERE id = %s AND user_id = %s",
            (booking_id, session["user_id"]),
        )
        notify_admins(f"{session['username']} requested cancellation of booking #{booking_id}.", url_for("admin_bookings"))
        notify(session["user_id"], f"Cancellation of booking #{booking_id} is awaiting admin approval.", url_for("my_bookings"))
        flash("Your cancellation request has been submitted and is awaiting admin approval.", "success")

    return redirect(url_for("my_bookings"))
```

The four paths, plain-language:

| Booking state | What happens |
|---|---|
| Paid & Confirmed | A `refund_requests` row is created (fee auto-calculated), booking flips to `Cancel Requested`, admin is notified. Requires a written reason. |
| Paid but not Confirmed (already Cancelled/Cancel Requested) | Rejected with an explanatory flash — nothing to do. |
| Unpaid & Pending | Cancelled immediately, no admin involvement — nothing was ever confirmed or charged. |
| Unpaid & Confirmed (or any other unpaid state) | Flips to `Cancel Requested`, needs admin approval, but there's no refund request since no money changed hands. |

### Admin side: three routes

**`admin_refunds()`** — the queue. Lists every refund request (joined to
booking + traveller), optionally filtered by status, grouped into sections
by status in a fixed order (`REFUND_STATUSES`), with summary counts and a
running total of fees actually collected:

```python
@app.route("/admin/refunds")
def admin_refunds():
    ...
    sql = """
        SELECT r.id, r.booking_id, r.reason, r.amount, r.cancellation_fee, r.status, r.admin_notes,
               r.refund_reference, r.created_at, r.updated_at,
               b.package_name, b.travel_date, b.price AS booking_price,
               u.full_name AS traveller_name, u.username AS traveller_username
        FROM refund_requests r
        JOIN bookings b ON b.id = r.booking_id
        JOIN users u ON u.id = r.user_id
        WHERE 1=1
    """
    # + optional "AND r.status = %s" filter, ORDER BY r.created_at DESC

    fees_row = query(
        "SELECT COALESCE(SUM(cancellation_fee), 0) AS total, COUNT(*) AS total_count "
        "FROM refund_requests WHERE status = 'Refunded' AND cancellation_fee > 0",
        fetch="one",
    )
    ...
```

**`admin_cancellation_fees()`** — a revenue report, fees actually collected
(only counts `Refunded` rows, since that's the only point money has
genuinely moved), grouped by package with a percentage bar for the
biggest-earning packages:

```python
@app.route("/admin/cancellation-fees")
def admin_cancellation_fees():
    ...
    by_package = query(
        """
        SELECT b.package_name,
               COUNT(*) AS refund_count,
               SUM(r.cancellation_fee) AS total_fee,
               SUM(r.amount) AS total_refunded,
               SUM(b.price) AS total_original
        FROM refund_requests r
        JOIN bookings b ON b.id = r.booking_id
        WHERE r.status = 'Refunded' AND r.cancellation_fee > 0
        GROUP BY b.package_name
        ORDER BY total_fee DESC
        """
    )
    total_fee = sum(row["total_fee"] for row in by_package)
    highest_fee = max((row["total_fee"] for row in by_package), default=0)
    for row in by_package:
        row["bar_percent"] = round((row["total_fee"] / highest_fee) * 100) if highest_fee else 0
        row["share_percent"] = round((row["total_fee"] / total_fee) * 100) if total_fee else 0
    ...
```

**`update_refund_status()`** — the actual decision-maker. This is a state
machine: a request can only move to specific next statuses from its
current one, enforced by a lookup table rather than trusting whatever
status the form posts:

```python
allowed_next = {
    "Requested": ("Approved", "Rejected"),
    "Approved": ("Refund Pending", "Rejected"),
    "Refund Pending": ("Refunded",),
}
```

So the flow is strictly: **Requested → Approved → Refund Pending →
Refunded** (with a Reject exit available from the first two stages, but
never from `Refund Pending` — once the payment is actually being
processed, there's no going back to Rejected). Two steps have extra
requirements before they're allowed through:

- Marking **Refunded** requires `refund_reference` (the payment provider's
  actual reference number) — you can't close out a refund without proof it
  happened.
- Marking **Rejected** requires `admin_notes` — a rejection always needs a
  stated reason, since it's shown to the traveller.

```python
if refund is None:
    flash("Refund request not found.", "error")
elif status not in allowed_next.get(refund["status"], ()):
    flash("That status change isn't valid from the request's current status.", "error")
elif status == "Refunded" and not refund_reference:
    flash("Please record the payment provider's refund reference before marking this as refunded.", "error")
elif status == "Rejected" and not admin_notes:
    flash("Please explain why the refund request is being rejected.", "error")
else:
    execute(
        """
        UPDATE refund_requests
        SET status = %s, admin_notes = %s, refund_reference = %s, decided_by = %s
        WHERE id = %s
        """,
        (status, admin_notes or refund["admin_notes"], refund_reference or refund["refund_reference"],
         session["user_id"], refund_id),
    )

    if status == "Refunded":
        execute(
            "UPDATE bookings SET status = 'Cancelled', payment_status = 'Refunded' WHERE id = %s",
            (refund["booking_id"],),
        )
        notify(refund["user_id"], f"Your refund for booking #{refund['booking_id']} has been completed.", url_for("my_bookings"))
    elif status == "Rejected":
        execute("UPDATE bookings SET status = 'Confirmed' WHERE id = %s", (refund["booking_id"],))
        notify(refund["user_id"], f"Your cancellation request for booking #{refund['booking_id']} was declined: {admin_notes}", url_for("my_bookings"))
    else:
        notify(refund["user_id"], f"Your refund request for booking #{refund['booking_id']} is now '{status}'.", url_for("my_bookings"))

    log_admin_activity(
        "refund.status_updated", "refund_request", refund_id, refund["package_name"],
        old_value=refund["status"], new_value=status, notes=admin_notes or None,
    )
```

Two side effects worth noting: a **Rejected** decision actually restores
the booking to `Confirmed` (the traveller keeps their trip, they just don't
get the refund/cancellation), while a **Refunded** decision cancels the
booking *and* marks the payment refunded in one move. Every decision is
logged to `admin_activity_log` via `log_admin_activity`.

### Where the templates plug in

- `templates/my_bookings.html` — the cancel button/refund-request form
  (shown conditionally depending on the booking's paid/status combination),
  plus a `refund-status` box showing whatever the latest refund request's
  status is, with different styling per status (`refund-refunded`,
  `refund-rejected`).
- `templates/admin_refunds.html` — one form per allowed next-status button
  (Approve / Reject / Mark Refund Pending / Mark Refunded), each posting to
  `update_refund_status` with a hidden `status` field, plus the required
  `admin_notes`/`refund_reference` inputs where the backend demands them.
- `templates/admin_cancellation_fees.html` — the per-package fee report,
  reading `by_package`, `total_fee`, `total_refunds` from the route above.
- `templates/admin_bookings.html` — shows a badge (via the `has_open_refund`
  subquery in `admin_bookings()`) when a booking has an open refund, and
  `update_booking_status()` refuses a direct status change while that's
  true: *"This booking has an open refund request. Resolve it from Manage
  Refunds first."*

---

## 8. Roadmap (for supervisor discussion)

A snapshot of where the app stands and what's realistically next, grouped
by effort/impact rather than by when it was built.

### Shipped so far

- Core booking flow: browse packages/places → book → admin confirms → pay
  (eSewa or Stripe) → travel → **auto-marked Completed** once the trip date
  passes (page-load triggered, not a real scheduler — see below).
- Cancellation & refund workflow with a flat 20% fee, full admin
  approval chain, and a fee/revenue report (detailed above).
- Travel groups with join requests and group chat.
- Admin activity log across bookings, packages, refunds, and travel groups.
- Traveller-facing: wishlist, downloadable PDF e-ticket for paid bookings,
  phone-OTP password recovery, day-by-day itineraries for every package
  and place.

### Near-term (small, high-value)

- **Admin itinerary editor** — itinerary days currently only exist as
  seeded MySQL rows; there's no screen to add/edit/reorder them. Natural
  extension of the existing Manage Packages / Manage Places photo screens.
- **Real refund/reset delivery** — wire an actual SMS provider (Twilio,
  Sparrow SMS) for the OTP flow and/or an SMTP provider for email
  notifications; both currently only work by showing content on-page.
- **`requirements.txt`** — the project has no dependency manifest at all,
  which makes it hard to hand off or deploy reliably.
- **Package capacity / availability** — booking overlap is currently only
  checked against a single traveller's own bookings, not against how many
  seats a package or a specific date actually has left.

### Mid-term

- **Reviews & ratings** — a natural pairing with the `Completed` status:
  prompt a traveller to rate a package once their trip is marked done,
  surface the average on the package page and in Reports.
- **Real-time booking completion** — replace the page-load-triggered
  `auto_complete_past_bookings()` with an `APScheduler` job (or an OS-level
  cron hitting a dedicated route) so trips complete on schedule even with
  no visitors.
- **Coupon/discount codes** at booking time.
- **Admin analytics charts** — Reports is currently table-based; charts
  (revenue over time, bookings by package, refund rate) would make trends
  easier to spot at a glance.
- **Waitlists** for travel groups that are full.

### Longer-term / infrastructure

- **Automated tests** — `test_places.py` exists but the booking, refund,
  and payment flows have no test coverage; a regression here (e.g. the
  refund state machine) would currently only be caught by manual testing.
- **Security review** — this app handles payments (Stripe/eSewa) and PII
  (phone, email); worth a formal pass before wider release, especially
  around the payment webhook and the still-page-shown OTP/reset codes.
- **Tiered or configurable cancellation policy** — right now the 20% fee
  is a single hardcoded constant with no notice-period tiering and no
  admin control; if the business wants a "cancel further out, pay less
  fee" model, that needs its own design pass.
- **Deployment/production readiness** — currently runs via
  `python app.py` with Flask's debug reloader; production would need a
  real WSGI server, environment-based config, and the `requirements.txt`
  above.
