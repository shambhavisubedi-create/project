# GoGather — Project Documentation

Analysis of the codebase as it stands. No code was changed to produce this document.

## 1. What this project is

GoGather is a Flask web app for a Nepal-focused travel platform: travellers browse
destinations and packages, book trips, and form/join travel groups; admins approve
bookings, groups, and group-join requests, and manage packages/destinations/travellers.

## 2. Languages & technologies

| Layer | Technology |
|---|---|
| Backend language | **Python** (Flask web framework) |
| Frontend | **HTML** (Jinja2 templates) + inline **CSS** (mostly embedded per-page, some external `.css` files) + a little inline **JavaScript** (Highcharts config on the reports page) |
| Primary database | **MySQL** (via `mysql-connector-python`, connection pooling) — users, bookings, packages, travel_groups, group_members |
| Secondary database | **SQLite** (via Python's built-in `sqlite3`) — used *only* for the destinations/places catalogue (`instance/places.sqlite3`) |
| Charting | **Highcharts** (loaded from CDN, used on the admin Reports page) |
| Config | `.env` file read via `python-dotenv` (DB host/user/password/name) |
| Testing | Python's built-in `unittest`, with `unittest.mock.patch` to stub out the database |

No JS framework, no build step, no package manager manifest (no `requirements.txt`
was found in the repo — dependencies are installed ad hoc).

## 3. File inventory

**33 files total** (excluding `.git/` and `__pycache__/`), organized as:

### Python — application code (3 files)
| File | Lines | Purpose |
|---|---|---|
| `app.py` | 1463 | The entire Flask app: every route, all business logic, in-code data constants |
| `db.py` | 61 | MySQL connection pooling + tiny `query()` / `execute()` helpers used everywhere |
| `place_store.py` | 55 | SQLite-backed CRUD for the destinations catalogue (separate from MySQL) |

### Python — tests (3 files)
| File | Lines | Purpose |
|---|---|---|
| `test_connection.py` | 32 | Sanity check that a DB connection can be made |
| `test_places.py` | 82 | Tests for the places/destinations feature |
| `test_reports.py` | 78 | Tests for the admin Reports page, with `app.query` mocked out |

### HTML templates (21 files, `templates/`)
| File | Lines | Purpose |
|---|---|---|
| `landing.html` | 197 | Public homepage |
| `login.html` | 147 | Login form |
| `register.html` | 181 | Signup form |
| `dashboard.html` | 196 | Traveller home after login |
| `admin_dashboard.html` | 227 | Admin home after login |
| `packages.html` | 297 | Browse/search real packages (MySQL-backed) |
| `package_detail.html` | 215 | Single package detail + itinerary blurb |
| `book_package.html` | 207 | Booking form |
| `my_bookings.html` | 254 | Traveller's own bookings list + cancel |
| `explore_places.html` | 369 | Browse destinations (SQLite-backed) |
| `place_detail.html` | 226 | Single destination detail |
| `travel_groups.html` | 437 | Create/search/join travel groups |
| `profile.html` | 115 | Traveller's own profile |
| `admin_packages.html` | 209 | Admin CRUD for packages |
| `admin_bookings.html` | 268 | Admin: view/search/filter/approve bookings |
| `admin_travel_groups.html` | 204 | Admin: approve/reject groups + member join requests |
| `admin_reports.html` | 93 | Admin analytics dashboard (charts + stats) |
| `manage_places.html` | 59 | Admin CRUD for destinations |
| `manage_travellers.html` | 180 | Admin: view/edit/delete traveller accounts |

### Static assets (6 files, `static/`)
`admin_reports.css`, `manage_places.css`, and 4 images (`ktm.webp`, `lumbini.jpg`,
`pict1.jpg`, `nepal-hero.svg`).

### Data / config (3 files)
| File | Purpose |
|---|---|
| `.env` | DB connection settings (host, port, user, db name — password left blank/local) |
| `.gitignore` | Excludes `__pycache__`, `.env`, etc. from git |
| `instance/places.sqlite3` | The live SQLite database file for destinations (auto-created on first run) |

## 4. How data is stored — two separate databases

This is the single most important thing to understand about this codebase:

- **MySQL** (`gogather` database) holds everything "core": `users`, `packages`,
  `bookings`, `travel_groups`, `group_members`. Accessed through `db.py`'s
  `query()`/`execute()` helpers everywhere in `app.py`.
- **SQLite** (`instance/places.sqlite3`) holds only the destinations catalogue.
  It's seeded once from the hardcoded `PLACES` list in `app.py` (lines ~59–70),
  then becomes the actual source of truth — edits via "Manage Places" change the
  SQLite file, not the Python list.
- `FEATURED_PACKAGES` (top of `app.py`) and `PACKAGE_DETAILS` are **hardcoded
  Python dicts**, not database-backed. `FEATURED_PACKAGES` is decorative sample
  data shown on the traveller dashboard; `PACKAGE_DETAILS` supplies extra
  itinerary text merged onto real MySQL package rows on the detail page.

## 5. Roles & access control

Two roles, stored as `session["user"]`:
- `0` = Traveller
- `1` = Admin

Nearly every route starts with the same guard pattern: check `"username" in
session`, then check the role matches, else redirect. There's no formal
decorator/middleware for this — it's repeated inline in each view function.

## 6. Application workflow (by feature)

### Auth
`/` (landing) → `/register` → `/login` → routes to `/dashboard` (traveller) or
`/admin-dashboard` (admin) based on role → `/logout` clears the session.

### Traveller: Packages & Booking
1. `/packages` — search/filter real packages from MySQL.
2. `/packages/<id>` — detail view, itinerary blurb merged in from `PACKAGE_DETAILS`.
3. `/book_package` — submit a booking; phone number is always pulled from the
   traveller's own account server-side (not trusted from the form, to prevent
   phone spoofing — see commit history).
4. `/my_bookings` — list own bookings; `/my_bookings/<id>/cancel` — a Pending
   booking cancels immediately; a Confirmed one becomes "Cancel Requested" and
   needs admin sign-off.

### Traveller: Destinations
`/explore_places` (SQLite-backed list) → `/explore_places/<slug>` (detail).

### Traveller: Travel Groups
1. `/travel_groups` — browse/search groups; only `Confirmed` groups (plus your
   own, regardless of status) are visible.
2. `/travel_groups/create` — creates a group with `status = 'Pending'`; creator
   is auto-added to `group_members` as `Confirmed` (no self-approval needed).
3. `/travel_groups/<id>/join` — inserts a `group_members` row with
   `status = 'Pending'`; the traveller is **not** actually a member yet.
4. Admin approval gate (two levels, both required before someone is truly "in"
   a group):
   - `/admin/travel_groups` + `update-status` — admin confirms/rejects the
     **group itself**.
   - `/admin/group_members/<id>/update-status` — admin confirms/rejects each
     **individual join request**. Only after this does the traveller show as
     a member and count toward `max_members`.

### Admin
- `/admin-dashboard` — overview stats.
- `/admin/places` (`manage_places.html`) — CRUD on the SQLite destinations store.
- `/admin/packages` — CRUD on MySQL `packages`.
- `/admin/bookings` — search/filter/approve/decline bookings and cancellation
  requests.
- `/admin/travel_groups` — approve/reject groups and, now, member join requests.
- `/admin/travellers` — view/edit/delete traveller accounts.
- `/admin/reports` — analytics: booking totals, revenue (`accepted_value`),
  activity-over-time and status-breakdown charts (Highcharts), top 5 packages,
  and platform-wide counts (travellers/destinations/packages/groups).

## 7. Notable patterns worth knowing

- **Runtime schema migration**: `ensure_group_columns()` in `app.py` runs at
  startup (only inside `if __name__ == "__main__":`) and does `DESCRIBE` +
  `ALTER TABLE ... ADD COLUMN` if a column is missing — there's no formal
  migrations folder or tool; schema changes are applied this way, by hand, once.
  ⚠️ This block only executes when the app is launched via `python app.py`
  directly — if it's ever run another way (e.g. `flask run`), new columns won't
  get added automatically.
- **Approval-gate pattern repeats three times**: bookings (Pending → Confirmed/
  Cancel Requested/Cancelled), travel groups (Pending → Confirmed/Rejected),
  and now group membership (Pending → Confirmed/Rejected) — all admin-gated to
  prevent unreviewed content/fraud from going live.
- **Two independent "id-less" data sources** feed into pages that otherwise look
  DB-backed: `FEATURED_PACKAGES` (dashboard teaser) and the original `PLACES`
  seed list — worth remembering these don't reflect live edits.
