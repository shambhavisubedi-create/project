# Complete Guide — Roadmap, Theory & Code for Every Feature

This file explains, for each feature you asked me to build, **three things
in order**:

1. **Roadmap** — exactly what you (via `FEATURE_ROADMAP.md`) asked for.
2. **Theory** — the general idea/concept behind *why* it's built the way it
   is, explained like a lesson, not just "here's what I typed."
3. **Code** — the actual code, with line numbers from `app.py`, and a
   plain-language explanation under every block.

At the end there's an appendix explaining the eSewa/Stripe payment system
(that one already existed before I started — it's included because you
asked, but it isn't one of "the features I told you," so it's kept
separate).

No code was changed to write this document. Line numbers refer to `app.py`
as it stands right now.

---

# Feature 1: Cancellation & Refund Requests

## 1.1 — Roadmap (what you asked for)

From `FEATURE_ROADMAP.md`, item 7:

> **Cancellation and refund requests** — Let someone with a paid booking
> submit a request and track the decision and refund progress. *Why it
> helps:* Replaces the current instruction to contact support with a
> trackable process. **Priority: High. Effort: Large.**

The roadmap's later "Phase 4" section adds more detail about *how* this
should work:

> - Define the cancellation deadline, approval rules, and possible refund
>   amounts before building the screens.
> - Add a paid-booking cancellation request form and admin decision page.
> - Show separate states for request submitted, approved, refund pending,
>   refunded, and rejected.
> - Start with admin-handled refunds and record the payment provider
>   reference.
> - **Done when:** A traveller can track a request, an admin can record its
>   resolution, and a booking is only shown as refunded after the refund is
>   confirmed. Repeated actions must not issue a second refund.

Everything below was built to satisfy that "Done when" checklist exactly.

## 1.2 — Theory: why it's built this way

**The core idea is a *state machine*.** A state machine is just a fancy
name for something very ordinary: a thing that can only be in one of a
fixed set of "states" at a time, and can only move between certain states
in a certain order — never randomly jumping around. You already use these
in real life: a food delivery order goes *Placed → Preparing → Out for
delivery → Delivered*, never backwards, never skipping a step. A refund
request works the same way here:

```
Requested → Approved → Refund Pending → Refunded
    ↓            ↓
Rejected     Rejected
```

**Why not just add a `refund_status` column onto the `bookings` table
itself, instead of a whole new table?** This is a genuinely important
design decision, and the reasoning is:

- A booking's job is to answer one question: *"is this trip happening or
  not?"* (Pending / Confirmed / Completed / Cancelled).
- A refund's job is to answer a *different* question: *"has the money been
  given back, and if not, what stage is that at?"*
- If you tried to cram both questions into one column, you'd end up needing
  combinations like `"Confirmed-RefundPending"`, `"Confirmed-Approved"`,
  `"Cancelled-Refunded"` — the list of combinations multiplies fast, and
  it becomes very easy to accidentally create a combination that makes no
  sense (like `"Pending-Refunded"` — refunding money for a booking that was
  never even confirmed).
- By keeping them as **two separate things, linked together**, each one
  stays simple and easy to reason about on its own. The only extra work
  this needs is a *guard* — a rule that stops one from being changed
  without going through the other correctly (explained in section 1.3,
  "Step 5" below) — which is a small price for a much cleaner design.

**Why require proof before certain steps?** Two rules exist:
- You can't mark something **Refunded** without typing in the payment
  provider's reference number.
- You can't **Reject** a request without typing a reason.

This is a general pattern called **requiring evidence for a state
transition**. Without it, a tired admin could accidentally click "Refunded"
on the wrong row, and now your records say money was returned when it
wasn't — a serious problem when real money is involved. Forcing a small
piece of proof at the riskiest steps costs the admin a few seconds but
prevents an entire category of costly mistakes.

**Why block a second request while one is already open?** This avoids what
programmers call a *race condition* or *duplicate action* problem — if a
traveller could file five refund requests for the same booking, an admin
might approve two of them by mistake, and now the traveller could be
refunded twice for one booking. Checking "is there already an open one?"
before accepting a new request closes that door.

## 1.3 — Code

### Step 1: A place to store the requests, and the state machine's vocabulary

**Lines 276–283:**

```python
# CANCELLATION & REFUND REQUESTS
# A Pending or unpaid Confirmed booking can still be cancelled directly (see
# cancel_booking below), but once a booking has actually been paid for,
# cancelling it means giving the traveller their money back -- that needs a
# trackable request/decision/refund trail instead of an instant status flip.
REFUND_STATUSES = ("Requested", "Approved", "Refund Pending", "Refunded", "Rejected")
OPEN_REFUND_STATUSES = ("Requested", "Approved", "Refund Pending")
```

*What this does:* `REFUND_STATUSES` is every stage that exists.
`OPEN_REFUND_STATUSES` is the subset that means "still being worked on"
(as opposed to `Refunded`/`Rejected`, which are finished). Every later
check reads from these two lists instead of typing the words out fresh
each time, so a typo like `"Refuned"` can't silently break something.

**Lines 285–305** — the actual database table:

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

*What this does:* creates a new database table (think: a new spreadsheet),
one row per refund request. `CREATE TABLE IF NOT EXISTS` means "only build
it the first time — if it already exists, do nothing," which is why this
is safe to run every time the app starts. Each row remembers: which
booking and traveller it's about, their typed reason, the amount involved,
its current stage (`status`), any admin note, the refund's reference
number once paid back, which admin decided it, and timestamps for created
and last-updated. The three `FOREIGN KEY` lines are the database's way of
enforcing "this booking_id must point at a real booking" (and similarly for
the two users) — it physically can't be filled in with a made-up ID.

**Lines 308–321** — two small lookup helpers used everywhere else:

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

*What this does:* `get_open_refund_request` answers "does this booking
already have an unfinished request?" — this is the duplicate-request guard
from the theory section. `get_latest_refund_request` just grabs whatever
the newest request is, finished or not (used for display purposes).

### Step 2: Letting a traveller file the request

Inside the existing `cancel_booking` function (**lines 1284–1353**), which
already handled cancelling *unpaid* bookings, I added a new branch
specifically for *paid* ones (**lines 1298–1325**):

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

*Line by line:*
- If the booking isn't currently `Confirmed` (e.g. it's already Completed
  or Cancelled), refuse — cancelling something twice makes no sense.
- If there's already an open request (using the helper from Step 1),
  refuse and explain one is already in progress.
- If no reason was typed in, refuse and ask for one.
- Otherwise: save a new row into `refund_requests`, flip the booking to
  `Cancel Requested` (so it shows up as needing attention), message every
  admin, message the traveller, and show a success message.

### Step 3: Showing the traveller their request's progress

In `my_bookings` (**lines 1242–1281**), the database question was changed
to also pull in whatever the *latest* refund request is for each booking
(**lines 1251–1266**):

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

*What this does:* `LEFT JOIN` is a database instruction meaning "attach
this extra information if it exists, but still show the row even if it
doesn't." So every booking is shown normally, and *if* it happens to have a
refund request, its status/reason/notes/reference come along for free in
the same result — no second trip to the database needed.

The template `templates/my_bookings.html` uses this to show a "Request
cancellation & refund" box (with a reason field) on paid bookings with no
open request, or the current stage/notes/reference for ones that do.

### Step 4: The admin's decision-making page

**`admin_refunds()`, lines 2498–2546** — the page that lists every request:

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

*What this does:* only an admin can open this page. It fetches every
refund request, `JOIN`ed with the booking and traveller so the page can
show a real name and package instead of bare numbers, optionally narrowed
to one status. It groups them by stage (`Requested` first, then `Approved`,
and so on) and works out the little summary counters shown at the top.

**`update_refund_status()`, lines 2549–2612** — where a decision actually
gets made. This is the heart of the state machine:

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

*Line by line:*
1. Only an admin can call this.
2. `allowed_next` is the state machine's rulebook, written directly as a
   dictionary: "from `Requested`, you may only go to `Approved` or
   `Rejected`." This one dictionary is the entire enforcement of the
   picture drawn back in section 1.2 — it's checked with
   `status not in allowed_next.get(refund["status"], ())`, meaning "look up
   what's allowed from the current status; if the requested new status
   isn't in that list, refuse."
3. The two "evidence required" rules from the theory section: no
   `Refunded` without a reference number, no `Rejected` without a reason.
4. If everything checks out, the request row itself is updated.
5. **Only if the new status is `Refunded`** does the actual booking change
   — to `Cancelled`, with `payment_status` set to `Refunded`. This is the
   *only* place in the whole app where a booking becomes "Refunded."
6. **Only if the new status is `Rejected`** does the booking go back to
   `Confirmed` — because the cancellation isn't happening after all.
7. Every decision gets written to the admin activity log (Feature 2 below)
   and the traveller is notified either way.

### Step 5: Keeping the old booking-status page from clashing with this new one

The existing booking list page already had "Approve cancellation / Keep
booking" buttons for unpaid cancellations, which directly changed a
booking's status. Those buttons had to be stopped from touching a booking
that now has its own refund request in progress — otherwise the booking
and the refund could end up telling two different stories.

**In `admin_bookings()`, lines 2390–2402**, the query now also works out,
for every booking, whether it has an open refund attached:

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

*What this does:* `EXISTS(...)` is a database yes/no check — "is there any
row in `refund_requests` for this booking with an open status?" That
true/false answer (`has_open_refund`) is then used in the webpage to swap
in a "go to Manage Refunds" link instead of the old buttons.

**And as a backstop, in `update_booking_status()`, lines 2458–2493**, even
a direct form submission is blocked on the server itself:

```python
    elif get_open_refund_request(booking_id):

        # This booking's cancellation is a paid refund in progress -- change
        # its status from the refund request instead, so the booking status
        # and the refund's own status can't drift apart.
        flash("This booking has an open refund request. Resolve it from Manage Refunds first.", "error")
```

*Theory note:* this is a defensive-programming principle called
**"don't trust the front-end alone."** The webpage already hides the old
buttons, but a determined user (or a bug) could still send that exact same
request directly. So the server checks the rule again itself, independent
of whatever the webpage happened to show. Never rely on the user interface
being the *only* thing enforcing a rule.

### New page: `templates/admin_refunds.html`

A brand-new page: five little counters at the top (one per stage), a status
filter, and every request listed with exactly the button(s) that make sense
for its current stage — matching the `allowed_next` rulebook above so the
webpage and the server logic never disagree about what's possible next.

---

# Feature 2: Admin Activity History

## 2.1 — Roadmap (what you asked for)

From `FEATURE_ROADMAP.md`, item 8:

> **Admin activity history** — Record which admin changed a booking,
> package, or group, and when. *Why it helps:* The team can understand and
> investigate changes. **Priority: High. Effort: Medium.**

Phase 3 of the roadmap adds:

> - Record admin actions, including the previous value, new value, admin,
>   and time.
> - **Done when:** ... important admin changes have a history...

## 2.2 — Theory: why it's built this way

This is a classic pattern called an **audit log** (sometimes "audit
trail"). You'll find the same idea in banking systems, hospital record
systems, and version control (like Git itself, which is really just a very
sophisticated audit log of every change to a codebase). The rules that
make an audit log actually useful are:

1. **It's append-only.** You only ever add new rows; you never edit or
   delete old ones. If you could edit history, it stops being trustworthy
   as history.
2. **It records the "who," not just the "what."** A log that says "the
   price changed" is much less useful than one that says "admin `sarah`
   changed the price on 12 Sept at 3:04pm."
3. **It snapshots details at the time**, instead of only storing a
   reference to look up later. This is the reasoning behind one specific
   design choice here: the log stores the admin's **username as plain
   text** (`admin_username`), not just their `id`. If it only stored the
   `id` and looked the username up fresh every time you viewed the log,
   then renaming or deleting that admin's account later would silently
   rewrite history — every old log entry would suddenly show a different
   name, or no name at all. Storing the username as it was *at the time*
   keeps the history honest, permanently.
4. **A general shape, not one table per thing.** Instead of building a
   separate `booking_history` table, a separate `package_history` table,
   and so on, there's *one* table with generic columns: `entity_type` (what
   kind of thing — `"booking"`, `"package"`, etc.), `entity_id` (which one),
   and `entity_label` (a human-readable name for it, since IDs alone aren't
   meaningful to a person reading the log). This is a common trade-off:
   you give up strict database-level guarantees (like "entity_id must
   really exist," which a `FOREIGN KEY` would give you) in exchange for
   never having to build a brand-new table every time a new kind of
   admin action is added later. For an audit log — which is meant to
   outlive the things it describes anyway (an entry should still make
   sense even after the package it refers to was deleted) — this trade-off
   is the right one.
5. **Recording both the old and new value.** Just knowing "the status
   changed" isn't enough to investigate anything — you need to know it went
   *from* `Pending` *to* `Confirmed`, specifically, to actually understand
   what happened.

## 2.3 — Code

### Step 1: The table itself

**Lines 240–258:**

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

*What this does:* builds the log table described in the theory above. Note
`admin_id` is allowed to be `NULL` and uses `ON DELETE SET NULL` — meaning
if that admin's account is ever deleted, this column just becomes empty
rather than deleting the log entry itself (which would defeat the whole
point). The `admin_username` text survives regardless, exactly as
explained in point 3 of the theory.

### Step 2: The one function everything else calls

**Lines 261–273:**

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

*What this does:* this is the single "write it down" function. It reads
`session.get(...)` to automatically know which admin is currently logged
in — nothing calling this function ever needs to say *who* did it, only
*what* happened, which keeps every call site short and consistent.

### Step 3: Every place it's actually called from

I added one call to this function inside every admin action that changes
something meaningful:

| Function | Line | What it logs |
|---|---|---|
| `update_booking_status` | 2486 | `"booking.status_updated"` |
| `update_refund_status` | 2606 | `"refund.status_updated"` |
| `update_group_status` | — | `"travel_group.status_updated"` |
| `update_group_communication` | — | `"travel_group.communication_updated"` |
| `update_group_member_status` | — | `"group_member.status_updated"` |
| `admin_packages` / `update_package` / `delete_package` | — | `"package.created"` / `"package.updated"` / `"package.deleted"` |
| `add_package_photo` / `delete_package_photo` | 2350 / 2365 | `"package.photo_added"` / `"package.photo_removed"` |
| `manage_places` (add/edit/delete) | 895 / 871 | `"place.created"` / `"place.updated"` / `"place.deleted"` |
| `add_place_photo` / `delete_place_photo` | 944 / 961 | `"place.photo_added"` / `"place.photo_removed"` |

Real example, from `update_booking_status` (**lines 2486–2489**):

```python
            log_admin_activity(
                "booking.status_updated", "booking", booking_id, booking["package_name"],
                old_value=booking["status"], new_value=status,
            )
```

*In plain words:* "Write down: this admin did a `booking.status_updated`
action, on entity type `booking`, entity id = this booking's ID, labelled
with the package's name so a person reading the log recognizes it, and it
changed from `booking["status"]` to `status`."

### Step 4: The page to actually read the history

**Lines 2684–2716:**

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

*What this does:* only an admin can see this. It reads the log newest
first, optionally filtered to one `entity_type` (e.g. only package
changes) or to one admin's username. The `LIMIT 300` is a deliberate
safety cap — an audit log can grow forever, and no one wants a webpage
trying to render ten thousand rows at once.

### New page: `templates/admin_activity.html`

A filterable table: **When / Admin / Area / Action / What changed**, with
a new **Activity** link added to the admin sidebar.

---

# Feature 3: Photo Galleries

## 3.1 — Roadmap (what you asked for)

From `FEATURE_ROADMAP.md`, item 9:

> **Photo galleries** — Show several photos for each destination and
> package. *Why it helps:* Travellers get a clearer idea of the
> experience. **Priority: Medium. Effort: Medium.**

Phase 5 adds:

> - Add destination and package photo galleries.
> - **Done when:** Admins can manage multiple photos... travellers can view
>   them comfortably on a phone...

## 3.2 — Theory: why it's built this way

**One-to-many relationships.** A single package can have *many* photos,
but each photo belongs to exactly *one* package. This is one of the most
common shapes in database design, called a **one-to-many relationship**,
and the standard way to represent it is: don't try to cram multiple photos
into one column of the `packages` table (which would be messy and hard to
search) — instead, make a *separate* table (`package_photos`) where each
row is one photo, and each row simply *points back* at its package using a
`package_id` column. This is the same reason `bookings` is its own table
instead of being crammed into `users`, and it's called **normalization** —
it keeps every table's rows uniform (fixed number of columns, one type of
thing per row) instead of some rows having 1 photo's worth of data and
others having 12.

**Why pick from existing images instead of allowing file uploads?** This
is a security and simplicity trade-off, made consistent with a choice your
app had *already* made for destination cover photos before I touched
anything. Accepting file uploads sounds more flexible, but it opens up a
long list of problems that have to be defended against:
- Someone could upload a very large file and fill up your disk.
- Someone could upload a file that *looks* like an image but is secretly a
  script, hoping the server runs it.
- You need to constantly manage disk space as old images pile up.

By instead saying "pick one of the images that are already sitting in the
`static` folder," none of that risk exists — there's nothing new being
written to the server by a random visitor, only a *choice* from a list an
admin controls. The trade-off is that adding a genuinely new photo means
first placing the image file into the `static` folder yourself (or asking
your developer to) — less convenient, but much safer, and appropriate for
a project at this stage.

**Why a `sort_order` column?** Databases don't promise to hand rows back in
any particular order unless you explicitly ask them to. If galleries just
displayed photos "in whatever order the database happens to store them,"
that order could silently shuffle around later. `sort_order` is a plain
number saved on each photo specifically so the display order is under your
control and stays stable.

**Why cap it at 12 photos?** A pure usability decision — a gallery with
hundreds of photos stops being a helpful preview and starts being a wall to
scroll through. A modest cap keeps the feature doing its job (giving a
*taste* of the experience) without needing anything more complicated like
pagination.

## 3.3 — Code

### Step 1: Knowing which images are even allowed to be picked

**Lines 832–840:**

```python
STATIC_IMAGE_EXTENSIONS = (".jpg", ".jpeg", ".png", ".webp")


def list_static_images():
    """Photo pickers (destination cover photos, package/place gallery photos)
    all choose from images already sitting in static/ rather than accepting
    uploads, so there's one place-name whitelist to validate against."""
    return sorted(path.name for path in Path(app.static_folder).iterdir()
                  if path.is_file() and path.suffix.lower() in STATIC_IMAGE_EXTENSIONS)
```

*What this does:* looks inside your `static` folder right now, and returns
the name of every actual image file in it (only files ending in `.jpg`,
`.jpeg`, `.png`, or `.webp` — nothing else counts). This one function is
now shared by *both* the pre-existing "destination cover photo" picker and
the two new gallery pickers, so there's exactly one place in the whole app
that decides "what counts as a valid image" — a good practice called
**avoiding duplicated logic** (if this rule ever needs to change, e.g. to
allow `.gif`, there's only one line to update).

### Step 2: Two new tables — one per "many" side of the relationship

**Lines 329–355:**

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

*What this does:* exactly the one-to-many table shape described in the
theory. `ON DELETE CASCADE` is worth calling out: it means "if the package
(or destination) this photo belongs to is ever deleted, automatically
delete its photos too" — so you can never end up with orphaned photo rows
pointing at a package that no longer exists.

### Step 3: Adding and removing photos (packages)

**Lines 2323–2368:**

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

*Line by line, `add_package_photo`:*
1. Admin-only.
2. Checks the package is real, the chosen image is genuinely one of the
   allowed files (`image not in list_static_images()` stops someone typing
   in a made-up filename), and the 12-photo cap hasn't been hit.
3. If all good, inserts the row, with `sort_order` set to the current photo
   count (so each new photo naturally lands at the end of the list), and
   logs it to the activity history from Feature 2.

*`delete_package_photo`* is the mirror image: admin-only, deletes the one
row, logs it.

The exact same pattern exists for destinations at **lines 919–964**
(`add_place_photo` / `delete_place_photo`) — the only difference is a
destination is looked up by its web-address slug (like `bandipur`) rather
than a plain number.

### Step 4: Showing the gallery to travellers

In `package_detail()` (**lines 1031–1050**):

```python
    photos = query(
        "SELECT id, image, caption FROM package_photos WHERE package_id = %s ORDER BY sort_order, id",
        (package_id,),
    )
    return render_template("package_detail.html", package=package, photos=photos)
```

And in `place_detail()` (**lines 1085–1107**):

```python
    photos = query(
        "SELECT id, image, caption FROM place_photos WHERE place_id = %s ORDER BY sort_order, id",
        (place["id"],),
    )
    return render_template(
        "place_detail.html", place=place, similar_places=similar_places, photos=photos
    )
```

*What this does:* fetches that item's photos, in the saved order
(`ORDER BY sort_order, id`), and hands them to the page. The page only
shows a gallery section at all if the list isn't empty.

### New/changed admin pages

- **Manage Packages** — each package's edit panel gained a "Photo gallery"
  section: thumbnails with a Remove button, plus a dropdown + caption field
  to add one.
- **Manage Places** — the same section was added per destination.

---

# Bonus: Total Revenue box (show/hide) on the Admin Dashboard

This one wasn't a roadmap item — you asked for it directly, separately, so
there's no roadmap quote for it, but the theory and code are here for
completeness.

## Theory

This uses a well-known interface pattern sometimes called **progressive
disclosure of sensitive information** — the same idea behind a password
field showing dots instead of letters, or a banking app hiding your
balance until you tap an eye icon. The reasoning: a number like "total
money collected" is sensitive enough that you might not want it visible to
anyone glancing at your screen (over your shoulder, in a screen-share, in a
screenshot), but you still want quick access to it yourself. Defaulting to
**hidden**, with a one-click reveal, gets both.

## Code

**Working out the number** — in `admin_dashboard()`, **lines 739–741**:

```python
    total_revenue = query(
        "SELECT COALESCE(SUM(price), 0) AS total FROM bookings WHERE payment_status = 'Paid'", fetch="one"
    )["total"]
```

*What this does:* adds up the price of every booking that has actually
been paid for. `COALESCE(..., 0)` just means "use 0 instead of a blank
result if there are no paid bookings at all."

**The hide/show box** — in `templates/admin_dashboard.html`:

```html
<div class="revenue-box">
  <div class="revenue-label">Total Revenue Collected</div>
  <div class="revenue-row">
    <span class="revenue-amount" id="revenueAmount" data-value="{{ total_revenue|npr }}">••••••</span>
    <button type="button" class="eye-btn" id="revenueToggle" aria-pressed="false" aria-label="Show total revenue">
      <!-- open-eye / closed-eye icons -->
    </button>
  </div>
</div>
```

*What this does:* the real number is quietly placed in `data-value`
(present in the page's underlying code, but not shown visually), while the
visible text starts as dots.

**The toggle logic (JavaScript):**

```javascript
document.getElementById('revenueToggle').addEventListener('click', function () {
  var amountEl = document.getElementById('revenueAmount');
  var eyeOpen = document.getElementById('eyeOpenIcon');
  var eyeClosed = document.getElementById('eyeClosedIcon');
  var isHidden = amountEl.textContent.indexOf('•') !== -1;
  if (isHidden) {
    amountEl.textContent = amountEl.dataset.value;   // reveal
    eyeOpen.hidden = true;
    eyeClosed.hidden = false;
  } else {
    amountEl.textContent = '••••••';                 // hide again
    eyeOpen.hidden = false;
    eyeClosed.hidden = true;
  }
});
```

*What this does:* every click checks "are dots currently showing?" and
flips between the real value and the dots, swapping the eye icon to match.
Because the box always starts hidden on page load, the real figure is
never shown by accident.

---

# Appendix: How Payments Already Work (eSewa + Stripe)

**This was not built today** — it already existed in your app. It's
included here, in the same Theory + Code style, only because you asked for
it to be documented too.

## Theory: why payments work the way they do here

**Two different trust problems, two different solutions.** The tricky part
of any online payment feature isn't collecting the money — it's making
absolutely sure that *only real, successful* payments get marked as paid in
your own database. If that check is done carelessly, someone could trick
your site into thinking they paid when they didn't. Your app solves this
differently for each provider, based on what each one actually offers:

- **eSewa** sends the traveller's browser back to your site with a
  signed message, but your app doesn't stop at just reading that message —
  it re-computes the "signature" itself (a cryptographic fingerprint made
  with a shared secret key) to prove the message really came from eSewa and
  wasn't altered, **and then** makes a *second*, completely separate
  request straight to eSewa's own servers asking "what actually happened
  with this transaction?" Only if eSewa's own direct answer says "COMPLETE"
  does the booking get marked paid. This is called **defence in depth** —
  two independent checks instead of trusting one.

- **Stripe** offers something stronger: a **webhook** — Stripe's own
  servers contact your server directly, with no involvement from the
  traveller's browser at all. Because the traveller's browser could always
  fail partway through (closed tab, lost signal, browser crash) *after*
  paying but *before* making it back to your site, relying on the
  traveller's browser to report success would be unreliable. The webhook
  sidesteps that entirely — Stripe tells your server directly, so payment
  confirmation doesn't depend on anything happening in the customer's
  browser at all.

**Why is a booking only payable once it's Confirmed?** This was already a
rule in your app before payments even existed (an admin has to approve a
booking request first). Payment builds on top of that same gate, so nobody
can ever pay for a booking an admin hasn't actually vetted yet — this is
called **reusing an existing security boundary** instead of building a
second, possibly weaker one from scratch.

## Code

### Configuration — lines 50–68

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

*Plain words:* `os.environ.get("NAME", default)` reads a setting from the
server's own configuration, falling back to `default` if it's not set.
eSewa's defaults point at its public **test/sandbox** system (safe, no real
money). Stripe's key has **no default at all** — deliberately, so a
forgotten setup fails with a clear error instead of quietly doing nothing.

**Lines 71–72**, currency conversion:

```python
def npr_to_usd_cents(npr_amount):
    return int(round((float(npr_amount) / NPR_PER_USD) * 100))
```

*Plain words:* rupees ÷ exchange rate = dollars; × 100 = cents (Stripe
always wants the smallest unit of a currency, not the "big" unit).

**Lines 75–77**, the signing function used to prove a message is genuine:

```python
def esewa_sign(message, secret_key):
    digest = hmac.new(secret_key.encode("utf-8"), message.encode("utf-8"), hashlib.sha256).digest()
    return base64.b64encode(digest).decode("utf-8")
```

*Plain words:* creates a scrambled "fingerprint" of a message using a
secret key only your server and eSewa know. Anyone without that key
can't produce a matching fingerprint, which is what makes it trustworthy
as proof.

**Lines 180–190**, keeping the database ready:

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

*Plain words:* checks the `bookings` table already has the columns needed
to track a payment, and adds any missing ones automatically the first time
the app runs.

**Lines 1362–1369**, the shared safety check both payment methods use:

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

*Plain words:* looks the booking up **only if it belongs to whoever is
logged in** (so no one can pay for someone else's booking by guessing an
ID), and refuses unless it's `Confirmed` and not already `Paid`.

### The eSewa journey

**Sending the traveller to eSewa — lines 1372–1407:**

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

*Step by step:* checks login and eligibility → builds a unique transaction
ID → signs a message proving this request is genuine → assembles every
field eSewa's payment form needs → saves the transaction ID on the booking
→ shows a page that auto-submits straight to eSewa.

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

*Plain words:* an invisible form filled in with the payment details, aimed
at eSewa's real website, submitted automatically by the one line of
JavaScript at the bottom — to the traveller it just looks like "taking you
to eSewa now."

**eSewa sends the traveller back — lines 1410–1456:**

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

*Step by step, matching the "defence in depth" theory above:*
1. Decode the scrambled data eSewa put in the web address.
2. **Check 1 — signature:** rebuild the exact fingerprint eSewa should have
   made and compare it. Mismatch → rejected, don't trust it.
3. **Check 2 — ask eSewa directly:** call eSewa's own status-check web
   address and ask what really happened, independent of anything the
   browser reported.
4. Only if eSewa's own direct answer says `"COMPLETE"` is the booking
   marked `Paid`.

**Lines 1459–1462**, the failure path:

```python
@app.route("/esewa/failure")
def esewa_failure():
    flash("Payment was cancelled or failed.", "error")
    return redirect(url_for("my_bookings"))
```

### The Stripe journey

**Starting a Stripe checkout — lines 1465–1497:**

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

*Step by step:* checks eligibility → asks Stripe itself to build a
ready-made payment page for this exact item and price, quietly attaching
the booking's ID as `metadata` (a tag Stripe hands back later) → saves the
new session's ID on the booking → sends the traveller's browser to Stripe's
own website to actually pay.

**Landing back on your site — lines 1500–1506:**

```python
@app.route("/stripe/success")
def stripe_success():
    # Display-only. Stripe can land a browser here without a completed
    # payment (tab closed early, etc.) -- the webhook below is the only
    # place payment_status is actually flipped to 'Paid'.
    flash("Payment received -- confirming now.", "success")
    return redirect(url_for("my_bookings"))
```

*Plain words:* just a friendly message. Crucially, **this page does not
mark anything as paid** — see the theory section above for why that would
be unreliable.

**The real confirmation — lines 1509–1530:**

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

*Step by step:* Stripe's own server (not the traveller's browser) calls
this address directly. The signature is verified (proving it really came
from Stripe) before anything else happens. If the event says a checkout
finished, the booking ID is pulled back out of the `metadata` tag from
earlier, double-checked against the Stripe session ID, and **only then**
marked `Paid`.

### Where the buttons come from

In `templates/my_bookings.html`:

```html
<a class="pay pay-esewa" href="{{ url_for('pay_with_esewa', booking_id=booking.id) }}">Pay with eSewa (NPR)</a>
<a class="pay pay-stripe" href="{{ url_for('pay_with_stripe', booking_id=booking.id) }}">Pay with Card (USD)</a>
```

Plain links to the two routes above — clicking one starts that payment
journey.

---

## One unrelated thing I noticed (not fixed, just flagged)

While testing today's work, `test_reports.py` (one of your existing test
files) fails — but this is unrelated to anything in this document. It's
caused by some other in-progress `payment_status` work that was already
present in your project files before today (visible in `admin_reports()`,
and hinted at by the `payment_integration_*.txt` notes already in your
project). I didn't touch that code or that test — just noting it here so it
isn't confused with the features above.
