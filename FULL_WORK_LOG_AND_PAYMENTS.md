# Full Work Log + How Payments Work — Explained Simply

This file has two parts:

- **Part 1** — everything I built today, in the order I built it, with the
  actual code and a plain-language explanation of every piece.
- **Part 2** — the eSewa and Stripe payment system. **I did not write this
  today — it already existed in your app before I started.** You asked for
  it to be explained too, so it's here in the same style: real code, line
  numbers, plain language.

Every line number below refers to `app.py` as it stands right now. Nothing
in the actual code was changed to write this document — this is only an
explanation of what's already there.

---

# PART 1 — What I Built Today

I built 4 things, in this order:

1. Cancellation & refund requests
2. Admin activity history
3. Photo galleries
4. A show/hide "Total Revenue" box on the admin dashboard

---

## 1. Cancellation & Refund Requests

### The problem this solves

If a traveller had **already paid** for a booking and wanted to cancel it,
the old code just showed a message saying "contact support" — there was no
actual button, and no way to track what happened next.

### The new idea, in plain words

- A traveller with a paid booking can now type a reason and send a
  cancellation request.
- That request moves through stages, one at a time:

  ```
  Requested  →  Approved  →  Refund Pending  →  Refunded
      ↓              ↓
  Rejected      Rejected
  ```

- The traveller gets a message at every stage.
- The booking itself only changes in two places: it becomes `Cancel
  Requested` the moment the traveller asks, and it becomes `Cancelled`
  (with payment marked `Refunded`) only once the money has actually been
  sent back. Everything in between (Approved, Refund Pending) is tracked on
  the request itself, not on the booking, so the two can never disagree with
  each other.

### Step 1 — A new place to store refund requests

**Lines 276–283** — a comment explaining why this is needed, plus two lists
of allowed status words:

```python
# CANCELLATION & REFUND REQUESTS
# A Pending or unpaid Confirmed booking can still be cancelled directly (see
# cancel_booking below), but once a booking has actually been paid for,
# cancelling it means giving the traveller their money back -- that needs a
# trackable request/decision/refund trail instead of an instant status flip.
REFUND_STATUSES = ("Requested", "Approved", "Refund Pending", "Refunded", "Rejected")
OPEN_REFUND_STATUSES = ("Requested", "Approved", "Refund Pending")
```

**What this does:** `REFUND_STATUSES` is the full list of stages a refund
request can be in. `OPEN_REFUND_STATUSES` is the smaller list of stages that
mean "this is still being worked on" (as opposed to `Refunded` or
`Rejected`, which are finished). The code checks against these lists all
over the place instead of typing the words out every time, so a typo can't
sneak in.

**Lines 285–305** — creates the actual database table the first time the
app runs:

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
```

**What this does, in plain words:** this is like creating a new spreadsheet
called `refund_requests`, with one row per cancellation request. Each row
remembers:
- which booking it's about (`booking_id`) and which traveller asked
  (`user_id`)
- the traveller's typed reason
- how much money is involved (`amount`)
- what stage it's at right now (`status`, starts as `Requested`)
- any note the admin leaves (e.g. why it was rejected) — `admin_notes`
- the payment provider's reference number once refunded —
  `refund_reference`
- which admin made the decision — `decided_by`
- when it was created and last changed

`CREATE TABLE IF NOT EXISTS` means: only create it the first time; if it's
already there, do nothing (so this is safe to run every time the app
starts).

**Lines 308–321** — two small helper functions used everywhere else:

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

**What this does:** `get_open_refund_request` answers the question "does
this booking already have a cancellation request that's still in progress?"
— used to stop someone from filing two requests for the same booking.
`get_latest_refund_request` just fetches whatever the most recent request
was, in progress or not.

### Step 2 — Letting a traveller actually ask for a refund

This is inside the existing `cancel_booking` function
(**lines 1284–1353**), which already handled *unpaid* cancellations. I
added a new branch for *paid* ones. Here's the exact new part
(**lines 1298–1325**):

```python
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
                execute(
                    "INSERT INTO refund_requests (booking_id, user_id, reason, amount) VALUES (%s, %s, %s, %s)",
                    (booking_id, session["user_id"], reason[:2000], booking["price"]),
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
                notify(session["user_id"], f"Your cancellation and refund request for booking #{booking_id} has been submitted.", url_for("my_bookings"))
                flash("Your cancellation and refund request has been submitted and is awaiting admin review.", "success")
```

**Line-by-line, in plain words:**
- *"if the booking isn't Confirmed"* → don't allow it (a booking that's
  already Completed or Cancelled shouldn't be cancelled again).
- *"if there's already an open request"* → don't allow a second one; tell
  the traveller one is already in progress.
- *"if no reason was typed"* → refuse and ask for one.
- Otherwise: **save the request** into the new table, **flip the booking**
  to `Cancel Requested` so admins see it needs attention, **message every
  admin** and **message the traveller**, then show a success message.

### Step 3 — Showing the traveller their refund's progress

In `my_bookings` (**lines 1242–1281**), the database question (the "query")
that fetches a traveller's bookings was changed to also pull in the *latest*
refund request for each booking (**lines 1251–1266**):

```python
    bookings = query(
        """
        SELECT b.id, b.package_name, b.travel_date, b.travellers, b.phone, b.price, b.status,
               b.payment_status, b.created_at,
               r.status AS refund_status, r.reason AS refund_reason,
               r.admin_notes AS refund_admin_notes, r.refund_reference AS refund_reference,
               r.updated_at AS refund_updated_at
        FROM bookings b
        LEFT JOIN refund_requests r ON r.id = (
            SELECT id FROM refund_requests WHERE booking_id = b.id ORDER BY id DESC LIMIT 1
        )
        WHERE b.user_id = %s
        ORDER BY b.created_at DESC
        """,
        (session["user_id"],),
    )
```

**In plain words:** for every booking, also attach "and if this booking has
a refund request, grab its status, its reason, any admin note, and the
refund reference number too." That's what `LEFT JOIN` means here — "attach
this extra information if it exists, but still show the booking even if it
doesn't."

The template `templates/my_bookings.html` was then updated so:
- A paid, Confirmed booking with **no** refund request shows a "Request
  cancellation & refund" box with a text field for the reason.
- Any booking that **does** have a refund request shows its current stage,
  and (if relevant) the rejection reason or the refund reference number.

### Step 4 — Admins reviewing and deciding refund requests

**New page** — `admin_refunds()`, **lines 2498–2546**:

```python
@app.route("/admin/refunds")
def admin_refunds():

    if "username" not in session:
        return redirect(url_for("login"))
    if session.get("user") != 1:
        return redirect(url_for("dashboard"))

    status_filter = request.args.get("status", "")

    sql = """
        SELECT r.id, r.booking_id, r.reason, r.amount, r.status, r.admin_notes,
               r.refund_reference, r.created_at, r.updated_at,
               b.package_name, b.travel_date,
               u.full_name AS traveller_name, u.username AS traveller_username
        FROM refund_requests r
        JOIN bookings b ON b.id = r.booking_id
        JOIN users u ON u.id = r.user_id
        WHERE 1=1
    """
    params = []
    if status_filter:
        sql += " AND r.status = %s"
        params.append(status_filter)
    sql += " ORDER BY r.created_at DESC"

    refund_requests = query(sql, tuple(params))

    status_rows = query("SELECT status, COUNT(*) AS total FROM refund_requests GROUP BY status")
    status_counts = {row["status"]: row["total"] for row in status_rows}

    grouped_refunds = []
    for status_name in REFUND_STATUSES:
        matches = [r for r in refund_requests if r["status"] == status_name]
        if matches:
            grouped_refunds.append((status_name, matches))

    return render_template(
        "admin_refunds.html",
        grouped_refunds=grouped_refunds,
        refund_requests=refund_requests,
        selected_status=status_filter,
        statuses=REFUND_STATUSES,
        requested_count=status_counts.get("Requested", 0),
        approved_count=status_counts.get("Approved", 0),
        pending_count=status_counts.get("Refund Pending", 0),
        refunded_count=status_counts.get("Refunded", 0),
        rejected_count=status_counts.get("Rejected", 0),
    )
```

**In plain words:** only an admin can open this page (the two `if` checks at
the top). It pulls every refund request, joined with the booking and
traveller details so the page can show a name and a package instead of just
numbers, optionally filtered to one status. It then groups them by status
(Requested first, then Approved, and so on) and works out the little counts
shown at the top of the page (e.g. "3 Requested, 1 Approved...").

**The actual decision-making** — `update_refund_status()`,
**lines 2549–2612**:

```python
@app.route("/admin/refunds/<int:refund_id>/update-status", methods=["POST"])
def update_refund_status(refund_id):

    if "username" not in session or session.get("user") != 1:
        return redirect(url_for("login"))

    status = request.form.get("status", "")
    admin_notes = request.form.get("admin_notes", "").strip()
    refund_reference = request.form.get("refund_reference", "").strip()
    status_filter = request.form.get("status_filter", "")

    refund = query(
        "SELECT r.*, b.package_name FROM refund_requests r JOIN bookings b ON b.id = r.booking_id WHERE r.id = %s",
        (refund_id,), fetch="one",
    )

    # Each step can only follow the one before it, so a request can't jump
    # straight from Requested to Refunded (or be re-decided once it's already
    # in a final state) -- the transition itself is the source of truth for
    # what admin action is happening, not the target status alone.
    allowed_next = {
        "Requested": ("Approved", "Rejected"),
        "Approved": ("Refund Pending", "Rejected"),
        "Refund Pending": ("Refunded",),
    }

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
        flash("Refund request updated.", "success")

    return redirect(url_for("admin_refunds", status=status_filter))
```

**In plain words, step by step:**
1. Only an admin can call this.
2. It reads what the admin typed/clicked (which new status, any note, any
   reference number).
3. `allowed_next` is the rulebook: from `Requested` you can only go to
   `Approved` or `Rejected` — never straight to `Refunded`. This stops an
   admin (or a stray click) from skipping steps.
4. If the admin is trying to mark something `Refunded` without typing a
   reference number, or `Rejected` without a reason — it refuses and asks
   for it.
5. Otherwise, it saves the new status onto the request.
6. **If the new status is `Refunded`**: the actual booking is updated too —
   its status becomes `Cancelled` and its payment status becomes
   `Refunded`. The traveller is told the refund is complete.
7. **If the new status is `Rejected`**: the booking goes back to
   `Confirmed` (because the cancellation didn't happen after all). The
   traveller is told why, using the admin's note.
8. **Any other step** (Approved, Refund Pending): just tells the traveller
   things have moved forward.
9. Finally, it writes one line into the new admin activity log (Part 2 of
   this document explains that log) so there's a record of who made this
   decision and when.

### Step 5 — Stopping the two admin pages from fighting each other

The booking list page (`admin_bookings`) already had its own "Approve
cancellation / Keep booking" buttons for the *old* unpaid-cancellation flow.
Those buttons directly changed a booking's status. I had to make sure an
admin couldn't use those same old buttons on a *paid* booking that now has
its own refund request in progress — otherwise the booking's status and the
refund request's status could end up disagreeing.

**In `admin_bookings()`, lines 2390–2402**, the database question was
changed to also check, for every booking, whether it has an open refund
request:

```python
    open_refund_placeholders = ",".join(["%s"] * len(OPEN_REFUND_STATUSES))
    sql = f"""
        SELECT b.id, b.package_name, b.travel_date, b.travellers, b.phone,
               b.price, b.status, b.payment_status, b.created_at,
               u.full_name AS traveller_name, u.username AS traveller_username,
               EXISTS(
                   SELECT 1 FROM refund_requests r
                   WHERE r.booking_id = b.id AND r.status IN ({open_refund_placeholders})
               ) AS has_open_refund
        FROM bookings b
        JOIN users u ON u.id = b.user_id
        WHERE 1=1
    """
```

**In plain words:** `EXISTS(...)` is a yes/no check — "does this booking
have any refund request that's still open?" That answer (`has_open_refund`)
is then used in the webpage itself to show a link to Manage Refunds instead
of the old buttons.

**And as a backup, in `update_booking_status()`, lines 2458–2493**, even if
someone tried to submit that old form directly, the server itself blocks
it:

```python
    elif get_open_refund_request(booking_id):

        # This booking's cancellation is a paid refund in progress -- change
        # its status from the refund request instead, so the booking status
        # and the refund's own status can't drift apart.
        flash("This booking has an open refund request. Resolve it from Manage Refunds first.", "error")
```

**In plain words:** this is a safety net. Even if someone tried to bypass
the webpage and send the old "change status" request directly, the server
checks again and refuses, telling them to go through Manage Refunds
instead.

### New page for admins: `templates/admin_refunds.html`

This is a brand new page (didn't exist before). It shows:
- Five little counter boxes at the top (Requested / Approved / Refund
  Pending / Refunded / Rejected)
- A dropdown to filter by one status
- Every request, grouped by its stage, each with the traveller's reason
  and the right buttons for whatever can happen next (Approve/Reject, Start
  refund/Reject, or Mark refunded with a reference number box)

---

## 2. Admin Activity History

### The problem this solves

Before, if a booking's status changed, or a package got edited, or a travel
group got approved — nothing recorded *who* did it or *when*. If two admins
shared the account, or something looked wrong later, there was no way to
check.

### The new idea, in plain words

Every time an admin changes something important, one line gets written down
automatically: which admin, what they changed, what it changed from and to,
and when. A new **Activity** page lets you read that list, and filter it.

### Step 1 — A place to store the history

**Lines 240–258**:

```python
def ensure_admin_activity_table():
    execute(
        """
        CREATE TABLE IF NOT EXISTS admin_activity_log (
            id INT AUTO_INCREMENT PRIMARY KEY,
            admin_id INT NULL,
            admin_username VARCHAR(100) NOT NULL,
            action VARCHAR(100) NOT NULL,
            entity_type VARCHAR(50) NOT NULL,
            entity_id VARCHAR(50) NOT NULL,
            entity_label VARCHAR(255) NULL,
            old_value VARCHAR(255) NULL,
            new_value VARCHAR(255) NULL,
            notes TEXT NULL,
            created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (admin_id) REFERENCES users(id) ON DELETE SET NULL
        )
        """
    )
```

**In plain words:** another new "spreadsheet," one row per admin action.
Each row records: which admin (`admin_id` and, as a backup,
`admin_username` — explained below), what kind of thing changed
(`entity_type`, e.g. `"booking"` or `"package"`), which specific one
(`entity_id`), a readable name for it (`entity_label`, e.g. the package's
name), what it was before (`old_value`) and what it became (`new_value`),
and an optional note.

### Step 2 — The one function that writes to it

**Lines 261–273**:

```python
def log_admin_activity(action, entity_type, entity_id, entity_label=None, old_value=None, new_value=None, notes=None):
    """Record an admin change. admin_username is snapshotted (rather than only
    joined at read time) so the history still reads sensibly if that admin
    account is later edited or removed."""
    execute(
        """
        INSERT INTO admin_activity_log
            (admin_id, admin_username, action, entity_type, entity_id, entity_label, old_value, new_value, notes)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
        """,
        (session.get("user_id"), session.get("username", "unknown"), action, entity_type,
         str(entity_id), entity_label, old_value, new_value, notes),
    )
```

**In plain words:** this is the one function every other part of the app
calls to "write down what just happened." It automatically knows which
admin is doing it (from `session`, i.e. who's currently logged in) and
saves their username as text right into the log row — so even if that
admin's account is later renamed or deleted, the history still reads
correctly (it doesn't rely on looking the name up again later).

### Step 3 — Where it gets called from

I added a call to `log_admin_activity(...)` inside every admin action that
changes something. Here's the full list, with the exact wording used for
each:

| Where (function) | What triggers it | What gets logged |
|---|---|---|
| `update_booking_status` (line 2486) | Admin changes a booking's status | `"booking.status_updated"` |
| `update_refund_status` (line 2606) | Admin decides a refund request | `"refund.status_updated"` |
| `update_group_status` | Admin approves/rejects a travel group | `"travel_group.status_updated"` |
| `update_group_communication` | Admin changes a group's chat mode | `"travel_group.communication_updated"` |
| `update_group_member_status` | Admin approves/rejects someone joining a group | `"group_member.status_updated"` |
| `admin_packages` (add), `update_package`, `delete_package` | Admin adds/edits/removes a package | `"package.created"` / `"package.updated"` / `"package.deleted"` |
| `add_package_photo`, `delete_package_photo` | Admin adds/removes a package photo | `"package.photo_added"` / `"package.photo_removed"` |
| `manage_places` (add/edit/delete) | Admin adds/edits/removes a destination | `"place.created"` / `"place.updated"` / `"place.deleted"` |
| `add_place_photo`, `delete_place_photo` | Admin adds/removes a destination photo | `"place.photo_added"` / `"place.photo_removed"` |

Example — this is the real line from `update_booking_status`
(**lines 2486–2489**):

```python
            log_admin_activity(
                "booking.status_updated", "booking", booking_id, booking["package_name"],
                old_value=booking["status"], new_value=status,
            )
```

**In plain words:** "Write down that this admin did a
`booking.status_updated` action, on a `booking`, specifically the one with
this ID, whose name/label is the package's name, and it changed from
whatever it was before to whatever it is now."

### Step 4 — The page to actually view the history

**Lines 2684–2716**:

```python
ACTIVITY_ENTITY_TYPES = ("booking", "refund_request", "package", "travel_group", "group_member", "place")


@app.route("/admin/activity")
def admin_activity():
    if "username" not in session:
        return redirect(url_for("login"))
    if session.get("user") != 1:
        return redirect(url_for("dashboard"))

    entity_type = request.args.get("entity_type", "")
    admin_search = request.args.get("admin", "").strip()

    sql = "SELECT * FROM admin_activity_log WHERE 1=1"
    params = []
    if entity_type:
        sql += " AND entity_type = %s"
        params.append(entity_type)
    if admin_search:
        sql += " AND admin_username LIKE %s"
        params.append(f"%{admin_search}%")
    sql += " ORDER BY created_at DESC, id DESC LIMIT 300"

    entries = query(sql, tuple(params))

    return render_template(
        "admin_activity.html",
        entries=entries,
        entity_types=ACTIVITY_ENTITY_TYPES,
        selected_entity_type=entity_type,
        admin_search=admin_search,
        total=query("SELECT COUNT(*) AS total FROM admin_activity_log", fetch="one")["total"],
    )
```

**In plain words:** only an admin can open this. It reads the log, most
recent first, and optionally narrows it down to one type of thing (e.g.
only package changes) or to actions by one particular admin (by typing part
of their username). It only ever shows the most recent 300 rows at once, so
the page doesn't get overwhelming.

### New page: `templates/admin_activity.html`

A simple table: **When / Admin / Area / Action / What changed**, with a
filter box above it. New sidebar link called **Activity**.

---

## 3. Photo Galleries

### The problem this solves

Packages had **no pictures at all**. Destinations had exactly **one** cover
photo and no way to show more.

### The new idea, in plain words

An admin can now attach several photos (up to 12) to any package or
destination, each with an optional caption, picked from the images that are
already sitting in your `static` folder (the same folder your website's
existing images live in) — no complicated file uploading involved. Those
photos then show up as a small gallery on that package's or destination's
page.

### Step 1 — Finding out which images are available to pick from

**Lines 832–840**:

```python
STATIC_IMAGE_EXTENSIONS = (".jpg", ".jpeg", ".png", ".webp")


def list_static_images():
    """Photo pickers (destination cover photos, package/place gallery photos)
    all choose from images already sitting in static/ rather than accepting
    uploads, so there's one place-name whitelist to validate against."""
    return sorted(path.name for path in Path(app.static_folder).iterdir()
                  if path.is_file() and path.suffix.lower() in STATIC_IMAGE_EXTENSIONS)
```

**In plain words:** this looks inside your `static` folder and returns a
list of every image file's name (only real image files — `.jpg`, `.jpeg`,
`.png`, `.webp` — nothing else). This same list is used both for the
existing "choose a cover photo" dropdown (which already existed for
destinations) and for the new gallery photo pickers, so there's only one
place that decides what counts as a valid image.

### Step 2 — Two new tables to store the photos

**Lines 329–355**:

```python
def ensure_photo_tables():
    execute(
        """
        CREATE TABLE IF NOT EXISTS package_photos (
            id INT AUTO_INCREMENT PRIMARY KEY,
            package_id INT NOT NULL,
            image VARCHAR(255) NOT NULL,
            caption VARCHAR(200) NULL,
            sort_order INT NOT NULL DEFAULT 0,
            created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (package_id) REFERENCES packages(id) ON DELETE CASCADE
        )
        """
    )
    execute(
        """
        CREATE TABLE IF NOT EXISTS place_photos (
            id INT AUTO_INCREMENT PRIMARY KEY,
            place_id INT NOT NULL,
            image VARCHAR(255) NOT NULL,
            caption VARCHAR(200) NULL,
            sort_order INT NOT NULL DEFAULT 0,
            created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (place_id) REFERENCES explore_places(id) ON DELETE CASCADE
        )
        """
    )
```

**In plain words:** two more "spreadsheets" — one row per photo. Each row
just says which package/destination it belongs to, which image file it is,
an optional caption, and what order it should appear in.

### Step 3 — Adding and removing package photos

**Lines 2323–2368**:

```python
MAX_PHOTOS_PER_ITEM = 12


@app.route("/admin/packages/<int:package_id>/photos/add", methods=["POST"])
def add_package_photo(package_id):

    if "username" not in session or session.get("user") != 1:
        return redirect(url_for("login"))

    package = query("SELECT name FROM packages WHERE id = %s", (package_id,), fetch="one")
    image = request.form.get("image", "")
    caption = request.form.get("caption", "").strip()[:200]
    photo_count = query(
        "SELECT COUNT(*) AS total FROM package_photos WHERE package_id = %s", (package_id,), fetch="one"
    )["total"]

    if package is None:
        flash("Package not found.", "error")
    elif image not in list_static_images():
        flash("Please choose a photo from the available images.", "error")
    elif photo_count >= MAX_PHOTOS_PER_ITEM:
        flash(f"A package can have at most {MAX_PHOTOS_PER_ITEM} photos.", "error")
    else:
        execute(
            "INSERT INTO package_photos (package_id, image, caption, sort_order) VALUES (%s, %s, %s, %s)",
            (package_id, image, caption or None, photo_count),
        )
        log_admin_activity("package.photo_added", "package", package_id, package["name"], new_value=image)
        flash("Photo added to the gallery.", "success")

    return redirect(url_for("admin_packages"))


@app.route("/admin/packages/<int:package_id>/photos/<int:photo_id>/delete", methods=["POST"])
def delete_package_photo(package_id, photo_id):

    if "username" not in session or session.get("user") != 1:
        return redirect(url_for("login"))

    package = query("SELECT name FROM packages WHERE id = %s", (package_id,), fetch="one")
    execute("DELETE FROM package_photos WHERE id = %s AND package_id = %s", (photo_id, package_id))
    if package:
        log_admin_activity("package.photo_removed", "package", package_id, package["name"])
    flash("Photo removed.", "success")

    return redirect(url_for("admin_packages"))
```

**In plain words:**
- **Adding:** only an admin can do this. It checks the package is real,
  that the chosen image is actually one of the allowed files (so someone
  can't type in a made-up filename), and that this package doesn't already
  have 12 photos. If all good, it saves the photo and writes a line to the
  activity log.
- **Deleting:** only an admin can do this. It just removes that one photo
  row and logs it.

The exact same pattern (add/delete, with the same checks) exists for
destinations, at **lines 919–964** (`add_place_photo` /
`delete_place_photo`) — the only difference is destinations are looked up
by their web address slug (like `bandipur`) instead of a plain number ID.

### Step 4 — Showing the photos to travellers

In `package_detail()` (**lines 1031–1050**), after fetching the package
itself, this was added:

```python
    photos = query(
        "SELECT id, image, caption FROM package_photos WHERE package_id = %s ORDER BY sort_order, id",
        (package_id,),
    )
    return render_template("package_detail.html", package=package, photos=photos)
```

And the same thing for `place_detail()` (**lines 1085–1107**):

```python
    photos = query(
        "SELECT id, image, caption FROM place_photos WHERE place_id = %s ORDER BY sort_order, id",
        (place["id"],),
    )
    return render_template(
        "place_detail.html", place=place, similar_places=similar_places, photos=photos
    )
```

**In plain words:** whenever a traveller opens a package or destination
page, the app now also fetches that item's photo list, and hands it to the
page. The page then displays them in a small gallery grid — but only if
there are any (an empty gallery section doesn't show up at all).

### New/changed admin pages

- **Manage Packages** — each package's "Edit details" section now also has
  a "Photo gallery" section: thumbnails with a Remove button, and a
  dropdown + caption field to add a new one.
- **Manage Places** — the exact same kind of section was added to each
  destination card.

---

## 4. "Total Revenue" box on the Admin Dashboard (show/hide)

You asked for a total money amount to appear next to "Welcome, admin," that
stays hidden until you click an eye icon.

### Working out the number

In `admin_dashboard()`, **lines 739–741**:

```python
    total_revenue = query(
        "SELECT COALESCE(SUM(price), 0) AS total FROM bookings WHERE payment_status = 'Paid'", fetch="one"
    )["total"]
```

**In plain words:** "add up the price of every booking that has actually
been paid for." `COALESCE(..., 0)` just means "if there are no paid
bookings at all, use 0 instead of an empty/blank result."

This number is then passed into the page as `total_revenue=total_revenue`
when the template is rendered.

### The hide/show box itself

In `templates/admin_dashboard.html`, the "Welcome, admin" section now looks
like this (simplified to the important part):

```html
<div class="revenue-box">
  <div class="revenue-label">Total Revenue Collected</div>
  <div class="revenue-row">
    <span class="revenue-amount" id="revenueAmount" data-value="{{ total_revenue|npr }}">••••••</span>
    <button type="button" class="eye-btn" id="revenueToggle" aria-pressed="false" aria-label="Show total revenue">
      <!-- open-eye icon and closed-eye icon here -->
    </button>
  </div>
</div>
```

**In plain words:** the real amount is quietly stored in `data-value`
(hidden in the page's code, not shown to the eye) while the visible text
starts as dots (`••••••`). A small bit of JavaScript then does the swap:

```javascript
document.getElementById('revenueToggle').addEventListener('click', function () {
  var amountEl = document.getElementById('revenueAmount');
  var eyeOpen = document.getElementById('eyeOpenIcon');
  var eyeClosed = document.getElementById('eyeClosedIcon');
  var isHidden = amountEl.textContent.indexOf('•') !== -1;
  if (isHidden) {
    amountEl.textContent = amountEl.dataset.value;   // show the real number
    eyeOpen.hidden = true;
    eyeClosed.hidden = false;
  } else {
    amountEl.textContent = '••••••';                 // hide it again
    eyeOpen.hidden = false;
    eyeClosed.hidden = true;
  }
});
```

**In plain words:** every time you click the eye button, it checks "is the
dotted version currently showing?" — if yes, swap in the real number and
switch to the "eye open" picture; if no, put the dots back and switch to
the "eye closed" picture. The box starts hidden every time the page loads,
so the real figure is never shown by accident.

---

# PART 2 — How Payments Already Work (eSewa + Stripe)

**Important: none of this was written today.** This code already existed in
your app before I started this conversation. You asked me to also explain
it in the same style, so here it is.

## The big picture, in plain words

Your app supports two ways to pay for a booking:

- **eSewa** — a popular Nepali payment app. The traveller is sent to
  eSewa's own website to pay in Nepali Rupees (NPR), then eSewa sends them
  back.
- **Stripe** — a card payment system used worldwide. The traveller pays by
  card in US Dollars (USD), since Stripe doesn't take NPR directly.

Either way, a payment is only offered **after an admin has already
confirmed the booking** — so nobody can pay for a booking that hasn't been
approved yet. This is explained right in the code's own comment
(**lines 1356–1359**):

```python
# PAYMENTS (eSewa + Stripe)
# Payment is only offered once admin has confirmed a booking -- that's the
# existing fraud gate (see admin_bookings / update_booking_status above),
# and payment shouldn't be collectible on a request that hasn't been vetted.
```

## Setting things up: the configuration

**Lines 50–68**:

```python
# PAYMENT GATEWAY CONFIG
# eSewa: sandbox/test merchant credentials as defaults so the flow works
# out of the box in dev -- swap for real merchant credentials via env vars
# before going live.
ESEWA_PRODUCT_CODE = os.environ.get("ESEWA_PRODUCT_CODE", "EPAYTEST")
ESEWA_SECRET_KEY = os.environ.get("ESEWA_SECRET_KEY", "8gBm/:&EnhH.1/q")
ESEWA_FORM_URL = os.environ.get("ESEWA_FORM_URL", "https://rc-epay.esewa.com.np/api/epay/main/v2/form")
ESEWA_STATUS_URL = os.environ.get("ESEWA_STATUS_URL", "https://rc.esewa.com.np/api/epay/transaction/status/")

# Stripe: no default keys -- payment routes below fail loudly instead of
# silently calling Stripe with an empty key if these aren't set.
STRIPE_SECRET_KEY = os.environ.get("STRIPE_SECRET_KEY", "")
STRIPE_WEBHOOK_SECRET = os.environ.get("STRIPE_WEBHOOK_SECRET", "")
stripe.api_key = STRIPE_SECRET_KEY

# Packages are priced in NPR; Stripe is charged in USD, so bookings paid by
# card are converted at this rate (override via env instead of editing code
# when the rate moves).
NPR_PER_USD = float(os.environ.get("NPR_PER_USD", "133"))
```

**In plain words:**
- `os.environ.get("NAME", "default")` means: "look for a setting called
  `NAME` in the server's environment (a `.env` file, typically); if it's
  not set, fall back to this default value."
- For **eSewa**, the defaults point at eSewa's own free **test/sandbox**
  system — safe for trying things out, not real money — with a shared
  test key (`8gBm/:&EnhH.1/q`) that eSewa publishes for exactly this
  purpose. To take real payments, someone would set real values for these
  through environment variables (not by editing this code).
- For **Stripe**, there is **no** default key at all — on purpose. If
  nobody sets one, `stripe.api_key` ends up empty, and any attempt to
  actually charge a card will fail loudly with an error, rather than
  quietly pretending to work.
- `NPR_PER_USD` is the exchange rate used to convert a price from Nepali
  Rupees into US cents for Stripe, e.g. "133 rupees = 1 dollar."

**Line 71–72** — the actual currency conversion:

```python
def npr_to_usd_cents(npr_amount):
    return int(round((float(npr_amount) / NPR_PER_USD) * 100))
```

**In plain words:** take a price in rupees, divide by the exchange rate to
get dollars, multiply by 100 to turn dollars into cents (Stripe always
wants amounts in the smallest currency unit — cents, not dollars), and
round it to a whole number.

**Lines 75–77** — a signature helper used only for eSewa:

```python
def esewa_sign(message, secret_key):
    digest = hmac.new(secret_key.encode("utf-8"), message.encode("utf-8"), hashlib.sha256).digest()
    return base64.b64encode(digest).decode("utf-8")
```

**In plain words:** eSewa requires every payment request (and every reply
it sends back) to be "signed" — a scrambled fingerprint made from the
payment details plus a secret key that only your server and eSewa know.
This function creates that fingerprint. It's used both to sign outgoing
payment requests and to double-check incoming ones weren't tampered with.

## Making sure the database can track payments

**Lines 180–190**:

```python
def ensure_payment_columns():
    """Add the columns needed to track eSewa/Stripe payments on a booking."""
    columns = {row["Field"] for row in query("DESCRIBE bookings")}
    if "payment_status" not in columns:
        execute("ALTER TABLE bookings ADD COLUMN payment_status VARCHAR(20) NOT NULL DEFAULT 'Unpaid'")
    if "payment_method" not in columns:
        execute("ALTER TABLE bookings ADD COLUMN payment_method VARCHAR(20) NULL")
    if "transaction_uuid" not in columns:
        execute("ALTER TABLE bookings ADD COLUMN transaction_uuid VARCHAR(100) NULL")
    if "stripe_session_id" not in columns:
        execute("ALTER TABLE bookings ADD COLUMN stripe_session_id VARCHAR(255) NULL")
```

**In plain words:** this checks whether the `bookings` table already has
the extra columns needed to track a payment (`payment_status`,
`payment_method`, `transaction_uuid` for eSewa, `stripe_session_id` for
Stripe) and adds any that are missing. This runs automatically, so an older
database gets "upgraded" the first time the app starts against it.

## Checking a booking is actually allowed to be paid for

**Lines 1362–1369**:

```python
def get_payable_booking(booking_id):
    booking = query(
        "SELECT id, user_id, package_name, price, status, payment_status FROM bookings WHERE id = %s AND user_id = %s",
        (booking_id, session["user_id"]), fetch="one",
    )
    if not booking or booking["status"] != "Confirmed" or booking["payment_status"] == "Paid":
        return None
    return booking
```

**In plain words:** this is a guard used by both payment methods. It looks
up the booking, but **only if it belongs to whoever is currently logged
in** (`AND user_id = %s`) — so nobody can pay for someone else's booking by
guessing an ID in the address bar. Then it checks the booking is
`Confirmed` (an admin approved it) and hasn't already been `Paid`. If any
of that fails, it returns nothing, and the calling code treats that as "not
allowed."

## The eSewa payment journey

### Step A — Sending the traveller to eSewa

**Lines 1372–1407**:

```python
@app.route("/pay/esewa/<int:booking_id>")
def pay_with_esewa(booking_id):
    if "user_id" not in session:
        return redirect(url_for("login"))

    booking = get_payable_booking(booking_id)
    if booking is None:
        abort(404)

    transaction_uuid = f"gogather-{booking_id}-{int(time.time())}"
    message = (
        f"total_amount={booking['price']},"
        f"transaction_uuid={transaction_uuid},"
        f"product_code={ESEWA_PRODUCT_CODE}"
    )

    fields = {
        "amount": booking["price"],
        "tax_amount": 0,
        "total_amount": booking["price"],
        "transaction_uuid": transaction_uuid,
        "product_code": ESEWA_PRODUCT_CODE,
        "product_service_charge": 0,
        "product_delivery_charge": 0,
        "success_url": url_for("esewa_success", _external=True),
        "failure_url": url_for("esewa_failure", _external=True),
        "signed_field_names": "total_amount,transaction_uuid,product_code",
        "signature": esewa_sign(message, ESEWA_SECRET_KEY),
    }

    execute(
        "UPDATE bookings SET transaction_uuid = %s, payment_method = 'esewa' WHERE id = %s",
        (transaction_uuid, booking_id),
    )

    return render_template("esewa_redirect.html", form_url=ESEWA_FORM_URL, fields=fields)
```

**In plain words, step by step:**
1. Must be logged in.
2. `get_payable_booking` makes sure this booking is really theirs, is
   confirmed, and isn't already paid — otherwise it's a dead end (404).
3. A unique transaction ID is generated
   (`gogather-<booking id>-<current time>`) so this specific payment attempt
   can be told apart from any other.
4. A "message" string is built out of the amount, the transaction ID, and
   eSewa's product code, and it's run through `esewa_sign` (explained
   above) to produce a signature — proof that this request really came from
   your server and wasn't tampered with.
5. A dictionary called `fields` is built — this is exactly what eSewa's
   payment form needs: the amount, the transaction ID, where to send the
   traveller back to after success or failure, which fields were signed,
   and the signature itself.
6. The booking is updated to remember its transaction ID and that it's
   being paid via eSewa.
7. Finally, it shows a small page (`esewa_redirect.html`) — see below —
   that automatically submits those fields straight to eSewa's real payment
   page.

**`templates/esewa_redirect.html`** (the whole file):

```html
<!doctype html>
<html>
<head>
  <meta charset="utf-8">
  <title>Redirecting to eSewa…</title>
</head>
<body>
  <p>Redirecting you to eSewa to complete your payment…</p>
  <form id="esewaForm" method="POST" action="{{ form_url }}">
    {% for name, value in fields.items() %}
      <input type="hidden" name="{{ name }}" value="{{ value }}">
    {% endfor %}
  </form>
  <script>document.getElementById("esewaForm").submit();</script>
</body>
</html>
```

**In plain words:** this page doesn't really show anything to look at — it
builds an invisible form filled in with all those payment fields, pointed
at eSewa's real web address, and the one line of JavaScript at the bottom
submits that form automatically the instant the page loads. To the
traveller, it just looks like "we're sending you to eSewa now."

### Step B — eSewa sends the traveller back

**Lines 1410–1456**:

```python
@app.route("/esewa/success")
def esewa_success():
    if "user_id" not in session:
        return redirect(url_for("login"))

    try:
        payload = json.loads(base64.b64decode(request.args.get("data", "")))
    except (ValueError, TypeError):
        flash("Payment could not be verified.", "error")
        return redirect(url_for("my_bookings"))

    # eSewa signs a different field set on the way back than on the way out
    # (it includes transaction_code, status, and signed_field_names itself),
    # and lists exactly which fields + order in signed_field_names -- so the
    # message to verify must be rebuilt from that, not the 3 outbound fields.
    signed_fields = payload.get("signed_field_names", "").split(",")
    expected_message = ",".join(f"{field}={payload.get(field)}" for field in signed_fields)

    # Never trust the redirect at face value -- recompute the signature
    # ourselves before treating this as a real payment result.
    if not signed_fields or esewa_sign(expected_message, ESEWA_SECRET_KEY) != payload.get("signature"):
        flash("Payment could not be verified.", "error")
        return redirect(url_for("my_bookings"))

    status = requests.get(
        ESEWA_STATUS_URL,
        params={
            "product_code": payload.get("product_code"),
            "total_amount": payload.get("total_amount"),
            "transaction_uuid": payload.get("transaction_uuid"),
        },
        timeout=10,
    ).json()

    booking = query(
        "SELECT id, user_id, package_name FROM bookings WHERE transaction_uuid = %s",
        (payload.get("transaction_uuid"),), fetch="one",
    )

    if status.get("status") == "COMPLETE" and booking:
        execute("UPDATE bookings SET payment_status = 'Paid' WHERE id = %s", (booking["id"],))
        notify(booking["user_id"], f"Payment received for booking #{booking['id']} ({booking['package_name']}).", url_for("my_bookings"))
        flash("Payment successful!", "success")
    else:
        flash("Payment was not completed.", "error")

    return redirect(url_for("my_bookings"))
```

**In plain words, step by step:**
1. eSewa redirects the traveller's browser back to this address, with a
   chunk of scrambled-looking text in the web address (`?data=...`). That
   text is actually a "base64" encoded (scrambled-but-reversible) bundle of
   information, so it's decoded and read as data first.
2. If that decoding fails for any reason, the app doesn't trust it — it
   just says "couldn't verify" and sends the traveller back to their
   bookings.
3. **The important security check:** eSewa also sends back its own
   signature. The code rebuilds the exact same kind of fingerprint eSewa
   would have made (using `esewa_sign`, the same function from before) and
   compares it to what eSewa sent. If they don't match, someone might be
   trying to fake a successful payment by messing with the web address —
   so it's rejected.
4. Even after the signature checks out, the code doesn't just take eSewa's
   word for it from the address bar alone — it makes a **second, direct
   check** by calling eSewa's own status-checking web address
   (`ESEWA_STATUS_URL`) and asking, "for this specific transaction, what
   really happened?" This is the safest way to confirm a payment, because
   it's not relying on anything that passed through the traveller's
   browser, which could theoretically be faked.
5. It looks up which booking this transaction belongs to.
6. **Only if eSewa's own answer says `"COMPLETE"`** does the booking get
   marked `Paid`, and the traveller gets a notification and a success
   message. Otherwise, it's treated as failed/incomplete.

**Lines 1459–1462** — if the traveller cancels or something goes wrong on
eSewa's side, eSewa sends them here instead:

```python
@app.route("/esewa/failure")
def esewa_failure():
    flash("Payment was cancelled or failed.", "error")
    return redirect(url_for("my_bookings"))
```

**In plain words:** just shows a "didn't work" message and sends them back.

## The Stripe payment journey

### Step A — Starting a Stripe checkout

**Lines 1465–1497**:

```python
@app.route("/pay/stripe/<int:booking_id>")
def pay_with_stripe(booking_id):
    if "user_id" not in session:
        return redirect(url_for("login"))

    booking = get_payable_booking(booking_id)
    if booking is None:
        abort(404)

    traveller = query("SELECT email FROM users WHERE id = %s", (session["user_id"],), fetch="one")

    checkout_session = stripe.checkout.Session.create(
        mode="payment",
        line_items=[{
            "price_data": {
                "currency": "usd",
                "product_data": {"name": booking["package_name"]},
                "unit_amount": npr_to_usd_cents(booking["price"]),
            },
            "quantity": 1,
        }],
        success_url=url_for("stripe_success", _external=True) + "?session_id={CHECKOUT_SESSION_ID}",
        cancel_url=url_for("my_bookings", _external=True),
        metadata={"booking_id": str(booking_id)},
        customer_email=traveller["email"],
    )

    execute(
        "UPDATE bookings SET stripe_session_id = %s, payment_method = 'stripe' WHERE id = %s",
        (checkout_session.id, booking_id),
    )

    return redirect(checkout_session.url, code=303)
```

**In plain words, step by step:**
1. Must be logged in, and the same `get_payable_booking` safety check as
   before applies.
2. It looks up the traveller's email, so Stripe's payment page can be
   pre-filled with it and Stripe can send its own receipt.
3. `stripe.checkout.Session.create(...)` asks Stripe itself, over the
   internet, to set up a ready-made payment page for this exact purchase:
   one item (the package name), priced in US cents (converted using
   `npr_to_usd_cents` from earlier), in US dollars. It also tells Stripe
   where to send the traveller after paying (`success_url`) or if they back
   out (`cancel_url`), and quietly attaches the booking's ID as
   `metadata` — a little tag Stripe will hand back later so the app knows
   which booking this payment was for.
4. Stripe replies with a "checkout session" — basically a ready-made
   payment page with its own unique ID and web address.
5. The booking is updated to remember that session's ID and that it's being
   paid via Stripe.
6. The traveller's browser is redirected straight to Stripe's own hosted
   payment page (`checkout_session.url`) — this is a page on Stripe's
   website, not yours, where they type in their card details.

### Step B — After paying, the traveller lands back on your site

**Lines 1500–1506**:

```python
@app.route("/stripe/success")
def stripe_success():
    # Display-only. Stripe can land a browser here without a completed
    # payment (tab closed early, etc.) -- the webhook below is the only
    # place payment_status is actually flipped to 'Paid'.
    flash("Payment received -- confirming now.", "success")
    return redirect(url_for("my_bookings"))
```

**In plain words:** this page is shown to the traveller right after they
finish on Stripe's site, but — importantly — **it does not, by itself, mark
anything as paid.** It's only a friendly "thanks, confirming now" message.
The comment explains exactly why: a browser can land on this page even if
the payment didn't really finish properly (e.g. the tab got closed at just
the wrong moment), so this page can't be trusted as proof of payment.

### Step C — The real proof: Stripe's webhook

**Lines 1509–1530**:

```python
@app.route("/webhooks/stripe", methods=["POST"])
def stripe_webhook():
    payload = request.get_data()
    sig_header = request.headers.get("Stripe-Signature", "")

    try:
        event = stripe.Webhook.construct_event(payload, sig_header, STRIPE_WEBHOOK_SECRET)
    except (ValueError, stripe.error.SignatureVerificationError):
        return "", 400

    if event["type"] == "checkout.session.completed":
        session_obj = event["data"]["object"]
        booking_id = session_obj["metadata"]["booking_id"]
        booking = query(
            "SELECT id, user_id, package_name FROM bookings WHERE id = %s AND stripe_session_id = %s",
            (booking_id, session_obj["id"]), fetch="one",
        )
        if booking:
            execute("UPDATE bookings SET payment_status = 'Paid' WHERE id = %s", (booking["id"],))
            notify(booking["user_id"], f"Payment received for booking #{booking['id']} ({booking['package_name']}).", url_for("my_bookings"))

    return "", 200
```

**In plain words:** a "webhook" is Stripe's own server reaching out
directly to your server (not through the traveller's browser at all) to
say "hey, here's something that really happened." This is the trustworthy
source of truth for whether a payment actually succeeded.

1. It reads the raw message Stripe sent, plus a signature Stripe attaches
   to every webhook it sends.
2. `stripe.Webhook.construct_event(...)` checks that signature using your
   secret key (`STRIPE_WEBHOOK_SECRET`) — proving this message really came
   from Stripe, and not from someone pretending to be Stripe. If that check
   fails, the request is rejected immediately (`400` means "bad request").
3. If the event is specifically a `"checkout.session.completed"` (meaning:
   "someone finished paying"), it pulls the booking ID back out of the
   `metadata` tag that was attached back in Step A, and double-checks the
   booking really matches that Stripe session ID.
4. Only then does it actually mark the booking `Paid` and notify the
   traveller.
5. It always answers with `"", 200` (meaning "OK, got it") at the end — this
   tells Stripe not to bother retrying the message.

## Why two different "confirmations" exist (Stripe's own success page vs.
the webhook)

This is worth calling out because it's a subtle but important design
choice already in your app: the page the traveller sees
(`/stripe/success`) is just for their benefit ("nice message, keep them
informed"), while the *only* place that actually flips a booking to `Paid`
for Stripe is the webhook. That way, even if a traveller's browser crashes,
loses signal, or they close the tab right after paying, the booking still
gets marked `Paid` correctly once Stripe's webhook arrives — it doesn't
depend on the traveller's browser successfully making it back to your
website at all.

eSewa doesn't have a webhook system in the same way, which is why its own
`esewa_success` page does the double-check itself (re-verifying the
signature, then asking eSewa's status API directly) instead of trusting a
separate background message.

---

## Where the "Pay" buttons come from

In `templates/my_bookings.html`, once a booking is `Confirmed` and not yet
paid, the traveller sees two buttons:

```html
<a class="pay pay-esewa" href="{{ url_for('pay_with_esewa', booking_id=booking.id) }}">Pay with eSewa (NPR)</a>
<a class="pay pay-stripe" href="{{ url_for('pay_with_stripe', booking_id=booking.id) }}">Pay with Card (USD)</a>
```

**In plain words:** these are just plain links pointing at the two routes
explained above (`/pay/esewa/<id>` and `/pay/stripe/<id>`) — clicking either
one kicks off the matching payment journey.

---

## A note on something unrelated I noticed

While testing today's changes, I found that one of your existing test
files (`test_reports.py`) currently fails — but this has nothing to do with
anything in this document. It's caused by some other in-progress work on
`payment_status` that was already sitting in your project files before I
started today (I could see it in `admin_reports()`, and there are also some
`payment_integration_*.txt` notes files in your project that suggest this
was mid-way through). I didn't touch that code and didn't fix that test,
since it's outside everything described above — just flagging it so it
doesn't get confused with today's work.
