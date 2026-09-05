from flask import Flask, flash, render_template, request, redirect, url_for, session
from dotenv import load_dotenv
from datetime import date, timedelta
from mysql.connector import IntegrityError
from werkzeug.security import check_password_hash, generate_password_hash

from db import close_db, execute, query

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
        "SELECT COUNT(*) AS total FROM travel_groups", fetch="one"
    )["total"]

    return render_template(
        "admin_dashboard.html",

        username=session["username"],

        featured_packages=packages,

        total_travellers=total_travellers,

        total_packages=total_packages,

        total_bookings=total_bookings,

        total_groups=total_groups
    )


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
        place for place in PLACES
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

    place = next((item for item in PLACES if item["slug"] == slug), None)
    if place is None:
        return redirect(url_for("explore_places"))

    similar_places = [
        item for item in PLACES
        if item["category"] == place["category"] and item["slug"] != slug
    ][:3]
    return render_template(
        "place_detail.html", place=place, similar_places=similar_places
    )


# BOOK PACKAGE


# Trips can be planned ahead, but not decades ahead -- two years covers any
# realistic advance booking.
MAX_BOOKING_DAYS_AHEAD = 180


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

        else:

            execute(
                """
                INSERT INTO bookings
                    (user_id, package_id, package_name, travel_date, travellers, phone, price, status)
                VALUES (%s, %s, %s, %s, %s, %s, %s, 'Confirmed')
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

            flash("Your booking is confirmed.", "success")

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

    else:
        execute(
            "UPDATE bookings SET status = 'Cancelled' WHERE id = %s AND user_id = %s",
            (booking_id, session["user_id"]),
        )
        flash("Your booking has been cancelled.", "success")

    return redirect(url_for("my_bookings"))


# TRAVEL GROUPS

@app.route("/travel_groups")
def travel_groups():

    if "username" not in session:
        return redirect(url_for("login"))

    if session.get("user") != 0:
        return redirect(url_for("admin_dashboard"))

    destination = request.args.get("destination", "").strip()
    sql = """
        SELECT tg.id, tg.name, tg.destination, tg.travel_date, tg.max_members,
               tg.budget_range, tg.description, u.username AS created_by,
               COUNT(gm.id) AS member_count,
               SUM(gm.user_id = %s) AS has_joined
        FROM travel_groups tg
        JOIN users u ON u.id = tg.created_by
        LEFT JOIN group_members gm ON gm.group_id = tg.id
        WHERE tg.travel_date >= CURDATE()
    """
    params = [session["user_id"]]
    if destination:
        sql += " AND (tg.destination LIKE %s OR tg.name LIKE %s)"
        params.extend([f"%{destination}%", f"%{destination}%"])
    sql += " GROUP BY tg.id ORDER BY tg.travel_date ASC"
    groups = query(sql, tuple(params))
    return render_template("travel_groups.html", groups=groups, destination=destination)


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

    if not name or not destination or not travel_date or not (2 <= max_members <= 30):
        flash("Complete all required fields and choose a group size from 2 to 30.", "error")
        return redirect(url_for("travel_groups"))

    group_id = execute(
        """
        INSERT INTO travel_groups
            (name, destination, travel_date, max_members, created_by, budget_range, description)
        VALUES (%s, %s, %s, %s, %s, %s, %s)
        """,
        (name, destination, travel_date, max_members, session["user_id"],
         budget_range or None, description or None),
    )
    execute("INSERT INTO group_members (group_id, user_id) VALUES (%s, %s)",
            (group_id, session["user_id"]))
    flash("Your travel group has been created. You are its first member!", "success")
    return redirect(url_for("travel_groups"))


@app.route("/travel_groups/<int:group_id>/join", methods=["POST"])
def join_travel_group(group_id):
    if "user_id" not in session or session.get("user") != 0:
        return redirect(url_for("login"))

    group = query(
        """SELECT tg.max_members, COUNT(gm.id) AS member_count
           FROM travel_groups tg LEFT JOIN group_members gm ON gm.group_id = tg.id
           WHERE tg.id = %s GROUP BY tg.id""",
        (group_id,), fetch="one",
    )
    already_joined = query(
        "SELECT id FROM group_members WHERE group_id = %s AND user_id = %s",
        (group_id, session["user_id"]), fetch="one",
    )
    if group is None:
        flash("That travel group is no longer available.", "error")
    elif already_joined:
        flash("You have already joined this group.", "error")
    elif group["member_count"] >= group["max_members"]:
        flash("This group is already full.", "error")
    else:
        execute("INSERT INTO group_members (group_id, user_id) VALUES (%s, %s)",
                (group_id, session["user_id"]))
        flash("You joined the travel group successfully!", "success")
    return redirect(url_for("travel_groups"))


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

    # Status counts are pulled independently of the filters above so the
    # summary cards always reflect every booking, not just the filtered view.

    status_rows = query("SELECT status, COUNT(*) AS total FROM bookings GROUP BY status")
    status_counts = {row["status"]: row["total"] for row in status_rows}

    return render_template(
        "admin_bookings.html",
        bookings=bookings,
        search=search,
        selected_status=status_filter,
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

        execute("UPDATE bookings SET status = %s WHERE id = %s", (status, booking_id))

        flash("Booking status updated.", "success")

    return redirect(url_for("admin_bookings", search=search, status=status_filter))


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
    app.run(debug=True)