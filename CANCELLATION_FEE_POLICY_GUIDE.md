# Cancellation Fee Policy — Full Feature Guide

This file explains, in plain language, the flat 20% cancellation fee
policy added to GoGather's refund flow: what it does, the roadmap in
simple steps, the functions/routes involved, and the full code.

It builds directly on the refund and cancellation system documented in
`PAYMENT_PHOTO_CANCELLATION_REFUND_GUIDE.md` — read that first if you
want the full context on how `refund_requests` and the
`Requested → Approved → Refund Pending → Refunded` state machine work.

---

## What it does, in plain language

Cancelling a **paid** booking used to refund 100% of what the traveller
paid. Now a flat **20% cancellation fee** is withheld automatically —
the traveller gets 80% back, GoGather keeps 20%. This is a policy
decision, not admin discretion: it applies the same way every time, with
no button to waive it, and the amount withheld is calculated and locked
in the moment the cancellation is requested (not at approval time), so it
can't drift if the booking's price is edited later.

## Roadmap (step by step)

1. One constant defines the whole policy: `CANCELLATION_FEE_PERCENT = 20`.
   Change this single number and every calculation, everywhere, follows.
2. `refund_requests` gets a new column, `cancellation_fee`, added
   automatically the next time the app starts (a self-migrating
   `ALTER TABLE`, the same pattern already used for `packages` and
   `users` — no manual database step, no separate migration file).
3. The moment a traveller requests cancellation of a **paid** booking
   (`cancel_booking()`), two numbers are computed from the booking's
   price: `cancellation_fee = price × 20%` and
   `refund_amount = price − cancellation_fee`. Both are stored on the new
   refund request row — `amount` keeps meaning exactly what it always
   meant ("what will actually be refunded"), while `cancellation_fee` is
   purely an auditable record of what was withheld.
4. The traveller sees the breakdown **before** they even submit the
   request (on the "Request cancellation & refund" form: *"A 20%
   cancellation fee applies — you'll be refunded Rs 6,800 of your Rs
   8,500 payment"*), and again in the flash message and notification
   right after submitting — so the deduction is never a surprise
   discovered later.
5. The admin, reviewing the request in Manage Refunds, sees the same
   breakdown ("of Rs 8,500 paid · Rs 1,700 cancellation fee withheld")
   next to the amount they're about to approve.
6. Nothing else in the refund state machine changes — the
   `Requested → Approved → Refund Pending → Refunded` flow, the
   reference-number requirement, the notifications — all operate on
   `amount` exactly as before, just with a smaller number in it than the
   full booking price.
7. Once `Refunded`, the traveller's own bookings page shows the final
   confirmation with the fee spelled out: *"Rs 6,800 refunded (after a
   Rs 1,700 cancellation fee) · Reference: FEE-TEST-1"*.

## Functions / routes used

| Name | Type | Job |
|---|---|---|
| `CANCELLATION_FEE_PERCENT` | constant | The single source of truth for the fee rate |
| `ensure_refund_requests_table()` (updated) | function | Adds the `cancellation_fee` column if it isn't already there |
| `cancel_booking()` (updated) | route `POST /my_bookings/<id>/cancel` | Computes and stores the fee + net refund amount at request time |
| `my_bookings()` (updated) | route `GET /my_bookings` | Also selects `cancellation_fee` so it can be shown to the traveller |
| `admin_refunds()` (updated) | route `GET /admin/refunds` | Also selects `cancellation_fee` and the original booking price for the admin's breakdown |

## Full code

```python
# Cancellation policy: a traveller cancelling a paid booking forfeits this
# percentage of what they paid -- the rest is what actually gets requested
# as a refund. Flat and automatic (not admin-adjustable, not tiered by how
# close to the travel date it is).
CANCELLATION_FEE_PERCENT = 20


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

```python
# Inside cancel_booking(), the branch for a paid, Confirmed booking:

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
```

```python
# my_bookings() -- added r.cancellation_fee AS refund_fee to the SELECT
SELECT b.id, b.package_name, b.travel_date, b.travellers, b.phone, b.price, b.status,
       b.payment_status, b.created_at,
       r.status AS refund_status, r.reason AS refund_reason, r.amount AS refund_amount,
       r.cancellation_fee AS refund_fee,
       r.admin_notes AS refund_admin_notes, r.refund_reference AS refund_reference,
       r.updated_at AS refund_updated_at
FROM bookings b
LEFT JOIN refund_requests r ON r.id = (
    SELECT id FROM refund_requests WHERE booking_id = b.id ORDER BY id DESC LIMIT 1
)
WHERE b.user_id = %s
```

```python
# admin_refunds() -- added r.cancellation_fee and b.price AS booking_price
SELECT r.id, r.booking_id, r.reason, r.amount, r.cancellation_fee, r.status, r.admin_notes,
       r.refund_reference, r.created_at, r.updated_at,
       b.package_name, b.travel_date, b.price AS booking_price,
       u.full_name AS traveller_name, u.username AS traveller_username
FROM refund_requests r
JOIN bookings b ON b.id = r.booking_id
JOIN users u ON u.id = r.user_id
```

```html
<!-- templates/my_bookings.html -- pre-submit notice on the cancellation form -->
<form method="post" action="{{ url_for('cancel_booking',booking_id=booking.id) }}" onsubmit="return confirm('Submit a cancellation and refund request for this booking?')">
  <p class="refund-fee-notice">A {{ cancellation_fee_percent }}% cancellation fee applies &mdash; you'll be refunded {{ (booking.price|float * (1 - cancellation_fee_percent / 100))|round|npr }} of your {{ booking.price|npr }} payment.</p>
  <textarea name="reason" rows="2" required maxlength="2000" placeholder="Tell us why you'd like to cancel this booking"></textarea>
  <button class="cancel" type="submit">Submit request</button>
</form>

<!-- while in progress / after refunded -->
{% if booking.refund_status == 'Refunded' %}
  <span>{{ booking.refund_amount|npr }} refunded{% if booking.refund_fee %} (after a {{ booking.refund_fee|npr }} cancellation fee){% endif %}{% if booking.refund_reference %} &middot; Reference: {{ booking.refund_reference }}{% endif %}</span>
{% endif %}
{% if booking.refund_status in ('Requested', 'Approved', 'Refund Pending') %}
  <span>{{ booking.refund_amount|npr }} will be refunded (after a {{ booking.refund_fee|npr }} cancellation fee) once approved.</span>
{% endif %}
```

```html
<!-- templates/admin_refunds.html -- fee breakdown next to the refundable amount -->
<div class="refund-amount">
  {{ r.amount|npr }}
  {% if r.cancellation_fee %}<div class="refund-fee-breakdown">of {{ r.booking_price|npr }} paid &middot; {{ r.cancellation_fee|npr }} cancellation fee withheld</div>{% endif %}
</div>
```

**Tested against the real dev database, full lifecycle:** an Rs 8,500
paid booking was cancelled → request stored with `amount = 6,800.00`,
`cancellation_fee = 1,700.00` → admin's Manage Refunds page correctly
showed "of Rs 8,500 paid · Rs 1,700 cancellation fee withheld" →
approved → refund pending → marked Refunded with reference
`FEE-TEST-1` → traveller's My Bookings page correctly displayed "Rs
6,800 refunded (after a Rs 1,700 cancellation fee) · Reference:
FEE-TEST-1".
