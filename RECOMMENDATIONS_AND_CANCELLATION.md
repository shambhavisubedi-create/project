# Recommendations & Cancellation — plain-language guide

This file explains, in simple terms, how two things work in the GoGather
app: how it suggests packages/places to a traveller, and how cancelling a
booking works. Both already exist in `app.py` — this isn't new code, just
an explanation of what's there.

---

## 1. Recommendations

Honest first note: there is **no smart recommendation engine** in this
app (nothing that learns what a traveller likes and personalizes results
for them). What exists instead are two simple, rule-based "suggestions"
features. Here's exactly how each one works.

### a) Featured packages (shown on the traveller dashboard)

An admin manually marks certain packages as "featured" (a yes/no flag on
the package, called `is_featured`). The dashboard just asks the database
for whichever packages have that flag turned on:

```python
featured_packages = query(
    "SELECT id, name, duration_days, hotel_type, transport_mode, price "
    "FROM packages WHERE is_featured = 1 ORDER BY id"
)
```

**In plain words:** it's a curated list, not a computed one. A human
(admin) decides which packages count as "featured," and the code just
displays whatever they picked, in ID order. There's no logic here beyond
"show me the ones marked featured."

### b) "Similar places" (shown on a place's detail page)

When a traveller looks at one place (say, Pokhara), the page also shows a
short list of "similar places." This is worked out with one simple rule:
**same category, not the same place, only the first 3.**

```python
similar_places = [
    item for item in all_places
    if item["category"] == place["category"] and item["slug"] != slug
][:3]
```

**In plain words:** every place has a category (like "Adventure",
"Cultural", "Nature"). This code looks at every other place, keeps only
the ones sharing the same category as the one being viewed, throws away
the current place itself, and keeps just the first 3 matches. That's the
entire "similarity" logic — it's a category match, nothing more advanced
(no ratings, no popularity, no user history involved).

### c) "Popular destinations" (shown on the traveller dashboard)

```python
popular_destinations = get_places()[:4]
```

**In plain words:** this isn't based on popularity data at all — it's
just "take the first 4 places in the list." The name is a bit misleading;
there's no click-count or booking-count behind it. Worth knowing if anyone
asks how "popular" is measured — the honest answer is it isn't measured,
it's just position in the list.

### Where each one is used

| Feature | Function/route it lives in | Shown on |
|---|---|---|
| Featured packages | `dashboard()` | Traveller dashboard |
| Similar places | `place_detail()` | A single place's detail page |
| Popular destinations | `dashboard()` | Traveller dashboard |

---

## 2. Cancellation

This is the flow a traveller goes through when they want to cancel a
booking. It lives in one function: `cancel_booking()`. The tricky part is
that "cancel" means something different depending on whether the booking
has been paid for yet.

### The rule in one sentence

**If you haven't paid, cancelling is instant. If you have paid, cancelling
starts a refund request that an admin has to approve — and you lose 20% of
what you paid as a cancellation fee.**

### The actual code

```python
CANCELLATION_FEE_PERCENT = 20


@app.route("/my_bookings/<int:booking_id>/cancel", methods=["POST"])
def cancel_booking(booking_id):

    booking = query(
        "SELECT id, status, payment_status, package_name, price FROM bookings WHERE id = %s AND user_id = %s",
        (booking_id, session["user_id"]), fetch="one",
    )

    if booking is None:
        flash("Booking not found.", "error")

    elif booking["payment_status"] == "Paid":
        # Already paid -- cancelling means giving money back, so this
        # starts a refund request instead of cancelling right away.
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
                flash(
                    f"Your cancellation and refund request has been submitted. A {CANCELLATION_FEE_PERCENT}% "
                    f"cancellation fee applies, so {format_npr(refund_amount)} of your {format_npr(booking['price'])} "
                    "payment will be refunded once approved.",
                    "success",
                )

    elif booking["status"] == "Pending":
        # Nothing was ever confirmed or paid for -- just cancel it, no
        # admin needed.
        execute(
            "UPDATE bookings SET status = 'Cancelled' WHERE id = %s AND user_id = %s",
            (booking_id, session["user_id"]),
        )
        flash("Your booking has been cancelled.", "success")

    else:
        # Confirmed but never paid -- needs admin sign-off, but there's no
        # money to refund since none was ever collected.
        execute(
            "UPDATE bookings SET status = 'Cancel Requested' WHERE id = %s AND user_id = %s",
            (booking_id, session["user_id"]),
        )
        flash("Your cancellation request has been submitted and is awaiting admin approval.", "success")

    return redirect(url_for("my_bookings"))
```

(Notification lines to admins/travellers were trimmed from the snippet
above to keep it readable — the real function also sends an in-app
notification at each step, but the cancellation logic itself is exactly
what's shown.)

### The four situations, explained simply

| What's true about the booking | What happens when you click "Cancel" |
|---|---|
| You already paid, and it's currently Confirmed | A refund request is created. You lose 20% as a fee. It now needs an admin to approve it before you get money back. |
| You already paid, but it's already cancelled or already has a cancellation in progress | Nothing new happens — you get an error message saying so. |
| You haven't paid yet, and it's still Pending (not even confirmed) | Cancelled immediately. No admin needed, since nothing was ever charged or confirmed. |
| You haven't paid yet, but it is Confirmed | Cancellation needs admin approval, but since you never paid, there's no refund step — just approval to cancel. |

### What happens after the refund request is created

Once a refund request exists, an admin has to move it through four
stages, in order, one at a time:

**Requested → Approved → Refund Pending → Refunded**

(An admin can also **Reject** it at the "Requested" or "Approved" stage —
after that, once it's actually "Refund Pending," it can only go forward to
Refunded, not backward.)

- To mark it **Refunded**, the admin must type in the actual refund
  reference number from eSewa/Stripe — you can't skip this step.
- To **Reject** it, the admin must type a reason — this is shown to the
  traveller so they know why.

This part lives in a second function, `update_refund_status()`. It's
covered in full detail (including its exact code) in
`SMALL_FEATURES_ADDED.md`, section 7, if a deeper technical explanation is
ever needed — this file keeps things at the "what does it do and why"
level.

### One thing to flag if a supervisor asks

The 20% cancellation fee is a fixed number in the code
(`CANCELLATION_FEE_PERCENT = 20`) — it's the same no matter how far in
advance someone cancels, and an admin can't change it without a developer
editing the code. If the business ever wants "cancel further ahead, pay a
smaller fee," that would need new logic, not just a settings change.
