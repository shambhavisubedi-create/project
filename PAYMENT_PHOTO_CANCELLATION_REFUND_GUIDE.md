# Payment, Photo, Cancellation & Refund — Full Feature Guide

This file explains, in plain language, exactly how four features work in
`app.py`: **Payment**, **Profile Photo** (upload/history), **Cancellation**,
and **Refund**. For each one you get: what it actually does, the roadmap in
simple steps, the functions/routes involved, and the full code.

---

# 1. PAYMENT (eSewa + Stripe)

## What it does, in plain language

Once an admin has confirmed a booking (checked it isn't fraudulent/fake),
the traveller can pay for it two ways:

- **eSewa** — a Nepali payment gateway, charged in NPR. The traveller is
  sent to eSewa's own site to log in and approve the payment, then eSewa
  sends them back to GoGather with a signed result.
- **Stripe** — a card payment gateway, charged in USD (converted from the
  NPR price using a fixed exchange rate). The traveller enters card details
  on Stripe's own checkout page.

Either way, GoGather **never touches the traveller's card or eSewa
password** — both gateways handle that on their own pages. GoGather only
ever receives a signed "yes, this was paid" message back.

A booking can only be paid for if:
1. It belongs to the traveller trying to pay.
2. Its status is `Confirmed` (an admin already approved it).
3. It hasn't already been paid.

## Roadmap (step by step)

1. Traveller clicks "Pay with eSewa" or "Pay with Card" on `my_bookings.html`.
2. The app checks the booking is actually payable (`get_payable_booking`).
3. **eSewa path:** the app builds a signed form (HMAC-SHA256 signature using
   a secret key) and renders `esewa_redirect.html`, which auto-submits the
   traveller straight to eSewa's payment page.
   **Stripe path:** the app creates a Stripe Checkout Session (server-side,
   using the secret key) and redirects the traveller to the URL Stripe gives
   back.
4. The traveller completes (or cancels) payment on the gateway's own site.
5. The gateway sends the traveller's browser back to a "success" URL on
   GoGather, carrying a result.
6. **eSewa:** the app decodes that result, **recomputes the signature
   itself** (never trusts the browser redirect at face value), and then
   double-checks the payment status directly against eSewa's status API
   before marking anything as paid.
   **Stripe:** the success page is just a "thanks, confirming now" message —
   the *actual* confirmation only ever happens through a separate webhook
   call from Stripe's servers (see step 7), because a browser landing on
   the success URL doesn't prove the payment really went through (the tab
   could've been closed early, etc.).
7. **Stripe webhook** (`/webhooks/stripe`) — Stripe's server calls this
   directly, signed with a webhook secret the app verifies. Only once that
   signed event says `checkout.session.completed` does the app mark the
   booking as paid.
8. On success (either gateway), `bookings.payment_status` is set to `'Paid'`
   and the traveller gets a notification.

## Functions / routes used

| Name | Type | Job |
|---|---|---|
| `esewa_sign(message, secret_key)` | function | Creates the HMAC-SHA256 signature eSewa requires, and that the app uses to verify eSewa's response |
| `npr_to_usd_cents(npr_amount)` | function | Converts an NPR price into USD cents for Stripe |
| `get_payable_booking(booking_id)` | function | Confirms the booking exists, belongs to this traveller, is `Confirmed`, and isn't already paid |
| `pay_with_esewa(booking_id)` | route `GET /pay/esewa/<id>` | Builds the signed eSewa form and renders the auto-submit redirect page |
| `esewa_success()` | route `GET /esewa/success` | Verifies eSewa's signed callback, re-checks status with eSewa directly, marks booking paid |
| `esewa_failure()` | route `GET /esewa/failure` | Flashes a "payment failed/cancelled" message |
| `pay_with_stripe(booking_id)` | route `GET /pay/stripe/<id>` | Creates a Stripe Checkout Session and redirects to it |
| `stripe_success()` | route `GET /stripe/success` | Display-only "confirming now" message |
| `stripe_webhook()` | route `POST /webhooks/stripe` | The ONLY place that actually flips a Stripe payment to `Paid`, triggered by Stripe's own server, signature-verified |

## Full code

```python
# --- Config (top of app.py) ---

ESEWA_PRODUCT_CODE = os.environ.get("ESEWA_PRODUCT_CODE", "EPAYTEST")
ESEWA_SECRET_KEY = os.environ.get("ESEWA_SECRET_KEY", "8gBm/:&EnhH.1/q")
ESEWA_FORM_URL = os.environ.get("ESEWA_FORM_URL", "https://rc-epay.esewa.com.np/api/epay/main/v2/form")
ESEWA_STATUS_URL = os.environ.get("ESEWA_STATUS_URL", "https://rc.esewa.com.np/api/epay/transaction/status/")

STRIPE_SECRET_KEY = os.environ.get("STRIPE_SECRET_KEY", "")
STRIPE_WEBHOOK_SECRET = os.environ.get("STRIPE_WEBHOOK_SECRET", "")
stripe.api_key = STRIPE_SECRET_KEY

NPR_PER_USD = float(os.environ.get("NPR_PER_USD", "133"))


def npr_to_usd_cents(npr_amount):
    return int(round((float(npr_amount) / NPR_PER_USD) * 100))


def esewa_sign(message, secret_key):
    digest = hmac.new(secret_key.encode("utf-8"), message.encode("utf-8"), hashlib.sha256).digest()
    return base64.b64encode(digest).decode("utf-8")


# --- Payment routes ---

def get_payable_booking(booking_id):
    booking = query(
        "SELECT id, user_id, package_name, price, status, payment_status FROM bookings WHERE id = %s AND user_id = %s",
        (booking_id, session["user_id"]), fetch="one",
    )
    if not booking or booking["status"] != "Confirmed" or booking["payment_status"] == "Paid":
        return None
    return booking


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


@app.route("/esewa/failure")
def esewa_failure():
    flash("Payment was cancelled or failed.", "error")
    return redirect(url_for("my_bookings"))


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


@app.route("/stripe/success")
def stripe_success():
    # Display-only. Stripe can land a browser here without a completed
    # payment (tab closed early, etc.) -- the webhook below is the only
    # place payment_status is actually flipped to 'Paid'.
    flash("Payment received -- confirming now.", "success")
    return redirect(url_for("my_bookings"))


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

## Why it's built this way (the important bit)

- **The signature check on `esewa_success` is the whole security model.**
  Anyone could type any URL with any `?data=` value into their browser and
  hit that route directly — the only reason that can't be used to fake a
  free payment is that the app recomputes eSewa's signature itself using a
  secret key eSewa and GoGather both know, and rejects anything that
  doesn't match.
- **Stripe confirmation happens via webhook, not the success page**, because
  a browser reaching `/stripe/success` proves nothing — the user could
  close the tab before Stripe finishes, or someone could visit that URL
  directly without paying. Only Stripe's own signed server-to-server call
  can be trusted.

---

# 2. PROFILE PHOTO (upload / history — "photo restoration")

## What it does, in plain language

There is **no explicit "restore a deleted photo" button** in the app today.
What exists is a **versioned upload system**: every time a traveller
uploads a new profile photo, the old one is *never deleted* — it just stops
being pointed to. In effect, every old photo is still sitting on the
server's disk and could be manually restored by an admin/developer, even
though there's no UI for it. This is likely what "photo restoration" refers
to — the mechanism is "photos are never actually destroyed," not "there's a
restore button."

## Roadmap (step by step)

1. Two rules are set up front: only `png/jpg/jpeg/webp` files are accepted,
   and nothing bigger than 3 MB.
2. Traveller submits the Edit Profile form with (optionally) a new photo
   attached.
3. If no file was attached, nothing photo-related happens at all — editing
   your name/phone never touches your photo.
4. If a file *was* attached, it's checked against the two rules from step 1
   before anything is saved. Reject early = old photo untouched.
5. A **brand-new filename** is invented using the exact upload timestamp
   (down to the millisecond): `avatar_<user_id>_<timestamp>.<ext>`. This
   guarantees the new file never collides with — or overwrites — the old
   one.
6. The new file is written to `static/uploads/avatars/`. The previous
   file for that user is still sitting in the same folder, completely
   untouched.
7. Only one row in the database changes: `users.avatar` is updated to point
   at the new filename. That's the only "switch" that happens.
8. Every time the profile page loads, it just displays whatever `avatar`
   currently points to — it has no idea older files exist.

## Functions / routes used

| Name | Type | Job |
|---|---|---|
| `avatar_extension(filename)` | function | Returns the file extension only if it's one of the allowed types, else `None` |
| `save_avatar(user_id, filename, data)` | function | Writes the uploaded bytes to a brand-new, never-reused filename on disk |
| `ensure_user_columns()` | function | Adds the `avatar` column to `users` if it doesn't exist yet |
| `profile()` | route `GET/POST /profile` | Handles viewing and editing the traveller's profile, including the avatar swap |

## Full code

```python
AVATAR_EXTENSIONS = {"png", "jpg", "jpeg", "webp"}
MAX_AVATAR_SIZE = 3 * 1024 * 1024  # 3 MB


def avatar_extension(filename):
    ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
    return ext if ext in AVATAR_EXTENSIONS else None


def save_avatar(user_id, filename, data):
    """Saves each upload under a unique name (avatar_<user_id>_<timestamp>.<ext>)
    so previous avatars stay on the server instead of being deleted."""
    ext = avatar_extension(filename)
    avatar_dir = Path(app.static_folder) / "uploads" / "avatars"
    avatar_dir.mkdir(parents=True, exist_ok=True)
    timestamp = int(time.time() * 1000)
    avatar_filename = f"avatar_{user_id}_{timestamp}.{ext}"
    avatar_path = avatar_dir / avatar_filename
    avatar_path.write_bytes(data)
    return f"uploads/avatars/{avatar_filename}"


def ensure_user_columns():
    columns = {row["Field"] for row in query("DESCRIBE users")}
    if "avatar" not in columns:
        execute("ALTER TABLE users ADD COLUMN avatar VARCHAR(255) NULL")


@app.route("/profile", methods=["GET", "POST"])
def profile():

    if "username" not in session:
        return redirect(url_for("login"))

    if session.get("user") != 0:
        return redirect(url_for("admin_dashboard"))

    if request.method == "POST":

        full_name = request.form.get("full_name", "").strip()
        email = request.form.get("email", "").strip()
        phone = request.form.get("phone", "").strip()

        avatar_file = request.files.get("avatar")
        avatar_data = avatar_file.read() if avatar_file and avatar_file.filename else None

        if not full_name or not email:
            flash("Full name and email are required.", "error")

        elif avatar_data is not None and not avatar_extension(avatar_file.filename):
            flash("Please upload a PNG, JPG or WEBP image.", "error")

        elif avatar_data is not None and len(avatar_data) > MAX_AVATAR_SIZE:
            flash("Please upload an image under 3 MB.", "error")

        else:
            try:
                execute(
                    "UPDATE users SET full_name = %s, email = %s, phone = %s WHERE id = %s",
                    (full_name, email, phone or None, session["user_id"]),
                )

                if avatar_data is not None:
                    avatar_path = save_avatar(session["user_id"], avatar_file.filename, avatar_data)
                    execute("UPDATE users SET avatar = %s WHERE id = %s", (avatar_path, session["user_id"]))

            except IntegrityError:
                flash("That email address is already in use.", "error")

            else:
                flash("Your profile has been updated.", "success")
                return redirect(url_for("profile"))

    user = query(
        "SELECT full_name, username, email, phone, avatar, created_at FROM users WHERE id = %s",
        (session["user_id"],), fetch="one",
    )

    return render_template("profile.html", user=user)
```

## Real restore feature (built)

The gap above — old photos existing on disk but with no way to actually get
one back — has now been closed with a proper `avatar_history` table and a
"Restore" button on the Profile page.

### What it does, in plain language

Every photo a traveller has ever uploaded is now a row in `avatar_history`,
not just an orphaned file. The Profile page shows every one of them as a
small thumbnail strip, the currently-active one badged "Current," and every
other one with a "Restore" button that makes it the active photo again —
instantly, no re-upload needed.

### Roadmap (step by step)

1. A new table, `avatar_history`, stores one row per upload: which user, the
   file path, and when.
2. **First time this feature runs** (table didn't exist yet), it backfills
   itself: whatever photo each traveller already had in `users.avatar`
   becomes that traveller's first history entry, so nobody's existing photo
   is left out of their own history.
3. **Every future upload** — the same `profile()` POST handler that already
   saved the new file (Section 2) now also inserts a row into
   `avatar_history` right after updating `users.avatar`. The old photo isn't
   touched, and it's already in the history table (from step 2, or from its
   own upload at the time), so nothing is ever lost.
4. **Viewing the profile page**, `avatar_history` is queried for that
   traveller (newest first) and handed to the template.
5. **`profile.html`** loops over that list: the entry whose path matches
   `user.avatar` gets a "Current" badge instead of a button; every other one
   gets a small "Restore" form.
6. **Clicking Restore** posts to `/profile/avatar/<history_id>/restore`,
   which re-checks that history row actually belongs to the logged-in
   traveller (so nobody can restore someone else's photo by guessing an ID),
   then simply does `UPDATE users SET avatar = <that path>`. That's the
   entire "restore" operation — one column changes back to an old value.
   The photo that was active a second ago doesn't disappear; it's already a
   row in the same history table, so it can be restored right back too.

### Functions / routes used

| Name | Type | Job |
|---|---|---|
| `ensure_avatar_history_table()` | function | Creates `avatar_history` if missing, and backfills each user's current photo into it the first time |
| `profile()` (updated) | route `GET/POST /profile` | Now also inserts a history row on every successful upload, and passes the full history list to the template |
| `restore_avatar(history_id)` | route `POST /profile/avatar/<id>/restore` | Verifies the photo belongs to the logged-in traveller, then makes it the active photo again |

### Full code

```python
def ensure_avatar_history_table():
    """Every photo a traveller has ever uploaded is kept here, so an old
    photo can be brought back instead of only ever having 'the current one'
    and whatever's silently still sitting on disk."""
    existed = bool(query("SHOW TABLES LIKE 'avatar_history'"))
    execute(
        """
        CREATE TABLE IF NOT EXISTS avatar_history (
            id INT AUTO_INCREMENT PRIMARY KEY,
            user_id INT NOT NULL,
            path VARCHAR(255) NOT NULL,
            created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
        )
        """
    )
    if not existed:
        # Backfill so whatever photo a traveller already has isn't the one
        # thing missing from their own history.
        for row in query("SELECT id, avatar FROM users WHERE avatar IS NOT NULL"):
            execute(
                "INSERT INTO avatar_history (user_id, path) VALUES (%s, %s)",
                (row["id"], row["avatar"]),
            )


@app.route("/profile", methods=["GET", "POST"])
def profile():

    if "username" not in session:
        return redirect(url_for("login"))

    if session.get("user") != 0:
        return redirect(url_for("admin_dashboard"))

    if request.method == "POST":

        full_name = request.form.get("full_name", "").strip()
        email = request.form.get("email", "").strip()
        phone = request.form.get("phone", "").strip()

        avatar_file = request.files.get("avatar")
        avatar_data = avatar_file.read() if avatar_file and avatar_file.filename else None

        if not full_name or not email:
            flash("Full name and email are required.", "error")

        elif avatar_data is not None and not avatar_extension(avatar_file.filename):
            flash("Please upload a PNG, JPG or WEBP image.", "error")

        elif avatar_data is not None and len(avatar_data) > MAX_AVATAR_SIZE:
            flash("Please upload an image under 3 MB.", "error")

        else:
            try:
                execute(
                    "UPDATE users SET full_name = %s, email = %s, phone = %s WHERE id = %s",
                    (full_name, email, phone or None, session["user_id"]),
                )

                if avatar_data is not None:
                    avatar_path = save_avatar(session["user_id"], avatar_file.filename, avatar_data)
                    execute("UPDATE users SET avatar = %s WHERE id = %s", (avatar_path, session["user_id"]))
                    execute(
                        "INSERT INTO avatar_history (user_id, path) VALUES (%s, %s)",
                        (session["user_id"], avatar_path),
                    )

            except IntegrityError:
                flash("That email address is already in use.", "error")

            else:
                flash("Your profile has been updated.", "success")
                return redirect(url_for("profile"))

    user = query(
        "SELECT full_name, username, email, phone, avatar, created_at FROM users WHERE id = %s",
        (session["user_id"],), fetch="one",
    )

    avatar_history = query(
        "SELECT id, path, created_at FROM avatar_history WHERE user_id = %s ORDER BY created_at DESC",
        (session["user_id"],),
    )

    return render_template("profile.html", user=user, avatar_history=avatar_history)


@app.route("/profile/avatar/<int:history_id>/restore", methods=["POST"])
def restore_avatar(history_id):

    if "username" not in session:
        return redirect(url_for("login"))

    if session.get("user") != 0:
        return redirect(url_for("admin_dashboard"))

    entry = query(
        "SELECT id, path FROM avatar_history WHERE id = %s AND user_id = %s",
        (history_id, session["user_id"]), fetch="one",
    )

    if entry is None:
        flash("That photo could not be found.", "error")
    else:
        execute("UPDATE users SET avatar = %s WHERE id = %s", (entry["path"], session["user_id"]))
        flash("Your profile photo has been restored.", "success")

    return redirect(url_for("profile"))
```

```html
<!-- templates/profile.html, between the avatar and the edit form -->
{% if avatar_history %}
<div class="history-section">
  <p class="history-title">Your previous photos</p>
  <div class="history-grid">
    {% for photo in avatar_history %}
      <div class="history-item {{ 'is-current' if photo.path == user.avatar else '' }}">
        <div class="history-thumb-wrap">
          <img class="history-thumb" src="{{ url_for('static', filename=photo.path) }}" alt="Photo from {{ photo.created_at.strftime('%d %b %Y') }}">
        </div>
        {% if photo.path == user.avatar %}
          <span class="history-current-badge">Current</span>
        {% else %}
          <form method="POST" action="{{ url_for('restore_avatar', history_id=photo.id) }}">
            <button type="submit" class="history-restore-btn">Restore</button>
          </form>
        {% endif %}
      </div>
    {% endfor %}
  </div>
</div>
{% endif %}
```

Registered at startup in both places `app.py` sets up tables, so it works
whether the app is launched with `python app.py` or `flask run`:

```python
# inside the @app.before_request prepare_notifications() gate, and again
# in the if __name__ == "__main__": block
ensure_avatar_history_table()
```

**Tested against the real dev database:** backfill correctly picked up two
existing users' current photos as their first history entry; uploading a
new photo added a second entry without touching the first; restoring the
first entry flipped `users.avatar` back to it while keeping the second
entry restorable too.

---

# 3. CANCELLATION

## What it does, in plain language

A traveller can cancel a booking, but **what happens depends on whether
they already paid**:

- **Not paid yet** → cancellation is instant, no admin needed.
- **Already paid** → cancelling requires giving the traveller their money
  back, so instead of an instant flip to "Cancelled," it creates a
  **Refund Request** (see Section 4) that an admin has to review and
  process step by step. This stops a traveller (or a bug) from silently
  wiping out a paid, confirmed booking with no trace.

The exact outcome also depends on the booking's current status:

| Booking status | Paid? | What clicking "Cancel" does |
|---|---|---|
| `Pending` | No | Cancelled immediately — nothing to undo since admin hadn't confirmed it yet |
| `Confirmed` | No | Cancelled immediately, admin notified |
| `Confirmed` | Yes (`Paid`) | Creates a refund request, booking status becomes `Cancel Requested`, awaits admin |
| `Cancel Requested` | — | Blocked — "you already requested cancellation" |
| `Cancelled` | — | Blocked — "already cancelled" |

## Roadmap (step by step)

1. Traveller clicks "Cancel booking" (or, for paid bookings, opens "Request
   cancellation & refund" and types a reason) on `my_bookings.html`.
2. The app loads that exact booking, but only if it belongs to the
   logged-in traveller (`... WHERE id = %s AND user_id = %s`) — this stops
   anyone from cancelling someone else's booking by guessing an ID.
3. If the booking doesn't exist for this user → error, stop.
4. **If it was already paid:**
   - It must still be `Confirmed` (can't cancel something already
     completed or in another cancellation flow).
   - It must not already have an open refund request in progress.
   - A reason is required (so admin has context to review against).
   - A row is inserted into `refund_requests`, the booking's status becomes
     `Cancel Requested`, and both the admin team and the traveller are
     notified.
5. **If it was never paid, and already cancelled / already requested** →
   blocked with an explanatory error.
6. **If it was never paid and still `Pending`** → cancelled immediately,
   since an unconfirmed booking never had money on the line.
7. **If it was never paid but already `Confirmed`** → same instant
   cancellation, just with a slightly different notification message.

## Functions / routes used

| Name | Type | Job |
|---|---|---|
| `cancel_booking(booking_id)` | route `POST /my_bookings/<id>/cancel` | The entire cancellation decision tree described above |
| `get_open_refund_request(booking_id)` | function (shared with Refund) | Checks whether this booking already has an in-progress refund request, to prevent duplicates |
| `notify(user_id, message, link)` | function | Writes one notification for one user |
| `notify_admins(message, link)` | function | Writes the same notification to every admin account |

## Full code

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

There's a matching guard on the **admin** side too — an admin can't
directly flip a booking's status away from `Cancel Requested` if there's an
open refund attached to it (in `update_booking_status`, `/admin/bookings/
<id>/update-status`), forcing the admin to resolve it through Manage
Refunds instead, so a booking's status and its refund's status can never
drift apart:

```python
elif get_open_refund_request(booking_id):
    # This booking's cancellation is a paid refund in progress -- change
    # its status from the refund request instead, so the booking status
    # and the refund's own status can't drift apart.
    flash("This booking has an open refund request. Resolve it from Manage Refunds first.", "error")
```

---

# 4. REFUND

## What it does, in plain language

Once a paid booking's cancellation creates a refund request (Section 3),
that request moves through **four admin-controlled stages**, in order,
never skipping a step:

```
Requested → Approved → Refund Pending → Refunded
                \
                 → Rejected (possible from "Requested" or "Approved")
```

- **Requested** — traveller asked to cancel + get their money back.
- **Approved** — admin agrees the refund is legitimate.
- **Refund Pending** — admin has started actually sending the money back
  (outside the app, e.g. via eSewa/Stripe's dashboard) and is waiting to
  record proof.
- **Refunded** — admin has entered the payment provider's refund reference
  number, confirming the money was actually sent. Only at this point does
  the booking flip to `Cancelled` / `payment_status = 'Refunded'`.
- **Rejected** — admin declines the request at any point before it's fully
  refunded, and must type a reason (shown to the traveller).

The traveller sees this whole journey on `my_bookings.html`, and once it
reaches `Refunded`, they also see **exactly how much was refunded and the
reference number** (this is the part added most recently).

## Roadmap (step by step)

1. `refund_requests` table stores one row per request: which booking, which
   traveller, why, how much, current status, admin's notes, and the refund
   reference once completed.
2. Admin opens **Manage Refunds** (`/admin/refunds`), sees every request
   grouped by status, with live counts for each stage.
3. Admin clicks the action button appropriate to that request's current
   stage (`Approve request`, `Start refund`, or `Confirm refunded` — the
   last one requires typing the payment provider's reference number first).
4. Every click goes to one single route, `update_refund_status`, which:
   - Looks up the refund's **current** status.
   - Checks the requested new status is a *legal next step* from there
     (using an explicit `allowed_next` map) — this is what stops a request
     from jumping straight from `Requested` to `Refunded`, or being
     re-decided once already finished.
   - Requires a refund reference before allowing `Refunded`.
   - Requires an explanation before allowing `Rejected`.
   - Updates the row, and — only on `Refunded` or `Rejected` — also updates
     the underlying booking's status, and notifies the traveller.
   - Logs the change to the admin activity log for auditing.
5. Traveller sees the current stage (and, once refunded, the amount +
   reference) on their own bookings page.

## Functions / routes used

| Name | Type | Job |
|---|---|---|
| `ensure_refund_requests_table()` | function | Creates the `refund_requests` table if missing |
| `get_open_refund_request(booking_id)` | function | Returns the most recent *unfinished* refund request for a booking, or `None` — used to block duplicate requests and to gate the admin's direct booking-status route |
| `get_latest_refund_request(booking_id)` | function | Returns the most recent refund request regardless of status |
| `admin_refunds()` | route `GET /admin/refunds` | Lists every refund request, grouped by status, with filtering |
| `update_refund_status(refund_id)` | route `POST /admin/refunds/<id>/update-status` | The whole state-machine: validates and applies one status transition |
| `notify` / `notify_admins` | functions | Tell the traveller / admins what happened at each stage |
| `log_admin_activity(...)` | function | Records the change for the audit trail |

## Full code

```python
REFUND_STATUSES = ("Requested", "Approved", "Refund Pending", "Refunded", "Rejected")
OPEN_REFUND_STATUSES = ("Requested", "Approved", "Refund Pending")


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

### Traveller-facing balance (added most recently)

`dashboard()` (`GET /dashboard`) also computes a **"Your Balance"** figure
for the traveller, shown top-right on their dashboard behind an eye-icon
toggle (same masked-by-default pattern as the admin's "Total Revenue"):

```python
# Balance = what the traveller has actually paid GoGather, net of
# anything already refunded back to them.
total_paid = query(
    "SELECT COALESCE(SUM(price), 0) AS total FROM bookings WHERE user_id = %s AND payment_status IN ('Paid', 'Refunded')",
    (session["user_id"],), fetch="one",
)["total"]

total_refunded = query(
    "SELECT COALESCE(SUM(amount), 0) AS total FROM refund_requests WHERE user_id = %s AND status = 'Refunded'",
    (session["user_id"],), fetch="one",
)["total"]

traveller_balance = total_paid - total_refunded
```

`payment_status IN ('Paid', 'Refunded')` matters: once a refund completes,
a booking's `payment_status` flips from `'Paid'` to `'Refunded'`, so
counting only `'Paid'` would silently drop that booking from the "money
paid" side while `total_refunded` still subtracted it — producing a
negative balance. Counting both statuses as "was paid," then netting out
refunds separately, is what keeps the number correct in both states.

And `my_bookings()` (`GET /my_bookings`) pulls the refunded amount
alongside the reference number so the traveller can see exactly how much
came back to them:

```python
r.status AS refund_status, r.reason AS refund_reason, r.amount AS refund_amount,
r.admin_notes AS refund_admin_notes, r.refund_reference AS refund_reference,
r.updated_at AS refund_updated_at
```

> **Note:** a flat 20% cancellation fee was later added on top of this
> refund flow — `amount` above now holds the *net* refundable amount
> after that fee. See `CANCELLATION_FEE_POLICY_GUIDE.md` for the full
> write-up (what it does, roadmap, functions, and code).
