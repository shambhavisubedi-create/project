from flask import Flask, abort, flash, render_template, request, redirect, url_for, session
import re
import secrets
import sqlite3
from pathlib import Path
from dotenv import load_dotenv
from datetime import date, timedelta
from mysql.connector import IntegrityError
from werkzeug.security import check_password_hash, generate_password_hash

from db import close_db, execute, query
from place_store import get_places, save_place, remove_place

load_dotenv()

app = Flask(__name__)

app.secret_key = "any-random-string-goes-here"
app.teardown_appcontext(close_db)

          
# MYSQL DATABASE CONNECTION
# DEMO USERS
# user = 0 → Traveller
# user = 1 → Admin


# FEATURED PACKAGES


FEATURED_PACKAGES = [
    {
        "name": "Pokhara & Kathmandu Heritage Tour",
        "duration_days": 5,
        "hotel_type": "standard",
        "transport_mode": "bus",
        "price": 32500
    },

    {
        "name": "Chitwan Wildlife Safari",
        "duration_days": 3,
        "hotel_type": "budget",
        "transport_mode": "bus",
        "price": 15500
    },

    {
        "name": "Everest Base Camp Trek",
        "duration_days": 14,
        "hotel_type": "standard",
        "transport_mode": "flight",
        "price": 185000
    }
]


# Nepal destinations shown in the traveller's Explore Places section.
PLACES = [
    {"slug": "kathmandu-valley", "name": "Kathmandu Valley", "region": "Bagmati", "category": "Cultural", "budget": "Moderate", "days": "2–3 days", "weather": "Mild", "best_time": "October–December, February–April", "image": "ktm.webp", "description": "A living heritage valley of courtyards, stupas, temples and vibrant local markets.", "highlights": ["Swayambhunath Stupa", "Patan Durbar Square", "Boudhanath"], "activities": ["Heritage walks", "Food tours", "Photography"]},
    {"slug": "pokhara", "name": "Pokhara", "region": "Gandaki", "category": "Adventure", "budget": "Moderate", "days": "3–4 days", "weather": "Mild", "best_time": "September–November, March–May", "image": "pict1.jpg", "description": "A relaxed lakeside city framed by Annapurna views, perfect for soft adventure and slow mornings.", "highlights": ["Phewa Lake", "World Peace Pagoda", "Sarangkot"], "activities": ["Boating", "Paragliding", "Café hopping"]},
    {"slug": "chitwan", "name": "Chitwan National Park", "region": "Bagmati", "category": "Wildlife", "budget": "Moderate", "days": "2–3 days", "weather": "Warm", "best_time": "October–March", "image": "pict1.jpg", "description": "A subtropical escape for guided jungle safaris, Tharu culture and quiet riverside sunsets.", "highlights": ["Jeep safari", "Rapti River", "Tharu villages"], "activities": ["Wildlife safari", "Canoeing", "Cultural show"]},
    {"slug": "lumbini", "name": "Lumbini", "region": "Lumbini", "category": "Cultural", "budget": "Budget", "days": "1–2 days", "weather": "Warm", "best_time": "October–March", "image": "lumbini.jpg", "description": "The birthplace of Lord Buddha, offering peaceful monastic zones and reflective cultural travel.", "highlights": ["Maya Devi Temple", "Sacred Garden", "Monastic Zone"], "activities": ["Pilgrimage walk", "Meditation", "Cycling"]},
    {"slug": "everest-region", "name": "Everest Region", "region": "Koshi", "category": "Trekking", "budget": "Premium", "days": "12–16 days", "weather": "Cold", "best_time": "March–May, October–November", "image": "pict1.jpg", "description": "An unforgettable high-Himalayan journey through Sherpa villages, suspension bridges and mountain panoramas.", "highlights": ["Namche Bazaar", "Tengboche", "Everest Base Camp"], "activities": ["Trekking", "Mountain photography", "Sherpa culture"]},
    {"slug": "nagarkot", "name": "Nagarkot", "region": "Bagmati", "category": "Nature", "budget": "Budget", "days": "1–2 days", "weather": "Cool", "best_time": "October–April", "image": "ktm.webp", "description": "A hilltop retreat near Kathmandu known for Himalayan sunrises and calm weekend stays.", "highlights": ["Sunrise viewpoint", "Panoramic trails", "Bhaktapur nearby"], "activities": ["Hiking", "Sunrise viewing", "Relaxation"]},
    {"slug": "bandipur", "name": "Bandipur", "region": "Gandaki", "category": "Cultural", "budget": "Moderate", "days": "1–2 days", "weather": "Mild", "best_time": "September–May", "image": "pict1.jpg", "description": "A beautifully preserved hill town where Newari architecture meets wide views of the central Himalaya.", "highlights": ["Bandipur Bazaar", "Tundikhel", "Siddha Cave"], "activities": ["Village walks", "Cave visit", "Local cuisine"]},
    {"slug": "mustang", "name": "Upper Mustang", "region": "Gandaki", "category": "Adventure", "budget": "Premium", "days": "7–10 days", "weather": "Dry", "best_time": "May–October", "image": "pict1.jpg", "description": "A dramatic trans-Himalayan landscape of ochre cliffs, ancient monasteries and walled villages.", "highlights": ["Lo Manthang", "Muktinath", "Kali Gandaki Valley"], "activities": ["Jeep tour", "Trekking", "Monastery visits"]},
    {"slug": "rara-lake", "name": "Rara Lake", "region": "Karnali", "category": "Nature", "budget": "Moderate", "days": "5–7 days", "weather": "Cool", "best_time": "April–June, September–October", "image": "pict1.jpg", "description": "A remote alpine lake with clear blue water, pine forests and some of Nepal's most peaceful scenery.", "highlights": ["Rara National Park", "Lakeside trail", "Talcha"], "activities": ["Hiking", "Camping", "Nature photography"]},
    {"slug": "janakpur", "name": "Janakpur", "region": "Madhesh", "category": "Cultural", "budget": "Budget", "days": "1–2 days", "weather": "Warm", "best_time": "October–March", "image": "lumbini.jpg", "description": "A colourful Mithila city centred on Janaki Mandir, sacred tradition and renowned local art.", "highlights": ["Janaki Mandir", "Mithila art", "Ganga Sagar"], "activities": ["Temple visit", "Art workshop", "Food exploration"]},
]


# Extra itinerary information displayed with the packages stored in MySQL.
PACKAGE_DETAILS = {
    1: {"tag": "Culture & lakes", "summary": "A balanced city and lakeside escape combining Kathmandu's heritage with Pokhara's mountain views.", "places": ["Kathmandu", "Patan", "Pokhara", "Phewa Lake"], "activities": ["Heritage walk", "Lakeside boating", "Sunrise viewpoint"], "includes": ["Hotel stay", "Private transfers", "Daily breakfast", "Local guide"]},
    2: {"tag": "Wildlife escape", "summary": "A relaxed jungle break with guided wildlife experiences and a warm introduction to Tharu culture.", "places": ["Sauraha", "Chitwan National Park", "Rapti River"], "activities": ["Jeep safari", "Canoe ride", "Tharu cultural evening"], "includes": ["Hotel stay", "Safari permits", "Breakfast and dinner", "Naturalist guide"]},
    3: {"tag": "High Himalayan trek", "summary": "A carefully paced Everest journey through Sherpa villages, dramatic valleys and iconic mountain viewpoints.", "places": ["Lukla", "Namche Bazaar", "Tengboche", "Everest Base Camp"], "activities": ["Guided trek", "Acclimatisation hikes", "Mountain photography"], "includes": ["Domestic flights", "Teahouse stays", "Trekking guide", "Required permits"]},
    5: {"tag": "Pilgrimage", "summary": "A short, peaceful visit to the birthplace of Lord Buddha and its surrounding monastic zone.", "places": ["Maya Devi Temple", "Sacred Garden", "Monastic Zone"], "activities": ["Pilgrimage walk", "Meditation session", "Cycling tour"], "includes": ["Hotel stay", "Private transfers", "Daily breakfast", "Local guide"]},
    6: {"tag": "Hilltop retreat", "summary": "A quick weekend escape near Kathmandu built around Himalayan sunrise views.", "places": ["Nagarkot viewpoint", "Panoramic trails"], "activities": ["Sunrise viewing", "Short hikes", "Photography"], "includes": ["Hotel stay", "Jeep transfers", "Daily breakfast"]},
    7: {"tag": "Heritage town", "summary": "A relaxed walk through a beautifully preserved Newari hill town with sweeping Himalayan views.", "places": ["Bandipur Bazaar", "Tundikhel", "Siddha Cave"], "activities": ["Village walks", "Cave visit", "Local cuisine tasting"], "includes": ["Hotel stay", "Private transfers", "Daily breakfast", "Local guide"]},
    8: {"tag": "Remote alpine lake", "summary": "A trek into Nepal's far west to reach one of the country's most untouched alpine lakes.", "places": ["Rara National Park", "Lakeside trail", "Talcha"], "activities": ["Guided trekking", "Camping", "Nature photography"], "includes": ["Domestic flights", "Camping/lodge stays", "Trekking guide", "Required permits"]},
    9: {"tag": "Trans-Himalayan jeep tour", "summary": "A dramatic jeep expedition into the walled villages and ochre cliffs of Upper Mustang.", "places": ["Lo Manthang", "Muktinath", "Kali Gandaki Valley"], "activities": ["Jeep touring", "Monastery visits", "Cultural walks"], "includes": ["4WD jeep transport", "Lodge stays", "Local guide", "Required permits"]},
    10: {"tag": "Cultural city break", "summary": "A colourful introduction to Mithila art and tradition centred on the Janaki Mandir.", "places": ["Janaki Mandir", "Ganga Sagar"], "activities": ["Temple visit", "Mithila art workshop", "Food exploration"], "includes": ["Hotel stay", "Private transfers", "Daily breakfast"]},
}


# FORMAT NPR

def format_npr(amount):
    return "Rs {:,.0f}".format(amount)


app.jinja_env.filters["npr"] = format_npr


def ensure_group_columns():
    """Add the optional group fields used by the Travel Groups feature."""
    columns = {row["Field"] for row in query("DESCRIBE travel_groups")}
    if "budget_range" not in columns:
        execute("ALTER TABLE travel_groups ADD COLUMN budget_range VARCHAR(50) NULL")
    if "description" not in columns:
        execute("ALTER TABLE travel_groups ADD COLUMN description TEXT NULL")
    if "status" not in columns:
        execute("ALTER TABLE travel_groups ADD COLUMN status VARCHAR(20) NOT NULL DEFAULT 'Pending'")

    member_columns = {row["Field"] for row in query("DESCRIBE group_members")}
    if "status" not in member_columns:
        # Existing memberships predate this feature, so grandfather them in as
        # Confirmed rather than making everyone re-request to join.
        execute("ALTER TABLE group_members ADD COLUMN status VARCHAR(20) NOT NULL DEFAULT 'Confirmed'")


def ensure_notifications_table():
    execute(
        """
        CREATE TABLE IF NOT EXISTS notifications (
            id INT AUTO_INCREMENT PRIMARY KEY,
            user_id INT NOT NULL,
            message VARCHAR(255) NOT NULL,
            link VARCHAR(255) NULL,
            is_read TINYINT(1) NOT NULL DEFAULT 0,
            created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
        )
        """
    )


def notify(user_id, message, link=None):
    execute("INSERT INTO notifications (user_id, message, link) VALUES (%s, %s, %s)", (user_id, message[:255], link))


@app.before_request
def prepare_notifications():
    # Also runs under `flask run`, where the __main__ startup block is skipped.
    if "user_id" in session and not app.config.get("NOTIFICATIONS_READY"):
        ensure_notifications_table()
        app.config["NOTIFICATIONS_READY"] = True


def notify_admins(message, link=None):
    for admin in query("SELECT id FROM users WHERE role = 1"):
        notify(admin["id"], message, link)


@app.context_processor
def inject_notifications():
    if "user_id" not in session:
        return {}
    unread_notification_count = query(
        "SELECT COUNT(*) AS total FROM notifications WHERE user_id = %s AND is_read = 0",
        (session["user_id"],), fetch="one",
    )["total"]
    return {"unread_notification_count": unread_notification_count}


# LANDING PAGE

@app.route("/")
def landing():
    return render_template("landing.html")


# LOGIN PAGE

@app.route("/login", methods=["GET", "POST"])
def login():

    error = None

    if request.method == "POST":

        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")

        # The form accepts either a username or email address.
        logged_in_user = query(
            """
            SELECT id, username, password_hash, role
            FROM users
            WHERE username = %s OR email = %s
            LIMIT 1
            """,
            (username, username),
            fetch="one",
        )

        # LOGIN SUCCESSFUL

        if logged_in_user and check_password_hash(
            logged_in_user["password_hash"], password
        ):

            # Store login information
            session["user_id"] = logged_in_user["id"]
            session["username"] = logged_in_user["username"]
            session["user"] = logged_in_user["role"]

            # USER = 0 → TRAVELLER

            if logged_in_user["role"] == 0:

                return redirect(
                    url_for("dashboard")
                )

            
            # USER = 1 → ADMIN

            elif logged_in_user["role"] == 1:

                return redirect(
                    url_for("admin_dashboard")
                )

        # LOGIN FAILED

        else:

            error = "Invalid username or password."

    return render_template(
        "login.html",
        error=error
    )


# REGISTER PAGE

@app.route("/register", methods=["GET", "POST"])
def register():

    error = None

    if request.method == "POST":

        full_name = request.form.get(
            "full_name", ""
        ).strip()

        username = request.form.get(
            "username", ""
        ).strip()

        email = request.form.get(
            "email", ""
        ).strip()

        phone = request.form.get("phone", "").strip()

        password = request.form.get(
            "password", ""
        )

        confirm_password = request.form.get(
            "confirm_password", ""
        )

        # REQUIRED FIELDS

        if not (
            full_name
            and username
            and email
            and password
            and confirm_password
        ):

            error = "Please fill in every required field."

        # PASSWORD MATCH

        elif password != confirm_password:

            error = "Password and Confirm Password don't match."

        # PASSWORD LENGTH

        elif len(password) < 8:

            error = "Password must be at least 8 characters long."

        # PHONE FORMAT

        elif phone and not (phone.isdigit() and len(phone) == 10):

            error = "Phone number must be a 10 digit number."

        else:

            try:

                # New registrations are always travellers (role = 0).

                execute(
                    """
                    INSERT INTO users
                        (full_name, username, email, phone, password_hash, role)
                    VALUES (%s, %s, %s, %s, %s, %s)
                    """,
                    (
                        full_name,
                        username,
                        email,
                        phone or None,
                        generate_password_hash(password),
                        0,
                    ),
                )

            except IntegrityError as e:

                # MySQL tells us which unique column caused the clash

                if "phone" in str(e):

                    error = "That phone number is already registered."

                else:

                    error = "That username or email address is already registered."

            else:

                return redirect(url_for("login"))

    return render_template(
        "register.html",
        error=error
    )


# TRAVELLER DASHBOARD

@app.route("/dashboard")
def dashboard():

    # User must be logged in
    if "username" not in session:

        return redirect(
            url_for("login")
        )

    # Only Traveller can access this page
    if session.get("user") != 0:

        return redirect(
            url_for("admin_dashboard")
        )

    # Counts are pulled live from the traveller's own data, not hardcoded.

    bookings_row = query(
        "SELECT COUNT(*) AS total FROM bookings WHERE user_id = %s",
        (session["user_id"],),
        fetch="one",
    )

    groups_row = query(
        "SELECT COUNT(*) AS total FROM group_members WHERE user_id = %s",
        (session["user_id"],),
        fetch="one",
    )

    return render_template(
        "dashboard.html",

        my_bookings_count=bookings_row["total"],

        my_groups_count=groups_row["total"],

        featured_packages=FEATURED_PACKAGES,

        popular_destinations=get_places(PLACES)[:4],

        username=session["username"]
    )


# ADMIN DASHBOARD

@app.route("/admin-dashboard")
def admin_dashboard():

    # User must be logged in
    if "username" not in session:

        return redirect(
            url_for("login")
        )

    # Only Admin can access this page
    if session.get("user") != 1:

        return redirect(
            url_for("dashboard")
        )

    # These stat cards used to show made-up numbers. Now they're a live
    # count straight from the tables they claim to describe.

    total_travellers = query(
        "SELECT COUNT(*) AS total FROM users WHERE role = 0", fetch="one"
    )["total"]

    packages = query(
        "SELECT id, name, duration_days, hotel_type, transport_mode, price FROM packages ORDER BY price"
    )

    total_packages = len(packages)

    total_bookings = query(
        "SELECT COUNT(*) AS total FROM bookings", fetch="one"
    )["total"]

    total_groups = query(
        "SELECT COUNT(*) AS total FROM travel_groups", 
        fetch="one"
    )["total"]

    # Items waiting on an admin decision, so the dashboard surfaces work to
    # do instead of just repeating the sidebar's navigation links.

    pending_bookings = query(
        "SELECT COUNT(*) AS total FROM bookings WHERE status = 'Pending'", fetch="one"
    )["total"]

    cancel_requests = query(
        "SELECT COUNT(*) AS total FROM bookings WHERE status = 'Cancel Requested'", fetch="one"
    )["total"]

    pending_groups = query(
        "SELECT COUNT(*) AS total FROM travel_groups WHERE status = 'Pending'", fetch="one"
    )["total"]

    pending_group_members = query(
        "SELECT COUNT(*) AS total FROM group_members WHERE status = 'Pending'", fetch="one"
    )["total"]

    recent_travellers = query(
        """
        SELECT full_name, username, email, created_at
        FROM users
        WHERE role = 0
        ORDER BY created_at DESC
        LIMIT 5
        """
    )

    # Traveller performance is worked out from each traveller's own booking
    # history (no separate rating step needed -- a booking's status already
    # tells us whether the trip went ahead, so we can score off that alone).

    traveller_performance = query(
        """
        SELECT u.full_name, u.username,
               COUNT(b.id) AS total_bookings,
               SUM(b.status = 'Completed') AS completed_bookings,
               SUM(b.status IN ('Cancelled', 'Cancel Requested')) AS cancelled_bookings,
               SUM(CASE WHEN b.status IN ('Confirmed', 'Completed') THEN b.price ELSE 0 END) AS total_spend,
               MAX(b.created_at) AS last_booking
        FROM users u
        JOIN bookings b ON b.user_id = u.id
        WHERE u.role = 0
        GROUP BY u.id
        ORDER BY total_spend DESC, total_bookings DESC
        LIMIT 8
        """
    )

    for row in traveller_performance:
        row["completion_rate"] = round(
            100 * row["completed_bookings"] / row["total_bookings"]
        )

    return render_template(
        "admin_dashboard.html",

        username=session["username"],

        featured_packages=packages,

        total_travellers=total_travellers,

        total_packages=total_packages,

        total_bookings=total_bookings,

        total_groups=total_groups,

        pending_bookings=pending_bookings,

        cancel_requests=cancel_requests,

        pending_groups=pending_groups,

        pending_group_members=pending_group_members,

        recent_travellers=recent_travellers,

        traveller_performance=traveller_performance
    )


PLACE_CATEGORIES = ("Cultural", "Adventure", "Wildlife", "Trekking", "Nature")
PLACE_BUDGETS = ("Budget", "Moderate", "Premium")


@app.route("/admin/places", methods=["GET", "POST"])
def manage_places():
    if "username" not in session:
        return redirect(url_for("login"))
    if session.get("user") != 1:
        return redirect(url_for("dashboard"))

    session.setdefault("places_csrf", secrets.token_hex(32))
    all_places = get_places(PLACES)
    images = sorted(path.name for path in Path(app.static_folder).iterdir()
                    if path.suffix.lower() in (".jpg", ".jpeg", ".png", ".webp"))
    selected = request.args.get("edit")
    editing = next((place for place in all_places if place["slug"] == selected), None)
    if selected and editing is None:
        abort(404)
    form = dict(editing) if editing else {}
    error = None
    if request.method == "POST":
        if not secrets.compare_digest(session["places_csrf"], request.form.get("csrf_token", "")):
            abort(400)
        action = request.form.get("action")
        slug = request.form.get("slug", "")
        existing = next((place for place in all_places if place["slug"] == slug), None)
        if action not in ("add", "edit", "delete"):
            abort(400)
        if action in ("edit", "delete") and existing is None:
            abort(404)
        if action == "delete":
            remove_place(PLACES, slug)
            flash(f'{existing["name"]} has been removed.', "success")
            return redirect(url_for("manage_places"))
        editing = existing if action == "edit" else None
        fields = ("name", "region", "category", "budget", "days", "weather",
                  "best_time", "image", "description", "highlights", "activities")
        form = {field: request.form.get(field, "").strip() for field in fields}
        form["slug"] = slug if editing else re.sub(r"[^a-z0-9]+", "-", form["name"].lower()).strip("-")
        if not all(form[field] for field in fields) or not form["slug"]:
            error = "Please fill in every field so travellers have the details they need."
        elif any(len(form[field]) > (2000 if field == "description" else 500) for field in fields):
            error = "Please keep the description under 2,000 characters and other fields under 500."
        elif form["category"] not in PLACE_CATEGORIES or form["budget"] not in PLACE_BUDGETS or form["image"] not in images:
            error = "Please choose a category, budget and cover photo from the available options."
        for field in ("highlights", "activities"):
            form[field] = [value.strip() for value in form[field].split(",") if value.strip()]
            if not form[field]:
                error = "Add at least one highlight and one activity."
        if error is None:
            try:
                save_place(PLACES, form, creating=action == "add")
            except sqlite3.IntegrityError:
                error = "A destination with this name already exists. Please choose another name."
            else:
                flash(f'{form["name"]} has been {"updated" if editing else "added"}.', "success")
                return redirect(url_for("manage_places"))

    search = request.args.get("search", "").strip()
    places = [
        place for place in all_places
        if search.casefold() in " ".join(
            (place["name"], place["region"], place["category"])
        ).casefold()
    ]
    return render_template("manage_places.html", places=places, search=search,
                           total=len(all_places), regions=len({p["region"] for p in all_places}),
                           categories=PLACE_CATEGORIES, budgets=PLACE_BUDGETS, images=images,
                           form=form, editing=editing, error=error,
                           show_form=bool(error or editing or request.args.get("add"))), (400 if error else 200)


# TRAVEL PACKAGES - TRAVELLER

@app.route("/packages")
def packages():

    # User must be logged in
    if "username" not in session:

        return redirect(
            url_for("login")
        )

    # Only Traveller
    if session.get("user") != 0:

        return redirect(
            url_for("admin_dashboard")
        )

    search = request.args.get("search", "").strip()
    hotel_type = request.args.get("hotel_type", "")
    transport_mode = request.args.get("transport_mode", "")

    sql = "SELECT id, name, duration_days, hotel_type, transport_mode, price FROM packages WHERE 1=1"
    params = []
    if search:
        sql += " AND name LIKE %s"
        params.append(f"%{search}%")
    if hotel_type:
        sql += " AND hotel_type = %s"
        params.append(hotel_type)
    if transport_mode:
        sql += " AND transport_mode = %s"
        params.append(transport_mode)
    sql += " ORDER BY price ASC"

    package_rows = query(sql, tuple(params))
    for package in package_rows:
        package.update(PACKAGE_DETAILS.get(package["id"], {
            "tag": "Nepal experience",
            "summary": "A thoughtfully planned Nepal trip.",
            "places": [], "activities": [], "includes": [],
        }))

    return render_template(
        "packages.html", packages=package_rows, search=search,
        selected_hotel=hotel_type, selected_transport=transport_mode,
    )


@app.route("/packages/<int:package_id>")
def package_detail(package_id):
    if "username" not in session:
        return redirect(url_for("login"))
    if session.get("user") != 0:
        return redirect(url_for("admin_dashboard"))

    package = query(
        "SELECT id, name, duration_days, hotel_type, transport_mode, price FROM packages WHERE id = %s",
        (package_id,), fetch="one",
    )
    if package is None:
        return redirect(url_for("packages"))
    package.update(PACKAGE_DETAILS.get(package_id, {
        "tag": "Nepal experience", "summary": "A thoughtfully planned Nepal trip.",
        "places": [], "activities": [], "includes": [],
    }))
    return render_template("package_detail.html", package=package)


@app.route("/explore_places")
def explore_places():

    if "username" not in session:
        return redirect(url_for("login"))

    # Only traveller can access this page
    if session.get("user") != 0:
        return redirect(url_for("admin_dashboard"))

    search = request.args.get("search", "").strip().lower()
    category = request.args.get("category", "")
    budget = request.args.get("budget", "")

    places = [
        place for place in get_places(PLACES)
        if (not search or search in place["name"].lower()
            or search in place["region"].lower()
            or search in place["description"].lower())
        and (not category or place["category"] == category)
        and (not budget or place["budget"] == budget)
    ]

    return render_template(
        "explore_places.html",
        places=places,
        search=search,
        selected_category=category,
        selected_budget=budget,
    )


@app.route("/explore_places/<slug>")
def place_detail(slug):
    if "username" not in session:
        return redirect(url_for("login"))
    if session.get("user") != 0:
        return redirect(url_for("admin_dashboard"))

    all_places = get_places(PLACES)
    place = next((item for item in all_places if item["slug"] == slug), None)
    if place is None:
        return redirect(url_for("explore_places"))

    similar_places = [
        item for item in all_places
        if item["category"] == place["category"] and item["slug"] != slug
    ][:3]
    return render_template(
        "place_detail.html", place=place, similar_places=similar_places
    )


# BOOK PACKAGE


# Trips can be planned ahead, but not decades ahead -- two years covers any
# realistic advance booking.
MAX_BOOKING_DAYS_AHEAD = 180


def has_overlapping_booking(user_id, travel_date, duration_days):
    """A traveller can hold as many bookings as they like, just not two
    that put them in two places on the same day."""

    new_start = travel_date
    new_end = travel_date + timedelta(days=duration_days - 1)

    active_bookings = query(
        """
        SELECT b.travel_date, COALESCE(p.duration_days, 1) AS duration_days
        FROM bookings b
        LEFT JOIN packages p ON p.id = b.package_id
        WHERE b.user_id = %s AND b.status != 'Cancelled'
        """,
        (user_id,),
    )

    for booking in active_bookings:
        existing_start = booking["travel_date"]
        existing_end = existing_start + timedelta(days=booking["duration_days"] - 1)
        if new_start <= existing_end and existing_start <= new_end:
            return True

    return False


@app.route("/book_package", methods=["GET", "POST"])
def book_package():

    if "username" not in session:
        return redirect(url_for("login"))

    if session.get("user") != 0:
        return redirect(url_for("admin_dashboard"))

    package_id = request.values.get("package_id", type=int)

    package = query(
        "SELECT id, name, duration_days, hotel_type, transport_mode, price FROM packages WHERE id = %s",
        (package_id,), fetch="one",
    ) if package_id else None

    max_travel_date = (date.today() + timedelta(days=MAX_BOOKING_DAYS_AHEAD)).isoformat()

    # The contact number always comes from the traveller's own account, never
    # from the submitted form -- otherwise anyone could book a trip under
    # someone else's phone number just by typing it into the field.
    traveller_phone = query(
        "SELECT phone FROM users WHERE id = %s", (session["user_id"],), fetch="one"
    )["phone"]

    if not traveller_phone:
        flash("Please add a phone number to your profile before booking a package.", "error")
        return redirect(url_for("profile"))

    if request.method == "POST":

        travel_date = request.form.get("travel_date", "")
        phone=request.form.get("phone","").strip()
        phone = traveller_phone

        try:
            travellers = int(request.form.get("travellers", "0"))
        except ValueError:
            travellers = 0

        # A booking needs a real package, a date that's neither already
        # passed nor unrealistically far out, and a sensible traveller count.

        if (
            package is None
            or travel_date < date.today().isoformat()
            or travel_date > max_travel_date
            or not phone
            or not 1 <= travellers <= 20
        ):

            flash("Please complete all booking details with a valid travel date within the next 6 month years.", "error")

        elif has_overlapping_booking(session["user_id"], date.fromisoformat(travel_date), package["duration_days"]):

            # You can't be in two places at once, so a traveller's active
            # bookings (cancelled ones don't count) can't share any days.

            flash("You already have a trip booked over these dates. You can only book one package at a time for a given period.", "error")

        else:

            execute(
                """
                INSERT INTO bookings
                    (user_id, package_id, package_name, travel_date, travellers, phone, price, status)
                VALUES (%s, %s, %s, %s, %s, %s, %s, 'Pending')
                """,
                (
                    session["user_id"],
                    package["id"],
                    package["name"],
                    travel_date,
                    travellers,
                    phone,
                    package["price"] * travellers,
                ),
            )

            notify_admins(f"{session['username']} requested a booking for '{package['name']}'.", url_for("admin_bookings"))
            notify(session["user_id"], f"Your booking for '{package['name']}' is awaiting admin confirmation.", url_for("my_bookings"))
            flash("Your booking request has been submitted and is awaiting admin confirmation.", "success")

            return redirect(url_for("my_bookings"))

    return render_template(
        "book_package.html",
        package=package,
        packages=query("SELECT id, name, duration_days, price FROM packages ORDER BY price"),
        today=date.today().isoformat(),
        max_travel_date=max_travel_date,
        traveller_phone=traveller_phone,
    )


# MY BOOKINGS


@app.route("/my_bookings")
def my_bookings():

    if "username" not in session:
        return redirect(url_for("login"))

    if session.get("user") != 0:
        return redirect(url_for("admin_dashboard"))

    bookings = query(
        """
        SELECT id, package_name, travel_date, travellers, phone, price, status, created_at
        FROM bookings
        WHERE user_id = %s
        ORDER BY created_at DESC
        """,
        (session["user_id"],),
    )

    # Split into what's still ahead versus what the traveller has already
    # been on, so the page can show trip history separately from upcoming
    # bookings.

    today = date.today()

    upcoming_bookings = [b for b in bookings if b["travel_date"] >= today]
    past_bookings = [b for b in bookings if b["travel_date"] < today]

    return render_template(
        "my_bookings.html",
        upcoming_bookings=upcoming_bookings,
        past_bookings=past_bookings,
    )


@app.route("/my_bookings/<int:booking_id>/cancel", methods=["POST"])
def cancel_booking(booking_id):

    if "user_id" not in session or session.get("user") != 0:
        return redirect(url_for("login"))

    booking = query(
        "SELECT id, status FROM bookings WHERE id = %s AND user_id = %s",
        (booking_id, session["user_id"]), fetch="one",
    )

    if booking is None:
        flash("Booking not found.", "error")

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


# TRAVEL GROUPS
MAX_GROUP_DAYS_AHEAD = 180
@app.route("/travel_groups")
def travel_groups():

    if "username" not in session:
        return redirect(url_for("login"))

    if session.get("user") != 0:
        return redirect(url_for("admin_dashboard"))

    destination = request.args.get("destination", "").strip()
    sql = """
        SELECT tg.id, tg.name, tg.destination, tg.travel_date, tg.max_members,
               tg.budget_range, tg.description, tg.status, u.username AS created_by,
               SUM(gm.status = 'Confirmed') AS member_count,
               SUM(gm.user_id = %s AND gm.status = 'Confirmed') AS has_joined,
               SUM(gm.user_id = %s AND gm.status = 'Pending') AS has_requested
        FROM travel_groups tg
        JOIN users u ON u.id = tg.created_by
        LEFT JOIN group_members gm ON gm.group_id = tg.id
        WHERE tg.travel_date >= CURDATE()
          AND (tg.status = 'Confirmed' OR tg.created_by = %s)
    """
    # you can always see your own group, even before it's approved
    params = [session["user_id"], session["user_id"], session["user_id"]]
    if destination:
        sql += " AND (tg.destination LIKE %s OR tg.name LIKE %s)"
        params.extend([f"%{destination}%", f"%{destination}%"])
    sql += " GROUP BY tg.id ORDER BY tg.travel_date ASC"
    groups = query(sql, tuple(params))
    max_travel_date = (date.today() + timedelta(days=MAX_GROUP_DAYS_AHEAD)).isoformat()
    return render_template("travel_groups.html", groups=groups, destination=destination, today=date.today().isoformat(), max_travel_date=max_travel_date)



@app.route("/travel_groups/create", methods=["POST"])
def create_travel_group():
    if "user_id" not in session or session.get("user") != 0:
        return redirect(url_for("login"))

    name = request.form.get("name", "").strip()
    destination = request.form.get("destination", "").strip()
    travel_date = request.form.get("travel_date", "")
    budget_range = request.form.get("budget_range", "").strip()
    description = request.form.get("description", "").strip()
    try:
        max_members = int(request.form.get("max_members", "0"))
    except ValueError:
        max_members = 0

    max_travel_date = (date.today() + timedelta(days=MAX_GROUP_DAYS_AHEAD)).isoformat()

    if (
        not name or not destination or not travel_date
        or travel_date < date.today().isoformat()
        or travel_date > max_travel_date
        or not (2 <= max_members <= 30)
    ):
        flash("Choose a travel date within the next 6 months and a group size from 2 to 30.", "error")
        return redirect(url_for("travel_groups"))

    group_id = execute(
        """
        INSERT INTO travel_groups
            (name, destination, travel_date, max_members, created_by, budget_range, description, status)
        VALUES (%s, %s, %s, %s, %s, %s, %s, 'Pending')
        """,
        (name, destination, travel_date, max_members, session["user_id"],
         budget_range or None, description or None),
    )
    execute("INSERT INTO group_members (group_id, user_id, status) VALUES (%s, %s, 'Confirmed')",
            (group_id, session["user_id"]))
    notify_admins(f"{session['username']} submitted the travel group '{name}' for review.", url_for("admin_travel_groups"))
    notify(session["user_id"], f"Your group '{name}' is awaiting admin approval.", url_for("travel_groups"))
    flash("Your travel group has been submitted for admin review. It'll be visible to other travellers once approved.", "success")
    return redirect(url_for("travel_groups"))






@app.route("/travel_groups/<int:group_id>/join", methods=["POST"])
def join_travel_group(group_id):
    if "user_id" not in session or session.get("user") != 0:
        return redirect(url_for("login"))

    group = query(
        """SELECT tg.name, tg.max_members, tg.travel_date, tg.status,
                  SUM(gm.status = 'Confirmed') AS member_count
           FROM travel_groups tg LEFT JOIN group_members gm ON gm.group_id = tg.id
           WHERE tg.id = %s GROUP BY tg.id""",
        (group_id,), fetch="one",
    )
    existing = query(
        "SELECT id, status FROM group_members WHERE group_id = %s AND user_id = %s",
        (group_id, session["user_id"]), fetch="one",
    )
    if group is None:
        flash("That travel group is no longer available.", "error")
    elif existing and existing["status"] == "Confirmed":
        flash("You have already joined this group.", "error")
    elif existing and existing["status"] == "Pending":
        flash("You've already requested to join this group and it's awaiting admin approval.", "error")
    elif group["status"] != "Confirmed":
        flash("This group hasn't been approved by an admin yet.", "error")
    elif group["travel_date"] < date.today():
        flash("This group's trip has already happened, so it's no longer accepting new members.", "error")
    elif group["member_count"] >= group["max_members"]:
        flash("This group is already full.", "error")
    else:
        if existing:
            # previously rejected -- let them try again
            execute("UPDATE group_members SET status = 'Pending' WHERE id = %s", (existing["id"],))
        else:
            execute("INSERT INTO group_members (group_id, user_id, status) VALUES (%s, %s, 'Pending')",
                    (group_id, session["user_id"]))
        notify_admins(
            f"{session['username']} requested to join the travel group '{group['name']}'.",
            url_for("admin_travel_groups"),
        )
        notify(session["user_id"], f"Your request to join '{group['name']}' is awaiting admin approval.", url_for("travel_groups"))
        flash("Your request to join has been sent to the admin for approval.", "success")
    return redirect(url_for("travel_groups"))


# MANAGE TRAVEL GROUPS - ADMIN

@app.route("/admin/travel_groups")
def admin_travel_groups():

    if "username" not in session:
        return redirect(url_for("login"))

    if session.get("user") != 1:
        return redirect(url_for("dashboard"))

    groups = query(
        """
        SELECT tg.id, tg.name, tg.destination, tg.travel_date, tg.max_members,
               tg.budget_range, tg.description, tg.status, tg.created_at,
               u.full_name AS creator_name, u.username AS creator_username,
               SUM(gm.status = 'Confirmed') AS member_count
        FROM travel_groups tg
        JOIN users u ON u.id = tg.created_by
        LEFT JOIN group_members gm ON gm.group_id = tg.id
        GROUP BY tg.id
        ORDER BY tg.created_at DESC
        """
    )

    grouped_groups = []
    for status_name in ("Pending", "Confirmed", "Rejected"):
        matches = [g for g in groups if g["status"] == status_name]
        if matches:
            grouped_groups.append((status_name, matches))

    status_counts = {}
    for g in groups:
        status_counts[g["status"]] = status_counts.get(g["status"], 0) + 1

    pending_members = query(
        """
        SELECT gm.id, tg.name AS group_name, tg.destination,
               u.full_name AS traveller_name, u.username AS traveller_username
        FROM group_members gm
        JOIN travel_groups tg ON tg.id = gm.group_id
        JOIN users u ON u.id = gm.user_id
        WHERE gm.status = 'Pending'
        ORDER BY gm.id
        """
    )

    return render_template(
        "admin_travel_groups.html",
        groups=groups,
        grouped_groups=grouped_groups,
        pending_count=status_counts.get("Pending", 0),
        confirmed_count=status_counts.get("Confirmed", 0),
        rejected_count=status_counts.get("Rejected", 0),
        pending_members=pending_members,
    )


@app.route("/admin/travel_groups/<int:group_id>/update-status", methods=["POST"])
def update_group_status(group_id):

    if "username" not in session or session.get("user") != 1:
        return redirect(url_for("login"))

    status = request.form.get("status", "")

    if status not in ("Confirmed", "Rejected"):
        flash("Please choose a valid group status.", "error")
    else:
        group = query("SELECT created_by, name, status FROM travel_groups WHERE id = %s", (group_id,), fetch="one")
        execute("UPDATE travel_groups SET status = %s WHERE id = %s", (status, group_id))
        if group and group["status"] != status:
            notify(group["created_by"], f"Your travel group '{group['name']}' was {status.lower()}.", url_for("travel_groups"))
        flash("Travel group status updated.", "success")

    return redirect(url_for("admin_travel_groups"))


@app.route("/admin/group_members/<int:member_id>/update-status", methods=["POST"])
def update_group_member_status(member_id):

    if "username" not in session or session.get("user") != 1:
        return redirect(url_for("login"))

    status = request.form.get("status", "")

    if status not in ("Confirmed", "Rejected"):
        flash("Please choose a valid membership status.", "error")
    else:
        member = query(
            """SELECT gm.user_id, gm.status, tg.name AS group_name
               FROM group_members gm JOIN travel_groups tg ON tg.id = gm.group_id
               WHERE gm.id = %s""",
            (member_id,), fetch="one",
        )
        execute("UPDATE group_members SET status = %s WHERE id = %s", (status, member_id))
        if member and member["status"] != status:
            notify(
                member["user_id"],
                f"Your request to join '{member['group_name']}' was {status.lower()}.",
                url_for("travel_groups"),
            )
        flash("Membership request updated.", "success")

    return redirect(url_for("admin_travel_groups"))


# PROFILE

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

        if not full_name or not email:

            flash("Full name and email are required.", "error")

        else:

            try:
                execute(
                    "UPDATE users SET full_name = %s, email = %s, phone = %s WHERE id = %s",
                    (full_name, email, phone or None, session["user_id"]),
                )

            except IntegrityError:

                flash("That email address is already in use.", "error")

            else:

                flash("Your profile has been updated.", "success")

                return redirect(url_for("profile"))

    user = query(
        "SELECT full_name, username, email, phone, created_at FROM users WHERE id = %s",
        (session["user_id"],), fetch="one",
    )

    return render_template("profile.html", user=user)


# MANAGE TRAVELLERS - ADMIN

@app.route("/admin/travellers", methods=["GET", "POST"])
def manage_travellers():

    # User must be logged in
    if "username" not in session:

        return redirect(url_for("login"))

    # Only Admin can access this page
    if session.get("user") != 1:

        return redirect(url_for("dashboard"))

    if request.method == "POST":

        full_name = request.form.get("full_name", "").strip()
        username = request.form.get("username", "").strip()
        email = request.form.get("email", "").strip()
        phone = request.form.get("phone", "").strip()
        password = request.form.get("password", "")

        # REQUIRED FIELDS

        if not (full_name and username and email and password):

            flash("Please fill in every required field.", "error")

        # PHONE FORMAT

        elif phone and not (phone.isdigit() and len(phone) == 10):

            flash("Phone number must be a 10 digit number.", "error")

        else:

            try:

                # Admin-added travellers are still travellers (role = 0).

                execute(
                    """
                    INSERT INTO users
                        (full_name, username, email, phone, password_hash, role)
                    VALUES (%s, %s, %s, %s, %s, 0)
                    """,
                    (
                        full_name,
                        username,
                        email,
                        phone or None,
                        generate_password_hash(password),
                    ),
                )

            except IntegrityError:

                flash("That username, email or phone number is already registered.", "error")

            else:

                flash("Traveller added.", "success")

        return redirect(url_for("manage_travellers"))

    travellers = query(
        """
        SELECT id, full_name, username, email, phone, created_at
        FROM users
        WHERE role = 0
        ORDER BY created_at DESC
        """
    )

    return render_template("manage_travellers.html", travellers=travellers)


@app.route("/admin/travellers/<int:user_id>/update", methods=["POST"])
def update_traveller(user_id):

    if "username" not in session or session.get("user") != 1:
        return redirect(url_for("login"))

    full_name = request.form.get("full_name", "").strip()
    email = request.form.get("email", "").strip()
    phone = request.form.get("phone", "").strip()

    if not full_name or not email:

        flash("Full name and email are required.", "error")

    elif phone and not (phone.isdigit() and len(phone) == 10):

        flash("Phone number must be a 10 digit number.", "error")

    else:

        try:
            execute(
                "UPDATE users SET full_name = %s, email = %s, phone = %s WHERE id = %s AND role = 0",
                (full_name, email, phone or None, user_id),
            )

        except IntegrityError:

            flash("That email or phone number is already in use.", "error")

        else:

            flash("Traveller updated.", "success")

    return redirect(url_for("manage_travellers"))


@app.route("/admin/travellers/<int:user_id>/delete", methods=["POST"])
def delete_traveller(user_id):

    if "username" not in session or session.get("user") != 1:
        return redirect(url_for("login"))

    # Bookings, group memberships and groups they created are all set to
    # cascade in the database, so removing the traveller cleans those up too.

    execute("DELETE FROM users WHERE id = %s AND role = 0", (user_id,))

    flash("Traveller removed.", "success")

    return redirect(url_for("manage_travellers"))


# MANAGE PACKAGES - ADMIN

# A real Nepal package is never a one-rupee line item and never a multi-decade
# "trip" -- these bounds keep the data admins enter believable.
MIN_PACKAGE_PRICE = 2000
MAX_PACKAGE_PRICE = 500000
MIN_PACKAGE_DAYS = 1
MAX_PACKAGE_DAYS = 60


def validate_package_fields(name, hotel_type, transport_mode, duration_days, price):
    """Returns an error message if the package fields aren't sane, else None."""

    if not (name and hotel_type and transport_mode):
        return "Please fill in every field."

    if not (MIN_PACKAGE_DAYS <= duration_days <= MAX_PACKAGE_DAYS):
        return f"Duration must be between {MIN_PACKAGE_DAYS} and {MAX_PACKAGE_DAYS} days."

    if not (MIN_PACKAGE_PRICE <= price <= MAX_PACKAGE_PRICE):
        return f"Price must be between {format_npr(MIN_PACKAGE_PRICE)} and {format_npr(MAX_PACKAGE_PRICE)}."

    return None


@app.route("/admin/packages", methods=["GET", "POST"])
def admin_packages():

    # User must be logged in
    if "username" not in session:

        return redirect(
            url_for("login")
        )

    # Only Admin
    if session.get("user") != 1:

        return redirect(
            url_for("dashboard")
        )

    if request.method == "POST":

        name = request.form.get("name", "").strip()
        hotel_type = request.form.get("hotel_type", "")
        transport_mode = request.form.get("transport_mode", "")

        try:
            duration_days = int(request.form.get("duration_days", "0"))
        except ValueError:
            duration_days = 0

        try:
            price = float(request.form.get("price", "0"))
        except ValueError:
            price = 0

        error = validate_package_fields(name, hotel_type, transport_mode, duration_days, price)

        if error:

            flash(error, "error")

        else:

            execute(
                """
                INSERT INTO packages
                    (name, duration_days, hotel_type, transport_mode, price)
                VALUES (%s, %s, %s, %s, %s)
                """,
                (name, duration_days, hotel_type, transport_mode, price),
            )

            flash("Package added.", "success")

        return redirect(url_for("admin_packages"))

    packages = query(
        "SELECT id, name, duration_days, hotel_type, transport_mode, price FROM packages ORDER BY price"
    )

    return render_template("admin_packages.html", packages=packages)


@app.route("/admin/packages/<int:package_id>/update", methods=["POST"])
def update_package(package_id):

    if "username" not in session or session.get("user") != 1:
        return redirect(url_for("login"))

    name = request.form.get("name", "").strip()
    hotel_type = request.form.get("hotel_type", "")
    transport_mode = request.form.get("transport_mode", "")

    try:
        duration_days = int(request.form.get("duration_days", "0"))
    except ValueError:
        duration_days = 0

    try:
        price = float(request.form.get("price", "0"))
    except ValueError:
        price = 0

    error = validate_package_fields(name, hotel_type, transport_mode, duration_days, price)

    if error:

        flash(error, "error")

    else:

        execute(
            """
            UPDATE packages
            SET name = %s, duration_days = %s, hotel_type = %s, transport_mode = %s, price = %s
            WHERE id = %s
            """,
            (name, duration_days, hotel_type, transport_mode, price, package_id),
        )

        flash("Package updated.", "success")

    return redirect(url_for("admin_packages"))


@app.route("/admin/packages/<int:package_id>/delete", methods=["POST"])
def delete_package(package_id):

    if "username" not in session or session.get("user") != 1:
        return redirect(url_for("login"))

    # Bookings that referenced this package keep their own copy of the
    # package name and price, so they're unaffected (packages.id is set
    # to NULL on the booking rather than the booking being removed).

    execute("DELETE FROM packages WHERE id = %s", (package_id,))

    flash("Package removed.", "success")

    return redirect(url_for("admin_packages"))


# MANAGE BOOKINGS - ADMIN

@app.route("/admin/bookings")
def admin_bookings():

    # User must be logged in
    if "username" not in session:

        return redirect(url_for("login"))

    # Only Admin can access this page
    if session.get("user") != 1:

        return redirect(url_for("dashboard"))

    search = request.args.get("search", "").strip()
    status_filter = request.args.get("status", "")

    sql = """
        SELECT b.id, b.package_name, b.travel_date, b.travellers, b.phone,
               b.price, b.status, b.created_at,
               u.full_name AS traveller_name, u.username AS traveller_username
        FROM bookings b
        JOIN users u ON u.id = b.user_id
        WHERE 1=1
    """
    params = []

    if search:
        sql += " AND (u.full_name LIKE %s OR u.username LIKE %s OR b.package_name LIKE %s)"
        like = f"%{search}%"
        params.extend([like, like, like])

    if status_filter:
        sql += " AND b.status = %s"
        params.append(status_filter)

    sql += " ORDER BY b.created_at DESC"

    bookings = query(sql, tuple(params))

    # Group by status (in a fixed order) instead of one interleaved list, so
    # confirmed/completed/cancelled bookings don't get mixed together when
    # the admin isn't filtering to a single status.
    # Pending bookings and cancel requests need admin action, so they're
    # grouped ahead of the settled statuses.
    grouped_bookings = []
    for status_name in ("Pending", "Cancel Requested", "Confirmed", "Completed", "Cancelled"):
        matches = [b for b in bookings if b["status"] == status_name]
        if matches:
            grouped_bookings.append((status_name, matches))

    # Status counts are pulled independently of the filters above so the
    # summary cards always reflect every booking, not just the filtered view.

    status_rows = query("SELECT status, COUNT(*) AS total FROM bookings GROUP BY status")
    status_counts = {row["status"]: row["total"] for row in status_rows}

    return render_template(
        "admin_bookings.html",
        bookings=bookings,
        grouped_bookings=grouped_bookings,
        search=search,
        selected_status=status_filter,
        pending_count=status_counts.get("Pending", 0),
        cancel_requested_count=status_counts.get("Cancel Requested", 0),
        confirmed_count=status_counts.get("Confirmed", 0),
        completed_count=status_counts.get("Completed", 0),
        cancelled_count=status_counts.get("Cancelled", 0),
    )


@app.route("/admin/bookings/<int:booking_id>/update-status", methods=["POST"])
def update_booking_status(booking_id):

    if "username" not in session or session.get("user") != 1:
        return redirect(url_for("login"))

    status = request.form.get("status", "")
    search = request.form.get("search", "")
    status_filter = request.form.get("status_filter", "")

    if status not in ("Confirmed", "Completed", "Cancelled"):

        flash("Please choose a valid booking status.", "error")

    else:

        booking = query("SELECT user_id, package_name, status FROM bookings WHERE id = %s", (booking_id,), fetch="one")
        execute("UPDATE bookings SET status = %s WHERE id = %s", (status, booking_id))
        if booking and booking["status"] != status:
            notify(booking["user_id"], f"Your booking #{booking_id} for '{booking['package_name']}' is now {status.lower()}.", url_for("my_bookings"))

        flash("Booking status updated.", "success")

    return redirect(url_for("admin_bookings", search=search, status=status_filter))


@app.route("/admin/reports")
def admin_reports():
    if "username" not in session:
        return redirect(url_for("login"))
    if session.get("user") != 1:
        return redirect(url_for("dashboard"))

    period = request.args.get("period", "all")
    periods = {"all": "All time", "30": "Last 30 days", "90": "Last 90 days", "year": "This year"}
    if period not in periods:
        period = "all"
    today = date.today()
    start = (date(today.year, 1, 1) if period == "year" else
             today - timedelta(days=int(period) - 1) if period in ("30", "90") else None)
    sql = "SELECT package_name, price, travellers, status, created_at FROM bookings"
    params = []
    if start:
        sql += " WHERE created_at >= %s AND created_at < %s"
        params = [start, today + timedelta(days=1)]
    bookings = query(sql + " ORDER BY created_at", tuple(params))
    statuses = dict.fromkeys(("Pending", "Cancel Requested", "Confirmed", "Completed", "Cancelled"), 0)
    packages = {}
    months = {}
    accepted_value = 0
    accepted_travellers = 0
    for booking in bookings:
        status = booking["status"]
        statuses[status] = statuses.get(status, 0) + 1
        accepted = status in ("Confirmed", "Completed")
        value = booking["price"] or 0
        if accepted:
            accepted_value += value
            accepted_travellers += booking["travellers"] or 0
        name = booking["package_name"] or "Unnamed package"
        package = packages.setdefault(name, {"name": name, "bookings": 0, "value": 0})
        package["bookings"] += 1
        if accepted:
            package["value"] += value
        month = booking["created_at"].strftime("%Y-%m")
        months[month] = months.get(month, 0) + 1

    # Show a continuous timeline, including months without bookings.
    first_month = (start or (bookings[0]["created_at"].date() if bookings else today)).replace(day=1)
    timeline = []
    month = first_month
    while month <= today:
        timeline.append({"label": month.strftime("%b %Y"), "count": months.get(month.strftime("%Y-%m"), 0)})
        month = (month.replace(day=28) + timedelta(days=4)).replace(day=1)
    return render_template(
        "admin_reports.html", period=period, periods=periods, today=today,
        total=len(bookings), accepted_value=accepted_value, accepted_travellers=accepted_travellers,
        needs_attention=statuses["Pending"] + statuses["Cancel Requested"],
        statuses=statuses, timeline=timeline,
        top_packages=sorted(packages.values(), key=lambda p: (-p["bookings"], p["name"]))[:5],
        travellers=query("SELECT COUNT(*) AS total FROM users WHERE role = 0", fetch="one")["total"],
        package_count=query("SELECT COUNT(*) AS total FROM packages", fetch="one")["total"],
        group_count=query("SELECT COUNT(*) AS total FROM travel_groups", fetch="one")["total"],
        place_count=len(get_places(PLACES)),
    )


# NOTIFICATIONS

@app.route("/notifications")
def notifications():
    if "user_id" not in session:
        return redirect(url_for("login"))

    items = query(
        "SELECT id, message, link, is_read, created_at FROM notifications WHERE user_id = %s ORDER BY created_at DESC, id DESC",
        (session["user_id"],),
    )
    return render_template("notifications.html", notifications=items, is_admin=session.get("user") == 1)


@app.route("/notifications/read", methods=["POST"])
def read_notifications():
    if "user_id" not in session:
        return redirect(url_for("login"))
    execute("UPDATE notifications SET is_read = 1 WHERE user_id = %s AND is_read = 0", (session["user_id"],))
    return redirect(url_for("notifications"))


# LOGOUT

@app.route("/logout")
def logout():

    session.clear()

    return redirect(
        url_for("landing")
    )


if __name__ == "__main__":
    with app.app_context():
        ensure_group_columns()
        ensure_notifications_table()
    app.run(debug=True)
