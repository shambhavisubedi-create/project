# Features Added — Cancellation & Refunds, Admin Activity History, Photo Galleries

Implemented on 12 September 2026, covering items **7, 8 and 9** from `FEATURE_ROADMAP.md`:

1. Cancellation and refund requests
2. Admin activity history
3. Photo galleries

All three follow the existing codebase's own conventions (self-migrating MySQL
tables created via `ensure_*()` functions, inline route guards, `notify()` /
`notify_admins()` for in-app notifications, and per-page embedded `<style>`
blocks matching the wine/cream theme). Nothing outside these three features
was changed.

Every route below was smoke-tested against the real dev database (creating
and then cleaning up test rows) — see the "How it was tested" section at the
end.

---

## 1. Cancellation & Refund Requests

**Problem before:** a *paid* Confirmed booking could not be self-cancelled at
all — `cancel_booking()` just flashed *"Contact support for a cancellation or
refund"* and the Cancel button was hidden for paid bookings in
`my_bookings.html`. There was no way to track a refund's progress.

**What it does now:** a traveller with a paid, Confirmed booking can submit a
cancellation request with a reason. Admins review it from a new **Manage
Refunds** page and move it through a linear set of states, each one
notifying the traveller:

```
Requested → Approved → Refund Pending → Refunded
Requested → Rejected            (also from Approved)
```

- **Refunded** requires the admin to record the payment provider's refund
  reference before it can be confirmed.
- **Rejected** requires the admin to type a reason, which the traveller sees.
- A booking's own `status`/`payment_status` only change at the two ends of
  the flow (`Cancel Requested` when the request is filed, `Cancelled` +
  `Refunded` once money has actually moved) — everything in between lives on
  the refund request row so the two can't drift out of sync.
- Only one open request (`Requested`/`Approved`/`Refund Pending`) can exist
  per booking at a time.
- The existing instant-cancel path for **Pending** or **unpaid Confirmed**
  bookings is unchanged — this only replaces the dead end that used to exist
  for *paid* bookings.

### Database

New table, created automatically the first time the app runs (see
`ensure_refund_requests_table()` in `app.py`):

```sql
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
```

### Backend (`app.py`)

- `cancel_booking()` (`/my_bookings/<id>/cancel`) — the paid-booking branch
  now requires a `reason` field, blocks a second open request, inserts into
  `refund_requests`, and sets the booking to `Cancel Requested` instead of
  refusing outright.
- `admin_refunds()` (`GET /admin/refunds`) — lists requests grouped by
  status, with a status filter and summary counts.
- `update_refund_status()` (`POST /admin/refunds/<id>/update-status`) —
  enforces the allowed transitions (`allowed_next` map), requires a refund
  reference to mark **Refunded** and a note to **Reject**, updates the
  linked booking, notifies the traveller, and logs the change to the new
  admin activity log.
- `get_open_refund_request(booking_id)` / `get_latest_refund_request(...)` —
  shared lookups used by the traveller's cancel form and by `my_bookings`.
- **Guard in `update_booking_status()`** (the generic admin booking-status
  route): if a booking has an open refund request, that route now refuses
  the change and points the admin at Manage Refunds instead — so a booking's
  status can no longer be changed two different ways at once.
- `admin_bookings()` — its query now includes an `EXISTS(...)` flag
  (`has_open_refund`) so the template can swap the old Approve/Keep buttons
  for a link to Manage Refunds on paid cancellation requests.

Key transition logic:

```python
allowed_next = {
    "Requested": ("Approved", "Rejected"),
    "Approved": ("Refund Pending", "Rejected"),
    "Refund Pending": ("Refunded",),
}
...
if status == "Refunded":
    execute("UPDATE bookings SET status = 'Cancelled', payment_status = 'Refunded' WHERE id = %s",
            (refund["booking_id"],))
elif status == "Rejected":
    execute("UPDATE bookings SET status = 'Confirmed' WHERE id = %s", (refund["booking_id"],))
```

### Templates

- **`templates/my_bookings.html`** — a Confirmed + Paid booking with no open
  refund request now shows a "Request cancellation & refund" disclosure with
  a reason textarea; any booking with a refund request shows its status
  (with the rejection reason or refund reference where relevant).
- **`templates/admin_bookings.html`** — a `Cancel Requested` row for a paid
  booking now shows *"This is a paid cancellation with a refund request →
  Manage Refunds"* instead of the old Approve/Keep buttons.
- **`templates/admin_refunds.html`** *(new)* — Manage Refunds page: status
  summary cards, a filter, and one action panel per request matching its
  current state (Approve/Reject, Start refund/Reject, or Mark refunded with
  a reference field).
- Sidebar: a **Refunds** link was added to `templates/_sidebar_admin.html`.

---

## 2. Admin Activity History

**Problem before:** nothing recorded *who* changed a booking, package, group
or destination, or *when* — only the current state of the row was visible.

**What it does now:** every admin action that changes one of those records
writes one row to a new activity log, and a new **Activity** page lets an
admin browse/filter that history (by area, or by admin username).

### Database

```sql
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
```

`admin_username` is stored as a snapshot (not just joined at read time) so
the history still reads sensibly if that admin account is later edited or
removed.

### Backend (`app.py`)

One helper, called from every admin mutation route:

```python
def log_admin_activity(action, entity_type, entity_id, entity_label=None,
                        old_value=None, new_value=None, notes=None):
    execute(
        """INSERT INTO admin_activity_log
               (admin_id, admin_username, action, entity_type, entity_id,
                entity_label, old_value, new_value, notes)
           VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)""",
        (session.get("user_id"), session.get("username", "unknown"), action, entity_type,
         str(entity_id), entity_label, old_value, new_value, notes),
    )
```

Wired into:

| Route | Action logged |
|---|---|
| `update_booking_status` | `booking.status_updated` |
| `update_refund_status` | `refund.status_updated` |
| `update_group_status` | `travel_group.status_updated` |
| `update_group_communication` | `travel_group.communication_updated` |
| `update_group_member_status` | `group_member.status_updated` |
| `admin_packages` (add), `update_package`, `delete_package` | `package.created` / `package.updated` / `package.deleted` |
| `add_package_photo`, `delete_package_photo` | `package.photo_added` / `package.photo_removed` |
| `manage_places` (add/edit/delete) | `place.created` / `place.updated` / `place.deleted` |
| `add_place_photo`, `delete_place_photo` | `place.photo_added` / `place.photo_removed` |

New page:

```python
@app.route("/admin/activity")
def admin_activity():
    ...
    sql = "SELECT * FROM admin_activity_log WHERE 1=1"
    # optional entity_type / admin-username filters
    sql += " ORDER BY created_at DESC, id DESC LIMIT 300"
```

### Templates

- **`templates/admin_activity.html`** *(new)* — a filterable table (When /
  Admin / Area / Action / What changed), most recent first.
- Sidebar: an **Activity** link was added to `templates/_sidebar_admin.html`.

---

## 3. Photo Galleries

**Problem before:** each package/destination had at most one image (or, for
packages, none at all) — no way to show several photos.

**What it does now:** admins can attach up to 12 photos (each picked from
the images already in `static/`, the same picker style already used for a
destination's cover photo — so no new upload/validation surface) to any
package or destination, with an optional caption. Travellers see a gallery
on the package/destination detail page.

### Database

```sql
CREATE TABLE IF NOT EXISTS package_photos (
    id INT AUTO_INCREMENT PRIMARY KEY,
    package_id INT NOT NULL,
    image VARCHAR(255) NOT NULL,
    caption VARCHAR(200) NULL,
    sort_order INT NOT NULL DEFAULT 0,
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (package_id) REFERENCES packages(id) ON DELETE CASCADE
)

CREATE TABLE IF NOT EXISTS place_photos (
    id INT AUTO_INCREMENT PRIMARY KEY,
    place_id INT NOT NULL,
    image VARCHAR(255) NOT NULL,
    caption VARCHAR(200) NULL,
    sort_order INT NOT NULL DEFAULT 0,
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (place_id) REFERENCES explore_places(id) ON DELETE CASCADE
)
```

A shared helper replaced the image-listing code that used to live only in
`manage_places()`:

```python
def list_static_images():
    return sorted(path.name for path in Path(app.static_folder).iterdir()
                  if path.is_file() and path.suffix.lower() in STATIC_IMAGE_EXTENSIONS)
```

### Backend (`app.py`)

- `add_package_photo` / `delete_package_photo`
  (`POST /admin/packages/<id>/photos/add`, `.../<photo_id>/delete`)
- `add_place_photo` / `delete_place_photo`
  (`POST /admin/places/<slug>/photos/add`, `.../<photo_id>/delete`)
- Both add routes reject anything not in `list_static_images()` and cap the
  gallery at `MAX_PHOTOS_PER_ITEM = 12`.
- `package_detail()` and `place_detail()` now also fetch that item's photos
  and pass them to the template.
- `admin_packages()` and `manage_places()` now attach each item's existing
  photos (`package["photos"]` / `place["photos"]`) for the management UI.

### Templates

- **`templates/admin_packages.html`** — each package's edit panel gained a
  "Photo gallery" disclosure: a thumbnail grid with a Remove button per
  photo, and an add-photo form (image dropdown + optional caption).
- **`templates/manage_places.html`** / **`static/manage_places.css`** — same
  pattern, added as a "Photo gallery" disclosure per destination card.
- **`templates/package_detail.html`** — a new gallery panel (only rendered
  when photos exist) above "Places covered".
- **`templates/place_detail.html`** — a new gallery strip below the hero
  image (only rendered when photos exist).

---

## Files touched

**Backend**
- `app.py` — all routes/tables/helpers described above.

**New templates**
- `templates/admin_refunds.html`
- `templates/admin_activity.html`

**Changed templates**
- `templates/my_bookings.html`
- `templates/admin_bookings.html`
- `templates/admin_packages.html`
- `templates/manage_places.html`
- `templates/package_detail.html`
- `templates/place_detail.html`
- `templates/_sidebar_admin.html`

**Changed static assets**
- `static/manage_places.css` (gallery styles)

No existing route, table column, or template block outside of what's listed
above was changed.

---

## How to try it

1. Start the app as usual; the three new tables are created automatically on
   first request (same self-migrating pattern as the existing
   `is_featured`/`payment_status` columns).
2. **Refunds:** as a traveller, pay for a booking, then use "Request
   cancellation & refund" on `/my_bookings`. As an admin, go to **Manage
   Refunds** in the sidebar to approve, start, and complete (or reject) it.
3. **Activity history:** make any booking/refund/package/group/place change
   as an admin, then open **Activity** in the sidebar.
4. **Photo galleries:** open **Manage Packages** or **Manage Places** as an
   admin, expand "Photo gallery" on any item, and add a photo from the
   dropdown. It then appears on that package's or destination's public
   detail page.

## How it was tested

Since a working dev MySQL database (`gogather`) was reachable, every new
route was exercised directly against it with Flask's test client rather than
only read by eye:

- Filed a real refund request on an existing paid booking, walked it through
  `Requested → Approved → Refund Pending → Refunded` (and confirmed the
  invalid `Requested → Refunded` jump, and marking Refunded without a
  reference, are both rejected), and checked the booking ended up
  `Cancelled` / `Refunded`.
- Confirmed `/admin/bookings` shows the "Manage Refunds" hand-off instead of
  the old buttons once a refund request is open.
- Added and removed a photo on both a package and a destination, and
  confirmed the gallery renders on `/packages/<id>` and
  `/explore_places/<slug>`.
- Confirmed `/admin/activity` recorded each of the above actions.
- All test data created during this was deleted afterwards and the booking
  used for the refund test was restored to its original `Confirmed`/`Paid`
  state — the database was left exactly as it was found.
- Ran the existing `test_places.py` suite — still passes unchanged.

**One pre-existing, unrelated issue found while testing:** `test_reports.py`
already fails on `git stash` (i.e. before any of today's work) once the
working tree's own in-progress `payment_status` changes to `admin_reports()`
are included — its mock data doesn't include a `payment_status` key. That
predates this session (it's part of the payment-integration work already in
progress, per the untracked `payment_integration_*.txt` files) and wasn't
touched here; flagging it separately since it's outside the scope of the
three features above.
