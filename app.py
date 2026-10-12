from flask import Flask, abort, flash, render_template, request, redirect, url_for, session, send_file, jsonify, Response
import base64
import csv
import hashlib
import hmac
import json
import logging
import os
import re
import secrets
import time
import requests
import stripe
from io import BytesIO, StringIO
from logging.handlers import RotatingFileHandler
from pathlib import Path
from dotenv import load_dotenv
from datetime import date, timedelta
from decimal import Decimal, InvalidOperation
from urllib.parse import urlsplit
from mysql.connector import IntegrityError
from reportlab.lib.pagesizes import A4
from reportlab.pdfgen import canvas as pdf_canvas
from werkzeug.exceptions import HTTPException
from werkzeug.middleware.proxy_fix import ProxyFix
from werkzeug.security import check_password_hash, generate_password_hash

from db import close_db, execute, query
from mailer import send_email
from place_store import get_places, save_place, remove_place

load_dotenv()

app = Flask(__name__)

# The secret key signs the session cookie (which stores user_id/role) -- it
# must come from the environment, not a literal in this file, otherwise
# anyone who can read the source can forge an admin cookie. Fails loudly
# instead of silently falling back to an insecure default, matching how the
# Stripe keys below are already handled.
app.secret_key = os.environ.get("SECRET_KEY")
if not app.secret_key:
    raise RuntimeError(
        "SECRET_KEY is not set. Add SECRET_KEY=<a long random value> to your "
        ".env file (see .env.example) before starting the app."
    )

app.teardown_appcontext(close_db)


# PRODUCTION CONFIGURATION
# APP_ENV=production switches on the stricter defaults below. Everything is
# still overridable from the environment (see .env.example / DEPLOYMENT.md).
IS_PRODUCTION = os.environ.get("APP_ENV", "development").lower() == "production"

app.config.update(
    # JavaScript can't read the session cookie, and other sites can't ride
    # along with it on a cross-site POST.
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    # Only sent over HTTPS in production. Off in development so
    # http://localhost still works -- a Secure cookie is dropped over plain http.
    SESSION_COOKIE_SECURE=os.environ.get("SESSION_COOKIE_SECURE", "1" if IS_PRODUCTION else "0") == "1",
    # Refuse any request body over 8 MB before it's read into memory (the
    # largest legitimate upload is a 3 MB profile photo).
    MAX_CONTENT_LENGTH=8 * 1024 * 1024,
    # A signed-in session ends after this long without any request (the
    # cookie is refreshed on every request, so it's an idle timeout, not a
    # hard cut-off). login() marks the session permanent so this applies.
    PERMANENT_SESSION_LIFETIME=timedelta(
        minutes=int(os.environ.get("SESSION_LIFETIME_MINUTES", "120"))
    ),
)

# Behind nginx / a load balancer every request appears to come from the
# proxy's own IP, which would make the login throttle and rate limiters below
# treat all visitors as one person. Set TRUST_PROXY=1 only when the app really
# is behind exactly one proxy that sets X-Forwarded-For (a client could
# otherwise forge it).
if os.environ.get("TRUST_PROXY") == "1":
    app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)

# APP_BASE_URL is used to build absolute links in emails (e.g. https://gogather.example.com).
APP_BASE_URL = os.environ.get("APP_BASE_URL", "http://localhost:5000").rstrip("/")


def configure_logging():
    """Errors and mail activity go to logs/gogather.log (rotated at 1 MB,
    5 old files kept) as well as the console, so a crash on a server can be
    diagnosed after the fact."""
    log_dir = Path(app.root_path) / "logs"
    try:
        log_dir.mkdir(exist_ok=True)
    except OSError:
        return  # read-only filesystem etc. -- console logging still works

    if any(isinstance(handler, RotatingFileHandler) for handler in app.logger.handlers):
        return

    handler = RotatingFileHandler(log_dir / "gogather.log", maxBytes=1_000_000, backupCount=5, encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    handler.setLevel(logging.INFO)
    app.logger.addHandler(handler)
    app.logger.setLevel(logging.INFO)
    # The mail helper logs under its own name -- route it to the same file.
    mail_logger = logging.getLogger("gogather.mail")
    mail_logger.addHandler(handler)
    mail_logger.setLevel(logging.INFO)


configure_logging()


# MYSQL DATABASE CONNECTION
# DEMO USERS
# user = 0 → Traveller
# user = 1 → Admin


# Featured packages, places and package itinerary details all live in MySQL
# now (see place_store.py and the `packages` table's is_featured/tag/summary/
# places_covered/activities/includes columns, both self-migrating the first
# time the app runs against a database that doesn't have them yet).


# FORMAT NPR

def format_npr(amount):
    return "Rs {:,.0f}".format(amount)


app.jinja_env.filters["npr"] = format_npr


# PAYMENT GATEWAY CONFIG
# eSewa: sandbox/test merchant credentials as defaults so the flow works
# out of the box in dev -- swap for real merchant credentials via env vars
# before going live.
ESEWA_PRODUCT_CODE = os.environ.get("ESEWA_PRODUCT_CODE", "EPAYTEST")
ESEWA_SANDBOX_SECRET = "8gBm/:&EnhH.1/q"  # eSewa's published test key -- public, so anyone can sign with it
ESEWA_SECRET_KEY = os.environ.get("ESEWA_SECRET_KEY", ESEWA_SANDBOX_SECRET)
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


def npr_to_usd_cents(npr_amount):
    return int(round((float(npr_amount) / NPR_PER_USD) * 100))


def esewa_sign(message, secret_key):
    digest = hmac.new(secret_key.encode("utf-8"), message.encode("utf-8"), hashlib.sha256).digest()
    return base64.b64encode(digest).decode("utf-8")


def esewa_is_trusted():
    """eSewa's sandbox signing key is public, so when it's still in use anyone
    could forge a 'payment complete' result. In production that is refused
    unless ESEWA_ALLOW_SANDBOX=1 is set on purpose (e.g. a demo site that
    only ever uses eSewa's test wallet and takes no real money)."""
    if not IS_PRODUCTION or ESEWA_SECRET_KEY != ESEWA_SANDBOX_SECRET:
        return True
    return os.environ.get("ESEWA_ALLOW_SANDBOX") == "1"


def ensure_package_columns():
    """Add the itinerary columns admins can edit from Manage Packages, and
    backfill them once (on the run that adds the columns) with the details
    that used to be hardcoded in this file as FEATURED_PACKAGES/PACKAGE_DETAILS."""
    columns = {row["Field"] for row in query("DESCRIBE packages")}
    is_new = "is_featured" not in columns
    if is_new:
        execute("ALTER TABLE packages ADD COLUMN is_featured TINYINT(1) NOT NULL DEFAULT 0")
    if "tag" not in columns:
        execute("ALTER TABLE packages ADD COLUMN tag VARCHAR(150) NULL")
    if "summary" not in columns:
        execute("ALTER TABLE packages ADD COLUMN summary TEXT NULL")
    if "places_covered" not in columns:
        execute("ALTER TABLE packages ADD COLUMN places_covered JSON NULL")
    if "activities" not in columns:
        execute("ALTER TABLE packages ADD COLUMN activities JSON NULL")
    if "includes" not in columns:
        execute("ALTER TABLE packages ADD COLUMN includes JSON NULL")
    if "cost_price" not in columns:
        # What GoGather pays the supplier per traveller -- separate from
        # `price` (what the traveller pays), so `price - cost_price` is the
        # actual margin instead of the whole price being treated as profit.
        execute("ALTER TABLE packages ADD COLUMN cost_price DECIMAL(10, 2) NULL")
        # Existing packages predate this field, so seed it from the same
        # DEFAULT_MARGIN_PERCENT formula new packages use, rather than
        # leaving it NULL (which would make their margin look like 100% of
        # the price).
        execute(
            "UPDATE packages SET cost_price = price * %s WHERE cost_price IS NULL",
            (1 - DEFAULT_MARGIN_PERCENT / 100,),
        )
    if "max_seats" not in columns:
        # How many travellers can depart on this package on the same date,
        # across everyone's bookings combined -- NULL means no limit (the
        # behaviour every existing package already had before this column
        # existed, so adding it can't retroactively block anyone).
        execute("ALTER TABLE packages ADD COLUMN max_seats INT NULL")
    if is_new:
        backfill_package_details()


def backfill_package_details():
    featured = [
        {
            "name": "Pokhara & Kathmandu Heritage Tour", "duration_days": 5, "hotel_type": "standard",
            "transport_mode": "bus", "price": 32500, "tag": "Culture & lakes",
            "summary": "A balanced city and lakeside escape combining Kathmandu's heritage with Pokhara's mountain views.",
            "places": ["Kathmandu", "Patan", "Pokhara", "Phewa Lake"],
            "activities": ["Heritage walk", "Lakeside boating", "Sunrise viewpoint"],
            "includes": ["Hotel stay", "Private transfers", "Daily breakfast", "Local guide"],
        },
        {
            "name": "Chitwan Wildlife Safari", "duration_days": 3, "hotel_type": "budget",
            "transport_mode": "bus", "price": 15500, "tag": "Wildlife escape",
            "summary": "A relaxed jungle break with guided wildlife experiences and a warm introduction to Tharu culture.",
            "places": ["Sauraha", "Chitwan National Park", "Rapti River"],
            "activities": ["Jeep safari", "Canoe ride", "Tharu cultural evening"],
            "includes": ["Hotel stay", "Safari permits", "Breakfast and dinner", "Naturalist guide"],
        },
        {
            "name": "Everest Base Camp Trek", "duration_days": 14, "hotel_type": "standard",
            "transport_mode": "flight", "price": 185000, "tag": "High Himalayan trek",
            "summary": "A carefully paced Everest journey through Sherpa villages, dramatic valleys and iconic mountain viewpoints.",
            "places": ["Lukla", "Namche Bazaar", "Tengboche", "Everest Base Camp"],
            "activities": ["Guided trek", "Acclimatisation hikes", "Mountain photography"],
            "includes": ["Domestic flights", "Teahouse stays", "Trekking guide", "Required permits"],
        },
    ]
    for pkg in featured:
        existing = query("SELECT id FROM packages WHERE name = %s", (pkg["name"],), fetch="one")
        params = (
            pkg["tag"], pkg["summary"], json.dumps(pkg["places"]),
            json.dumps(pkg["activities"]), json.dumps(pkg["includes"]),
        )
        if existing is None:
            execute(
                """
                INSERT INTO packages
                    (name, duration_days, hotel_type, transport_mode, price, cost_price,
                     is_featured, tag, summary, places_covered, activities, includes)
                VALUES (%s, %s, %s, %s, %s, %s, 1, %s, %s, %s, %s, %s)
                """,
                (pkg["name"], pkg["duration_days"], pkg["hotel_type"], pkg["transport_mode"],
                 pkg["price"], cost_price_for(pkg["price"])) + params,
            )
        else:
            execute(
                """
                UPDATE packages
                SET is_featured = 1, tag = %s, summary = %s, places_covered = %s, activities = %s, includes = %s
                WHERE id = %s
                """,
                params + (existing["id"],),
            )

    # These packages only ever had their rich itinerary details hardcoded
    # (keyed by id, on the assumption a package with that id already
    # existed) -- update them in place if they exist, and leave them alone
    # otherwise rather than inventing packages with no real base data.
    extra_details = {
        5: {"tag": "Pilgrimage", "summary": "A short, peaceful visit to the birthplace of Lord Buddha and its surrounding monastic zone.", "places": ["Maya Devi Temple", "Sacred Garden", "Monastic Zone"], "activities": ["Pilgrimage walk", "Meditation session", "Cycling tour"], "includes": ["Hotel stay", "Private transfers", "Daily breakfast", "Local guide"]},
        6: {"tag": "Hilltop retreat", "summary": "A quick weekend escape near Kathmandu built around Himalayan sunrise views.", "places": ["Nagarkot viewpoint", "Panoramic trails"], "activities": ["Sunrise viewing", "Short hikes", "Photography"], "includes": ["Hotel stay", "Jeep transfers", "Daily breakfast"]},
        7: {"tag": "Heritage town", "summary": "A relaxed walk through a beautifully preserved Newari hill town with sweeping Himalayan views.", "places": ["Bandipur Bazaar", "Tundikhel", "Siddha Cave"], "activities": ["Village walks", "Cave visit", "Local cuisine tasting"], "includes": ["Hotel stay", "Private transfers", "Daily breakfast", "Local guide"]},
        8: {"tag": "Remote alpine lake", "summary": "A trek into Nepal's far west to reach one of the country's most untouched alpine lakes.", "places": ["Rara National Park", "Lakeside trail", "Talcha"], "activities": ["Guided trekking", "Camping", "Nature photography"], "includes": ["Domestic flights", "Camping/lodge stays", "Trekking guide", "Required permits"]},
        9: {"tag": "Trans-Himalayan jeep tour", "summary": "A dramatic jeep expedition into the walled villages and ochre cliffs of Upper Mustang.", "places": ["Lo Manthang", "Muktinath", "Kali Gandaki Valley"], "activities": ["Jeep touring", "Monastery visits", "Cultural walks"], "includes": ["4WD jeep transport", "Lodge stays", "Local guide", "Required permits"]},
        10: {"tag": "Cultural city break", "summary": "A colourful introduction to Mithila art and tradition centred on the Janaki Mandir.", "places": ["Janaki Mandir", "Ganga Sagar"], "activities": ["Temple visit", "Mithila art workshop", "Food exploration"], "includes": ["Hotel stay", "Private transfers", "Daily breakfast"]},
    }
    for package_id, details in extra_details.items():
        execute(
            """
            UPDATE packages
            SET tag = %s, summary = %s, places_covered = %s, activities = %s, includes = %s
            WHERE id = %s
            """,
            (details["tag"], details["summary"], json.dumps(details["places"]),
             json.dumps(details["activities"]), json.dumps(details["includes"]), package_id),
        )


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
    if "cost_price" not in columns:
        # Frozen copy of the package's cost_price * travellers at booking
        # time -- mirrors why `price` itself is copied onto the booking
        # rather than looked up live: a later change to the package's cost
        # shouldn't rewrite the margin on a booking that already happened.
        execute("ALTER TABLE bookings ADD COLUMN cost_price DECIMAL(10, 2) NULL")
        execute(
            """
            UPDATE bookings b
            JOIN packages p ON p.id = b.package_id
            SET b.cost_price = p.cost_price * b.travellers
            WHERE b.cost_price IS NULL
            """
        )
    if "service_fee" not in columns:
        # The slice of `price` that's GoGather's booking fee rather than the
        # package cost -- kept only so reporting can show it as its own
        # profit line. Existing bookings predate the fee, so they're left at
        # 0 rather than having a fee invented for them retroactively.
        execute("ALTER TABLE bookings ADD COLUMN service_fee DECIMAL(10, 2) NOT NULL DEFAULT 0")
    if "discount_amount" not in columns:
        # How much of FIRST_BOOKING_DISCOUNT_PERCENT was actually knocked
        # off this booking's price -- stored (not just recomputed from the
        # percent) so the traveller's receipt and admin reporting keep
        # showing the real discount even if the percentage changes later.
        execute("ALTER TABLE bookings ADD COLUMN discount_amount DECIMAL(10, 2) NOT NULL DEFAULT 0")
    if "eula_accepted_at" not in columns:
        # When the traveller accepted the EULA for this booking's payment --
        # kept per booking so there's a record of what was agreed to before
        # money changed hands.
        execute("ALTER TABLE bookings ADD COLUMN eula_accepted_at TIMESTAMP NULL")


def ensure_group_columns():
    """Add the optional group fields used by the Travel Groups feature."""
    columns = {row["Field"] for row in query("DESCRIBE travel_groups")}
    if "budget_range" not in columns:
        execute("ALTER TABLE travel_groups ADD COLUMN budget_range VARCHAR(50) NULL")
    if "description" not in columns:
        execute("ALTER TABLE travel_groups ADD COLUMN description TEXT NULL")
    if "status" not in columns:
        execute("ALTER TABLE travel_groups ADD COLUMN status VARCHAR(20) NOT NULL DEFAULT 'Pending'")
    if "communication_mode" not in columns:
        # 2 = two-way (any confirmed member can post), 1 = one-way (only the
        # group creator can post), 0 = blocked (no one can post). Admin sets
        # this per group from Manage Travel Groups.
        execute("ALTER TABLE travel_groups ADD COLUMN communication_mode TINYINT NOT NULL DEFAULT 2")

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


def notify(user_id, message, link=None, email_subject=None):
    """Adds an in-app notification. Pass email_subject for the events people
    shouldn't have to log in to find out about (booking decisions, payments,
    refunds, replies from support) and the same message is emailed too."""
    execute("INSERT INTO notifications (user_id, message, link) VALUES (%s, %s, %s)", (user_id, message[:255], link))
    if email_subject:
        email_user(user_id, email_subject, message, link)


def email_user(user_id, subject, message, link=None):
    """Emails a user at the address on their account. Never raises -- a mail
    problem must not undo the booking, payment or refund that triggered it."""
    try:
        user = query("SELECT full_name, email FROM users WHERE id = %s", (user_id,), fetch="one")
        if not user or not user["email"]:
            return False
        body = f"Hi {user['full_name']},\n\n{message}\n"
        if link:
            body += f"\nView it here: {APP_BASE_URL}{link}\n"
        body += "\nThe GoGather team"
        return send_email(user["email"], subject, body)
    except Exception:
        app.logger.exception("Could not queue email to user %s", user_id)
        return False


# ADMIN ACTIVITY HISTORY
# Every admin action that changes a booking, package, travel group or
# destination is recorded here so the team can see who changed what and
# when, instead of that history only living in whichever row got overwritten.

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


# CANCELLATION & REFUND REQUESTS
# A Pending or unpaid Confirmed booking can still be cancelled directly (see
# cancel_booking below), but once a booking has actually been paid for,
# cancelling it means giving the traveller their money back -- that needs a
# trackable request/decision/refund trail instead of an instant status flip.
REFUND_STATUSES = ("Requested", "Approved", "Refund Pending", "Refunded", "Rejected")
OPEN_REFUND_STATUSES = ("Requested", "Approved", "Refund Pending")

# Rows per page on the admin list screens (bookings, packages, destinations,
# travellers) -- fine to load everything in one query at today's data
# volume, but keeps these pages fast once any of those lists grows large.
PAGE_SIZE = 25

# Cancellation policy: a traveller cancelling a paid booking forfeits this
# percentage of what they paid -- the rest is what actually gets requested
# as a refund. Flat and automatic (not admin-adjustable, not tiered by how
# close to the travel date it is).
CANCELLATION_FEE_PERCENT = 20

# GoGather's own booking/service fee -- on top of the package price, and
# entirely profit (no supplier cost behind it, unlike the package margin).
# Folded straight into the booking's `price` at booking time so it rides
# along with every existing amount-handling path (eSewa, Stripe, refund
# calculations, receipts) without those needing to know it exists.
SERVICE_FEE_PERCENT = float(os.environ.get("SERVICE_FEE_PERCENT", "5"))

# A one-time welcome discount off the package price (before the service fee
# above is added) -- applies only when this is the traveller's very first
# booking of any kind (checked by whether they already have any row in
# `bookings`, regardless of that earlier booking's status), never on a
# second or later booking.
FIRST_BOOKING_DISCOUNT_PERCENT = float(os.environ.get("FIRST_BOOKING_DISCOUNT_PERCENT", "7"))

# What GoGather keeps from each package's price as margin -- packages.cost_price
# (what's owed to the supplier) is derived automatically from this instead of
# an admin typing a real supplier cost in per package, so every package is
# guaranteed a margin without needing that data entry. 25% matches the
# placeholder ensure_package_columns already backfilled existing packages
# with, so changing this only affects packages saved after the change.
DEFAULT_MARGIN_PERCENT = float(os.environ.get("DEFAULT_MARGIN_PERCENT", "25"))


def cost_price_for(price):
    """The supplier cost GoGather assumes for a package priced at `price` --
    price minus the fixed DEFAULT_MARGIN_PERCENT margin GoGather keeps."""
    return round(price * (1 - DEFAULT_MARGIN_PERCENT / 100), 2)


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


# PHOTO GALLERIES
# Extra photos for a package or destination, on top of the single required
# cover photo each already has -- picked from the same static/ whitelist as
# the cover photo rather than accepting raw uploads.

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


# DAY-BY-DAY ITINERARIES
# Two tables per subject (days, then each day's timed activities) rather
# than one blob per day -- an itinerary is really "Day 1: at 08:00 do X, at
# 13:00 do Y", and that's naturally two levels, not one. Kept as real tables
# (not JSON) so a future admin screen could add/reorder a single activity
# without rewriting a whole day's content.

def ensure_itinerary_tables():
    existed = bool(query("SHOW TABLES LIKE 'package_itinerary_days'"))
    extras_existed = bool(query("SHOW TABLES LIKE 'package_itinerary_extras'"))
    # The single-row-per-day version of these tables (title+description,
    # no times) is being replaced by the two-level version below -- drop it
    # rather than leave an unused table behind.
    execute("DROP TABLE IF EXISTS package_itinerary")
    execute("DROP TABLE IF EXISTS place_itinerary")
    execute(
        """
        CREATE TABLE IF NOT EXISTS package_itinerary_days (
            id INT AUTO_INCREMENT PRIMARY KEY,
            package_id INT NOT NULL,
            day_number INT NOT NULL,
            title VARCHAR(150) NOT NULL,
            UNIQUE KEY uniq_package_day (package_id, day_number),
            FOREIGN KEY (package_id) REFERENCES packages(id) ON DELETE CASCADE
        )
        """
    )
    execute(
        """
        CREATE TABLE IF NOT EXISTS package_itinerary_activities (
            id INT AUTO_INCREMENT PRIMARY KEY,
            day_id INT NOT NULL,
            sort_order INT NOT NULL,
            activity_time VARCHAR(10) NOT NULL,
            activity VARCHAR(255) NOT NULL,
            FOREIGN KEY (day_id) REFERENCES package_itinerary_days(id) ON DELETE CASCADE
        )
        """
    )
    execute(
        """
        CREATE TABLE IF NOT EXISTS place_itinerary_days (
            id INT AUTO_INCREMENT PRIMARY KEY,
            place_id INT NOT NULL,
            day_number INT NOT NULL,
            title VARCHAR(150) NOT NULL,
            UNIQUE KEY uniq_place_day (place_id, day_number),
            FOREIGN KEY (place_id) REFERENCES explore_places(id) ON DELETE CASCADE
        )
        """
    )
    execute(
        """
        CREATE TABLE IF NOT EXISTS place_itinerary_activities (
            id INT AUTO_INCREMENT PRIMARY KEY,
            day_id INT NOT NULL,
            sort_order INT NOT NULL,
            activity_time VARCHAR(10) NOT NULL,
            activity VARCHAR(255) NOT NULL,
            FOREIGN KEY (day_id) REFERENCES place_itinerary_days(id) ON DELETE CASCADE
        )
        """
    )
    # The "what to pack" and "things to know before you go" panels on the
    # full itinerary page -- one row per package/place, each a JSON array
    # of short strings (same convention as packages.activities/includes).
    execute(
        """
        CREATE TABLE IF NOT EXISTS package_itinerary_extras (
            package_id INT NOT NULL PRIMARY KEY,
            packing_list JSON NOT NULL,
            things_to_know JSON NOT NULL,
            FOREIGN KEY (package_id) REFERENCES packages(id) ON DELETE CASCADE
        )
        """
    )
    execute(
        """
        CREATE TABLE IF NOT EXISTS place_itinerary_extras (
            place_id INT NOT NULL PRIMARY KEY,
            packing_list JSON NOT NULL,
            things_to_know JSON NOT NULL,
            FOREIGN KEY (place_id) REFERENCES explore_places(id) ON DELETE CASCADE
        )
        """
    )

    # Departure details and optional-activity extra costs -- packages only
    # (that's what's actually booked), so these live as extra nullable
    # columns on package_itinerary_extras rather than a new table.
    extras_columns = {row["Field"] for row in query("DESCRIBE package_itinerary_extras")}
    departure_columns_new = "departure_time" not in extras_columns
    if departure_columns_new:
        execute("ALTER TABLE package_itinerary_extras ADD COLUMN departure_time VARCHAR(100) NULL")
        execute("ALTER TABLE package_itinerary_extras ADD COLUMN departure_point VARCHAR(200) NULL")
    if "extra_expenses" not in extras_columns:
        execute("ALTER TABLE package_itinerary_extras ADD COLUMN extra_expenses JSON NULL")

    if not existed:
        backfill_package_itinerary()
        backfill_place_itinerary()
    if not extras_existed:
        backfill_itinerary_extras()
    if departure_columns_new:
        backfill_departure_info()


def _insert_itinerary_days(days, day_table, activity_table, parent_column, parent_id):
    """Shared by the package and place backfills: insert one day row per
    (title, [(time, activity), ...]) tuple, then that day's activities."""
    for day_number, (title, activities) in enumerate(days, start=1):
        day_id = execute(
            f"INSERT INTO {day_table} ({parent_column}, day_number, title) VALUES (%s, %s, %s)",
            (parent_id, day_number, title),
        )
        for sort_order, (activity_time, activity) in enumerate(activities):
            execute(
                f"INSERT INTO {activity_table} (day_id, sort_order, activity_time, activity) VALUES (%s, %s, %s, %s)",
                (day_id, sort_order, activity_time, activity),
            )


def backfill_package_itinerary():
    """Real, published-itinerary-style day plans for the packages that ship
    with this app -- times and stops follow how these routes are actually
    run (the Everest Base Camp days match the standard Lukla-Namche-
    Tengboche-Dingboche-Gorak Shep-Kala Patthar schedule used across
    published Nepal trekking itineraries). Keyed by package name so this
    only ever fills in real packages, never invents new ones."""
    itineraries = {
        "Pokhara & Kathmandu Heritage Tour": [
            ("Arrival in Kathmandu", [
                ("07:30", "Arrive at Tribhuvan International Airport, met and transferred to your hotel."),
                ("14:00", "Check in and rest, then an easy orientation walk to Kathmandu Durbar Square."),
                ("18:30", "Welcome dinner featuring traditional Nepali dal-bhat."),
            ]),
            ("Kathmandu & Patan heritage", [
                ("08:00", "Breakfast, then a guided heritage walk through Kathmandu Durbar Square."),
                ("12:30", "Lunch near Patan, followed by an afternoon exploring Patan Durbar Square."),
                ("17:00", "Evening free to explore Thamel's markets and cafes."),
            ]),
            ("Drive to Pokhara", [
                ("07:00", "Breakfast, then depart Kathmandu for the scenic drive to Pokhara."),
                ("13:00", "Lunch en route with mountain views along the Prithvi Highway."),
                ("16:30", "Arrive in Pokhara, check in, evening free at Lakeside."),
            ]),
            ("Phewa Lake & Sarangkot sunrise", [
                ("05:00", "Early departure for the Sarangkot viewpoint to catch sunrise over the Annapurna range."),
                ("09:00", "Return for breakfast, then a relaxed boat ride on Phewa Lake."),
                ("15:00", "Free afternoon for Lakeside cafes, optional visit to the World Peace Pagoda."),
            ]),
            ("Departure", [
                ("08:00", "Breakfast and check-out."),
                ("10:00", "Free time for last-minute shopping around Lakeside."),
                ("13:00", "Transfer to the airport, or onward journey, for departure."),
            ]),
        ],
        "Chitwan Wildlife Safari": [
            ("Arrival in Sauraha", [
                ("09:00", "Depart for the drive to Sauraha, Chitwan."),
                ("14:00", "Check in and lunch, followed by a short village walk."),
                ("18:00", "Evening Tharu cultural stick dance performance."),
            ]),
            ("Jungle safari day", [
                ("06:30", "Early morning jeep safari through Chitwan National Park."),
                ("13:00", "Lunch, then an afternoon canoe ride on the Rapti River."),
                ("16:30", "Visit a nearby Tharu village and the Elephant Breeding Centre."),
            ]),
            ("Nature walk & departure", [
                ("07:00", "Guided nature walk near the national park buffer zone."),
                ("09:00", "Breakfast and check-out."),
                ("10:30", "Transfer back to Kathmandu or Pokhara."),
            ]),
        ],
        "Everest Base Camp Trek": [
            ("Fly to Lukla, trek to Phakding", [
                ("06:30", "Early morning mountain flight from Kathmandu to Lukla."),
                ("08:00", "Begin trekking, descending gently through Chaurikharka toward Phakding."),
                ("15:00", "Arrive in Phakding, overnight in a teahouse."),
            ]),
            ("Trek to Namche Bazaar", [
                ("07:00", "Depart Phakding, crossing suspension bridges over the Dudh Koshi river."),
                ("12:30", "Lunch at Jorsale before the steep climb toward Namche."),
                ("16:00", "Arrive in Namche Bazaar, the region's main Sherpa trading hub."),
            ]),
            ("Acclimatisation day in Namche", [
                ("08:00", "Breakfast, then a short acclimatisation hike toward the Everest View Hotel."),
                ("12:00", "Views of Everest and Ama Dablam before descending."),
                ("15:00", "Free afternoon to explore Namche's market and bakeries."),
            ]),
            ("Trek to Tengboche", [
                ("07:30", "Depart Namche, following the trail through rhododendron forest."),
                ("12:00", "Lunch at Phunki Thanka by the river."),
                ("15:30", "Arrive at Tengboche, visit the region's most famous monastery."),
            ]),
            ("Trek to Dingboche", [
                ("07:30", "Depart Tengboche, descending through Deboche before climbing to Pangboche."),
                ("12:30", "Lunch at Pangboche village."),
                ("16:00", "Arrive in Dingboche, above the treeline in the Imja valley."),
            ]),
            ("Acclimatisation day in Dingboche", [
                ("08:00", "Breakfast, then a hike up to the Nangkartshang viewpoint."),
                ("12:00", "Panoramic views of Makalu and the Imja valley before returning."),
                ("15:00", "Rest and hydrate at the teahouse for the remainder of the day."),
            ]),
            ("Trek to Lobuche", [
                ("07:30", "Depart Dingboche, passing memorials for climbers at Thukla."),
                ("12:00", "Lunch at Thukla before the final climb to Lobuche."),
                ("15:30", "Arrive in Lobuche, overnight near the Khumbu Glacier."),
            ]),
            ("Gorak Shep & Everest Base Camp", [
                ("07:00", "Depart Lobuche for Gorak Shep, arriving by late morning."),
                ("13:00", "Lunch, then trek across the glacier moraine to Everest Base Camp."),
                ("17:00", "Return to Gorak Shep for the night."),
            ]),
            ("Kala Patthar sunrise, descend to Pheriche", [
                ("04:30", "Pre-dawn climb to Kala Patthar for sunrise views of Everest."),
                ("09:00", "Return to Gorak Shep for breakfast, then begin the descent."),
                ("15:00", "Arrive in Pheriche for the night."),
            ]),
            ("Trek down to Namche Bazaar", [
                ("07:30", "Depart Pheriche, retracing the trail through Tengboche."),
                ("12:30", "Lunch at Sanasa."),
                ("16:00", "Arrive back in Namche Bazaar."),
            ]),
            ("Trek down to Lukla", [
                ("07:00", "Depart Namche for the final descent to Lukla."),
                ("12:30", "Lunch at Jorsale or Monjo."),
                ("15:30", "Arrive in Lukla, celebration dinner with the trekking crew."),
            ]),
            ("Buffer day", [
                ("08:00", "Contingency day held free in case of weather delays to mountain flights."),
                ("11:00", "Optional easy walk around Lukla and nearby Chaurikharka village."),
                ("17:00", "Rest and relax at the teahouse, celebrating the completed trek."),
            ]),
            ("Fly back to Kathmandu", [
                ("07:00", "Morning flight from Lukla back to Kathmandu."),
                ("12:00", "Check in at the hotel, free afternoon to rest and shower after the trek."),
                ("19:00", "Farewell dinner in Kathmandu with the trekking group."),
            ]),
            ("Departure", [
                ("09:00", "Breakfast and check-out."),
                ("11:00", "Free time for last-minute shopping in Thamel."),
                ("15:00", "Transfer to Tribhuvan International Airport for departure."),
            ]),
        ],
        "Lumbini Buddhist Pilgrimage": [
            ("Arrival & Maya Devi Temple", [
                ("09:00", "Arrive in Lumbini and check in."),
                ("11:00", "Visit the sacred Maya Devi Temple, marking the birthplace of Buddha."),
                ("15:00", "Guided pilgrimage walk through the Sacred Garden and Ashoka Pillar."),
            ]),
            ("Monastic Zone & departure", [
                ("07:00", "Morning meditation session led by a local monastery."),
                ("09:30", "Cycling tour through the Monastic Zone's international temples."),
                ("13:00", "Lunch, then transfer for departure."),
            ]),
        ],
        "Nagarkot Sunrise Getaway": [
            ("Arrival in Nagarkot", [
                ("15:00", "Depart Kathmandu for the drive to Nagarkot."),
                ("17:00", "Check in and settle in with panoramic Himalayan views."),
                ("19:00", "Dinner with mountain views, weather permitting."),
            ]),
            ("Sunrise & hike", [
                ("05:30", "Early wake-up for sunrise viewing from the Nagarkot viewpoint tower."),
                ("08:00", "Breakfast, then a short hike along the panoramic ridge trails."),
                ("11:00", "Return transfer to Kathmandu."),
            ]),
        ],
        "Bandipur Heritage Weekend": [
            ("Arrival & bazaar walk", [
                ("11:00", "Arrive in Bandipur and check in."),
                ("14:00", "Explore the pedestrian Newari bazaar on foot."),
                ("18:00", "Sunset views from Tundikhel, followed by dinner."),
            ]),
            ("Siddha Cave & departure", [
                ("08:00", "Breakfast, then a morning visit to Siddha Cave."),
                ("11:00", "Local cuisine tasting in the bazaar."),
                ("13:00", "Check-out and return transfer."),
            ]),
        ],
        "Rara Lake Wilderness Trek": [
            ("Fly to Talcha", [
                ("07:00", "Morning flight from Kathmandu/Nepalgunj to the remote Talcha airstrip."),
                ("10:00", "Short trek to the edge of Rara National Park."),
                ("16:00", "Set up camp and rest for the night."),
            ]),
            ("Trek to Rara Lake", [
                ("07:30", "Breakfast, then a guided trek through forest trails."),
                ("12:30", "Lunch break along the trail."),
                ("15:00", "Arrive at the shore of Rara Lake, Nepal's largest lake."),
            ]),
            ("Lakeside exploration", [
                ("08:00", "Full-day walk along the lakeside trail."),
                ("12:00", "Picnic lunch by the lake."),
                ("16:00", "Free time for nature photography as the light changes."),
            ]),
            ("Murma Top viewpoint", [
                ("07:00", "Depart camp for the climb to Murma Top."),
                ("11:00", "Panoramic views over the lake and surrounding peaks."),
                ("15:00", "Descend back to the lakeside camp."),
            ]),
            ("Rara National Park wildlife walk", [
                ("08:00", "Guided walk through the park's forest in search of wildlife."),
                ("12:30", "Lunch at camp."),
                ("16:00", "Relaxed evening by the lake."),
            ]),
            ("Trek back to Talcha", [
                ("07:30", "Depart the lakeside camp, retracing the trail toward Talcha."),
                ("12:30", "Lunch en route."),
                ("16:00", "Arrive back near Talcha, evening camp."),
            ]),
            ("Fly back", [
                ("07:00", "Morning flight back to Kathmandu/Nepalgunj."),
                ("10:00", "Free time to rest after the trek before onward travel."),
                ("14:00", "Onward departure or connecting flight home."),
            ]),
        ],
        "Upper Mustang Jeep Expedition": [
            ("Drive to Jomsom via Kali Gandaki Valley", [
                ("06:00", "Early departure from Pokhara along the Kali Gandaki gorge."),
                ("13:00", "Lunch in Tatopani or Ghasa en route."),
                ("16:00", "Arrive in Jomsom, overnight stop."),
            ]),
            ("Muktinath visit", [
                ("07:30", "Depart Jomsom for the sacred Muktinath temple."),
                ("10:00", "Pilgrimage visit to the 108 water spouts and eternal flame."),
                ("14:00", "Continue the jeep tour onward toward Kagbeni."),
            ]),
            ("Drive to Lo Manthang", [
                ("07:00", "Depart Kagbeni for the full-day jeep tour into the restricted area."),
                ("13:00", "Lunch at Ghami or Charang."),
                ("17:00", "Arrive in Lo Manthang, the walled former royal capital."),
            ]),
            ("Lo Manthang exploration", [
                ("08:00", "Cultural walk through the ancient royal palace."),
                ("11:00", "Visit the Jampa and Thubchen monasteries within the city walls."),
                ("15:00", "Free afternoon to explore the town."),
            ]),
            ("Chosar caves & monasteries", [
                ("08:00", "Jeep excursion to the ancient sky caves near Chosar."),
                ("11:00", "Visit the Nyphu and Jhampa monastery complexes."),
                ("15:00", "Return to Lo Manthang for the evening."),
            ]),
            ("Drive back toward Jomsom", [
                ("07:00", "Depart Lo Manthang, retracing the route with village stops along the way."),
                ("13:00", "Lunch at Ghami."),
                ("16:30", "Arrive back in Jomsom."),
            ]),
            ("Jomsom to Pokhara", [
                ("07:00", "Depart Jomsom for the return journey to Pokhara."),
                ("13:00", "Lunch en route."),
                ("16:00", "Arrive in Pokhara, free evening to relax by the lake."),
            ]),
            ("Departure", [
                ("08:00", "Breakfast and check-out."),
                ("10:00", "Free time for last-minute shopping."),
                ("13:00", "Transfer for onward departure."),
            ]),
        ],
        "Janakpur Mithila Culture Tour": [
            ("Arrival & Janaki Mandir", [
                ("11:00", "Arrive in Janakpur and check in."),
                ("14:00", "Visit the ornate Janaki Mandir temple."),
                ("18:00", "Evening food exploration in the local bazaar."),
            ]),
            ("Mithila art & Ganga Sagar", [
                ("08:00", "Hands-on traditional Mithila art workshop."),
                ("11:00", "Visit the sacred Ganga Sagar pond."),
                ("13:00", "Lunch, then departure."),
            ]),
        ],
        "Mustang and Manag": [
            ("Drive to the Manang valley gateway", [
                ("06:00", "Early departure from Kathmandu along the Marsyangdi river."),
                ("13:00", "Lunch en route in Besisahar."),
                ("17:00", "Arrive at the gateway to the Manang valley."),
            ]),
            ("On to Manang village", [
                ("07:30", "Depart for Manang village, following the valley trail."),
                ("12:30", "Lunch en route."),
                ("16:00", "Arrive in the historic village of Manang."),
            ]),
            ("Acclimatisation day in Manang", [
                ("08:00", "Short acclimatisation hike around Manang."),
                ("12:00", "Views of Gangapurna and the surrounding peaks."),
                ("15:00", "Rest afternoon to prepare for the pass crossing."),
            ]),
            ("Cross Thorong La pass", [
                ("04:00", "Very early departure to cross the high Thorong La pass."),
                ("10:00", "Reach the pass summit, then begin the descent."),
                ("15:00", "Arrive in the Muktinath area for the night."),
            ]),
            ("Muktinath to Kagbeni", [
                ("08:00", "Visit the sacred Muktinath temple."),
                ("11:00", "Depart for Kagbeni, following the Kali Gandaki valley."),
                ("15:00", "Arrive in the historic river village of Kagbeni."),
            ]),
            ("Kagbeni to Jomsom", [
                ("08:00", "Depart Kagbeni for Jomsom along the valley."),
                ("12:00", "Lunch in Jomsom."),
                ("15:00", "Free afternoon to rest."),
            ]),
            ("Return to Pokhara", [
                ("07:00", "Morning flight or jeep from Jomsom to Pokhara."),
                ("11:00", "Free time in Pokhara to rest and relax by Phewa Lake."),
                ("14:00", "Onward departure or connecting transport home."),
            ]),
        ],
    }
    for name, days in itineraries.items():
        package = query("SELECT id FROM packages WHERE name = %s", (name,), fetch="one")
        if package is None:
            continue
        _insert_itinerary_days(days, "package_itinerary_days", "package_itinerary_activities", "package_id", package["id"])


def backfill_place_itinerary():
    """A short suggested itinerary for each standalone destination -- sized
    to a realistic first visit rather than the full range in that place's
    'ideal stay' (e.g. a 3-day highlights version of a 12-16 day trek), keyed
    by slug so it only ever fills in real places."""
    itineraries = {
        "kathmandu-valley": [
            ("Kathmandu Durbar Square & Thamel", [
                ("08:00", "Breakfast, then a guided walk through Kathmandu Durbar Square's temples and palaces."),
                ("12:30", "Lunch near Basantapur, then free time to browse Thamel's shops."),
                ("18:00", "Evening stroll through Thamel's cafes and markets."),
            ]),
            ("Patan & Bhaktapur day trip", [
                ("08:30", "Depart for Patan to explore its Newari courtyards and the Patan Museum."),
                ("12:00", "Lunch in Patan."),
                ("14:00", "Continue to Bhaktapur to see its medieval Durbar Square and pottery square."),
            ]),
        ],
        "pokhara": [
            ("Phewa Lake & Lakeside", [
                ("09:00", "Arrive in Pokhara and check in."),
                ("14:00", "Relaxed boat ride on Phewa Lake."),
                ("18:00", "Evening at Lakeside's restaurants and bars."),
            ]),
            ("Sarangkot sunrise & Peace Pagoda", [
                ("05:00", "Early hike or drive up to Sarangkot for sunrise over the Annapurna range."),
                ("09:00", "Breakfast back in Pokhara."),
                ("14:00", "Visit the World Peace Pagoda across the lake."),
            ]),
            ("Caves & gorges", [
                ("09:00", "Visit Mahendra Cave."),
                ("11:00", "Explore Devi's Fall."),
                ("15:00", "Free time for cafes and shopping around Lakeside."),
            ]),
        ],
        "chitwan": [
            ("Jungle safari", [
                ("06:30", "Morning jeep safari through Chitwan National Park."),
                ("13:00", "Lunch at the lodge."),
                ("16:00", "Visit the Elephant Breeding Centre."),
            ]),
            ("Canoe ride & Tharu village", [
                ("07:30", "Morning canoe ride on the Rapti River."),
                ("11:00", "Visit a local Tharu village and cultural museum."),
                ("18:00", "Evening Tharu stick dance performance."),
            ]),
        ],
        "lumbini": [
            ("Maya Devi Temple & Sacred Garden", [
                ("09:00", "Visit the Maya Devi Temple, the birthplace of Buddha."),
                ("11:00", "Walk through the peaceful Sacred Garden and Ashoka Pillar."),
                ("15:00", "Visit the Lumbini Museum."),
            ]),
            ("Monastic Zone cycling tour", [
                ("07:00", "Morning meditation session."),
                ("09:00", "Cycling tour through the Monastic Zone's international monasteries."),
                ("13:00", "Lunch and departure."),
            ]),
        ],
        "everest-region": [
            ("Fly to Lukla & trek to Phakding", [
                ("06:30", "Scenic mountain flight from Kathmandu to Lukla."),
                ("08:00", "Begin trekking down into the Khumbu valley."),
                ("15:00", "Arrive in Phakding for the night."),
            ]),
            ("Trek to Namche Bazaar", [
                ("07:00", "Depart Phakding, crossing suspension bridges over the Dudh Koshi."),
                ("12:30", "Lunch at Jorsale."),
                ("16:00", "Arrive in Namche Bazaar."),
            ]),
            ("Everest View Hotel viewpoint", [
                ("08:00", "Hike up to the Everest View Hotel for panoramic mountain views."),
                ("12:00", "Return to Namche for lunch."),
                ("14:00", "Begin the trek back down toward Lukla."),
            ]),
        ],
        "nagarkot": [
            ("Sunrise viewpoint & short hike", [
                ("05:30", "Early sunrise viewing from the Nagarkot viewpoint tower."),
                ("08:00", "Breakfast with Himalayan views."),
                ("10:00", "Short hike along the panoramic ridge trails."),
            ]),
        ],
        "bandipur": [
            ("Heritage bazaar walk & Siddha Cave", [
                ("09:00", "Guided walk through Bandipur's pedestrian Newari bazaar."),
                ("12:00", "Lunch featuring local Newari cuisine."),
                ("14:00", "Visit the nearby Siddha Cave."),
            ]),
        ],
        "mustang": [
            ("Drive along Kali Gandaki Valley to Jomsom", [
                ("06:00", "Depart Pokhara for the jeep journey along the Kali Gandaki gorge."),
                ("13:00", "Lunch in Tatopani or Ghasa."),
                ("16:00", "Arrive in Jomsom."),
            ]),
            ("Muktinath & onward to Lo Manthang", [
                ("07:30", "Visit the sacred Muktinath temple."),
                ("11:00", "Depart for the walled kingdom of Lo Manthang."),
                ("17:00", "Arrive in Lo Manthang."),
            ]),
            ("Lo Manthang exploration", [
                ("08:00", "Cultural walk through the ancient royal palace."),
                ("11:00", "Visit the Jampa and Thubchen monasteries."),
                ("15:00", "Free time to explore the walled city."),
            ]),
        ],
        "rara-lake": [
            ("Fly to Talcha & trek to Rara Lake", [
                ("07:00", "Morning flight to the remote Talcha airstrip."),
                ("10:00", "Short forest trek toward Rara National Park."),
                ("15:00", "Arrive at the shore of Rara Lake."),
            ]),
            ("Lakeside trail & Murma Top viewpoint", [
                ("08:00", "Full-day walk along the lake's trail."),
                ("12:00", "Picnic lunch by the lake."),
                ("14:00", "Climb to the Murma Top viewpoint for panoramic views."),
            ]),
            ("Return trek to Talcha", [
                ("07:30", "Depart the lakeside camp, retracing the trail toward Talcha."),
                ("12:30", "Lunch en route."),
                ("16:00", "Arrive near Talcha ahead of the flight out."),
            ]),
        ],
        "janakpur": [
            ("Janaki Mandir & Mithila art", [
                ("09:00", "Visit the ornate Janaki Mandir temple."),
                ("12:00", "Lunch, then a hands-on traditional Mithila art workshop."),
                ("15:00", "Visit the Ganga Sagar pond."),
            ]),
        ],
    }
    for slug, days in itineraries.items():
        place = query("SELECT id FROM explore_places WHERE slug = %s", (slug,), fetch="one")
        if place is None:
            continue
        _insert_itinerary_days(days, "place_itinerary_days", "place_itinerary_activities", "place_id", place["id"])


def backfill_itinerary_extras():
    """What to pack, and what to actually be careful about, for each
    package/place -- grounded in real, well-known Nepal travel practicalities
    (altitude sickness above 3,000m on the Everest/Mustang/Manang routes,
    Upper Mustang's restricted-area permit, temple dress codes, Terai heat,
    safari safety) rather than generic filler repeated for every trip."""
    package_extras = {
        "Pokhara & Kathmandu Heritage Tour": (
            ["Comfortable walking shoes", "Light layers for cool mornings and evenings",
             "Modest clothing covering shoulders and knees for temple visits", "Sunscreen and sunglasses",
             "A reusable water bottle", "A small daypack for day trips"],
            ["Kathmandu's traffic and streets can be chaotic -- stay alert when crossing roads",
             "Keep valuables secure in crowded tourist areas like Thamel and Durbar Square",
             "Remove shoes before entering temple courtyards",
             "Altitude here is moderate, so no special acclimatisation is needed"],
        ),
        "Chitwan Wildlife Safari": (
            ["Light, breathable clothing in neutral colours (khaki/green, not bright colours)",
             "Closed-toe shoes for jungle walks", "Insect repellent", "Sunscreen and a hat",
             "Binoculars, if you have them"],
            ["Always follow your naturalist guide's instructions during safaris and keep a safe distance from wildlife",
             "Malaria precautions may be advisable depending on the season -- check with a travel clinic beforehand",
             "The Rapti River canoe ride depends on water levels and may be adjusted or substituted"],
        ),
        "Everest Base Camp Trek": (
            ["Warm layered clothing and a down jacket", "A four-season sleeping bag or liner",
             "Sturdy, already broken-in trekking boots", "Trekking poles", "A headlamp with spare batteries",
             "Sunglasses rated for high-altitude UV", "A basic first-aid kit",
             "A power bank -- electricity is limited and sometimes charged for above Namche",
             "Enough Nepalese cash -- there are no ATMs beyond Namche Bazaar"],
            ["Altitude sickness is a real risk above 3,000m -- the acclimatisation days in Namche and Dingboche are not optional",
             "Travel insurance with high-altitude rescue/evacuation cover is essential",
             "Lukla flights are frequently delayed by weather -- keep spare days in any onward travel plans",
             "A TIMS card and Sagarmatha National Park permit are required and are arranged as part of this package"],
        ),
        "Lumbini Buddhist Pilgrimage": (
            ["Modest clothing covering shoulders and knees for temple and monastery visits",
             "Comfortable shoes for walking and cycling", "A sun hat", "A reusable water bottle"],
            ["Remove shoes before entering temples and monasteries",
             "Photography is restricted in some meditation zones inside the Sacred Garden",
             "The Terai region can be very hot in summer -- carry sun protection and stay hydrated"],
        ),
        "Nagarkot Sunrise Getaway": (
            ["Warm layers for the early sunrise viewing -- mornings are cold even in warmer months",
             "Comfortable walking shoes", "A camera or phone with good battery life for the view"],
            ["The sunrise viewpoint can get crowded in peak season -- arrive early for the best spot",
             "Mountain visibility depends heavily on the weather and season"],
        ),
        "Bandipur Heritage Weekend": (
            ["Comfortable walking shoes for the town's cobblestone lanes", "A light jacket for cool evenings",
             "A camera for the heritage architecture"],
            ["Some heritage lanes are narrow, cobblestoned, and involve uphill walking -- may not suit those with mobility difficulties",
             "The walk to Siddha Cave's entrance can be short but slippery after rain"],
        ),
        "Rara Lake Wilderness Trek": (
            ["Warm layered clothing", "Rain gear", "Sturdy trekking boots", "A sleeping bag if camping",
             "A basic first-aid kit", "A headlamp", "Enough cash -- there's no ATM access in the region"],
            ["This is a remote area with very limited medical facilities -- travel insurance with evacuation cover is essential",
             "Flights to Talcha airstrip are weather-dependent and can be delayed",
             "Mobile network coverage is patchy to non-existent for most of the trek"],
        ),
        "Upper Mustang Jeep Expedition": (
            ["Warm, windproof clothing -- the trans-Himalayan desert is dry, dusty, and windy",
             "A scarf or dust mask", "Sunglasses", "Motion-sickness tablets for the long jeep rides",
             "Your passport -- the restricted-area permit must be carried at all times"],
            ["Upper Mustang is a restricted area requiring a special permit, which is arranged as part of this package",
             "Roads are rough and dusty, and can be affected by landslides during the monsoon",
             "Nights can be very cold despite warm days"],
        ),
        "Janakpur Mithila Culture Tour": (
            ["Light, breathable clothing -- the Terai region is hot for most of the year", "A sun hat",
             "Modest clothing for the Janaki Mandir temple visit"],
            ["Temperatures can be very high, especially before the monsoon -- stay hydrated and avoid the midday sun where possible",
             "Dress modestly when visiting the temple"],
        ),
        "Mustang and Manag": (
            ["Warm layered clothing for high-altitude nights", "A good sleeping bag", "Sturdy trekking boots",
             "Trekking poles", "Altitude sickness medication -- consult a doctor before travel",
             "Sunglasses and sunscreen for high-altitude UV"],
            ["Crossing Thorong La pass (over 5,000m) is physically demanding and entirely weather-dependent -- the schedule may shift by a day",
             "Altitude sickness precautions are essential above Manang",
             "Some sections have little to no mobile network coverage"],
        ),
    }
    for name, (packing_list, things_to_know) in package_extras.items():
        package = query("SELECT id FROM packages WHERE name = %s", (name,), fetch="one")
        if package is None:
            continue
        execute(
            "INSERT INTO package_itinerary_extras (package_id, packing_list, things_to_know) VALUES (%s, %s, %s)",
            (package["id"], json.dumps(packing_list), json.dumps(things_to_know)),
        )

    place_extras = {
        "kathmandu-valley": (
            ["Comfortable walking shoes", "Modest clothing for temple visits", "Sunscreen and sunglasses",
             "A reusable water bottle"],
            ["Traffic and streets can be chaotic -- stay alert when crossing roads",
             "Keep valuables secure in crowded areas like Thamel and Durbar Square",
             "Remove shoes before entering temple courtyards"],
        ),
        "pokhara": (
            ["Comfortable walking shoes", "Light layers for the lake in the evening", "Sunscreen and sunglasses",
             "A swimsuit if you plan to visit the lake"],
            ["Sarangkot's sunrise view depends on clear weather -- have a backup morning if it's cloudy",
             "Lakeside can get busy with vendors -- agree on prices before buying"],
        ),
        "chitwan": (
            ["Light, breathable clothing in neutral colours", "Closed-toe shoes for jungle walks",
             "Insect repellent", "Sunscreen and a hat"],
            ["Always follow your guide's instructions during safaris and keep a safe distance from wildlife",
             "Malaria precautions may be advisable depending on the season"],
        ),
        "lumbini": (
            ["Modest clothing covering shoulders and knees", "Comfortable shoes for walking and cycling",
             "A sun hat"],
            ["Remove shoes before entering temples and monasteries",
             "The Terai region can be very hot -- carry sun protection and stay hydrated"],
        ),
        "everest-region": (
            ["Warm layered clothing and a down jacket", "Sturdy, already broken-in trekking boots",
             "Sunglasses rated for high-altitude UV", "A power bank", "Enough cash -- limited ATM access"],
            ["Altitude sickness is a real risk even on a short trek into the region above 3,000m",
             "Lukla flights are frequently delayed by weather -- keep spare days if flying onward"],
        ),
        "nagarkot": (
            ["Warm layers -- mornings are cold even in warmer months", "Comfortable walking shoes"],
            ["The viewpoint can get crowded in peak season -- arrive early for the best spot",
             "Mountain visibility depends heavily on the weather and season"],
        ),
        "bandipur": (
            ["Comfortable walking shoes for cobblestone lanes", "A light jacket for cool evenings"],
            ["Some lanes are narrow and involve uphill walking -- may not suit those with mobility difficulties"],
        ),
        "mustang": (
            ["Warm, windproof clothing", "A scarf or dust mask", "Sunglasses", "Motion-sickness tablets",
             "Your passport -- a restricted-area permit is required"],
            ["Upper Mustang is a restricted area requiring a special permit arranged in advance",
             "Roads are rough and dusty, and nights can be very cold despite warm days"],
        ),
        "rara-lake": (
            ["Warm layered clothing", "Rain gear", "Sturdy trekking boots", "Enough cash -- no ATM access"],
            ["This is a remote area with very limited medical facilities",
             "Flights to Talcha airstrip are weather-dependent and can be delayed"],
        ),
        "janakpur": (
            ["Light, breathable clothing", "A sun hat", "Modest clothing for the temple visit"],
            ["Temperatures can be very high, especially before the monsoon -- stay hydrated"],
        ),
    }
    for slug, (packing_list, things_to_know) in place_extras.items():
        place = query("SELECT id FROM explore_places WHERE slug = %s", (slug,), fetch="one")
        if place is None:
            continue
        execute(
            "INSERT INTO place_itinerary_extras (place_id, packing_list, things_to_know) VALUES (%s, %s, %s)",
            (place["id"], json.dumps(packing_list), json.dumps(things_to_know)),
        )


def backfill_departure_info():
    """Real departure logistics and genuinely optional, additional-cost
    activities for each package -- e.g. a heli return from Everest Base Camp
    or paragliding over Pokhara aren't included in the package price, so
    travellers should see that cost up front rather than being surprised by
    it on the trip. Packages only (this is what's actually booked)."""
    # Every extra-expense line below is deliberately written without a
    # comma anywhere in it -- these are stored as a comma-separated list
    # (same convention as packing_list/things_to_know/activities elsewhere
    # in this app), so a comma inside one entry (e.g. from a thousands
    # separator like "Rs 8,000") would silently split that single entry
    # into two when an admin re-saves the form. "Rs 8000" instead of
    # "Rs 8,000" is the trade-off for that round-trip to stay safe.
    departures = {
        "Pokhara & Kathmandu Heritage Tour": (
            "7:00 AM", "Kathmandu -- Thamel tourist bus stand",
            ["Paragliding over Pokhara (optional) -- approx. Rs 8000-10000 per person",
             "Zip-flying at Sarangkot (optional) -- approx. Rs 4500 per person",
             "Cultural dance dinner show in Kathmandu (optional) -- approx. Rs 1200 per person"],
        ),
        "Chitwan Wildlife Safari": (
            "6:30 AM", "Kathmandu -- Naya Bus Park (or Pokhara Tourist Bus Park)",
            ["Elephant bathing experience (optional) -- approx. Rs 500 per person",
             "Extended canoe ride along the Rapti River (optional) -- approx. Rs 800 per person",
             "Guided night jungle walk where permitted (optional) -- approx. Rs 1500 per person"],
        ),
        "Everest Base Camp Trek": (
            "Early morning flight", "Kathmandu -- Tribhuvan International Airport domestic terminal (flight to Lukla)",
            ["Helicopter return from Gorak Shep instead of trekking back (optional and weather-dependent) -- approx. USD 500-650 per person",
             "Extra personal porter (optional) -- approx. Rs 2500 per day",
             "Satellite phone rental for remote sections (optional) -- approx. Rs 500 per day"],
        ),
        "Lumbini Buddhist Pilgrimage": (
            "6:00 AM", "Kathmandu -- Naya Bus Park (or domestic flight to Bhairahawa)",
            ["Guided meditation session with a resident monk (optional) -- approx. Rs 1000 per person",
             "Bicycle rental to tour the Sacred Garden (optional) -- approx. Rs 300 per day"],
        ),
        "Nagarkot Sunrise Getaway": (
            "3:00 PM (afternoon departure for a sunrise the next morning)", "Kathmandu -- central pick-up point",
            ["Guided sunrise hike toward Changunarayan (optional) -- approx. Rs 1500 per person",
             "Horse riding at the viewpoint (optional) -- approx. Rs 800 per person"],
        ),
        "Bandipur Heritage Weekend": (
            "8:00 AM", "Kathmandu or Pokhara -- Prithvi Highway pick-up point",
            ["Guided Siddha Cave exploration with headlamp rental (optional) -- approx. Rs 500 per person",
             "Guided hike to Thani Mai Temple viewpoint (optional) -- approx. Rs 700 per person"],
        ),
        "Rara Lake Wilderness Trek": (
            "Early morning flight", "Kathmandu or Nepalgunj -- domestic terminal (flight to Talcha airstrip)",
            ["Extra camping night with full board (optional) -- approx. Rs 3500 per night",
             "Additional porter or horse for heavier bags (optional) -- approx. Rs 2000 per day"],
        ),
        "Upper Mustang Jeep Expedition": (
            "6:00 AM", "Pokhara -- drives to Jomsom before continuing by jeep",
            ["Extending your stay in Lo Manthang beyond the package's covered days -- restricted-area permit costs approx. USD 50 per day",
             "Guided visit to the Mustang sky caves (optional) -- approx. Rs 1000 per person"],
        ),
        "Janakpur Mithila Culture Tour": (
            "Domestic flight or overnight bus", "Kathmandu -- domestic terminal or Naya Bus Park",
            ["Mithila art workshop with a local artist (optional) -- approx. Rs 1500 per person",
             "Cycle rickshaw city tour (optional) -- approx. Rs 500 per person"],
        ),
        "Mustang and Manag": (
            "6:00 AM", "Kathmandu -- drives via Besisahar to the trailhead",
            ["Upgraded porter/guide support for the Thorong La crossing (optional) -- approx. Rs 2500 per day",
             "Additional high-altitude rescue/evacuation insurance top-up (strongly recommended) -- approx. USD 20"],
        ),
    }
    for name, (departure_time, departure_point, extra_expenses) in departures.items():
        package = query("SELECT id FROM packages WHERE name = %s", (name,), fetch="one")
        if package is None:
            continue
        execute(
            "UPDATE package_itinerary_extras SET departure_time = %s, departure_point = %s, extra_expenses = %s "
            "WHERE package_id = %s",
            (departure_time, departure_point, json.dumps(extra_expenses), package["id"]),
        )


def ensure_group_messages_table():
    execute(
        """
        CREATE TABLE IF NOT EXISTS group_messages (
            id INT AUTO_INCREMENT PRIMARY KEY,
            group_id INT NOT NULL,
            user_id INT NOT NULL,
            message VARCHAR(1000) NOT NULL,
            created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (group_id) REFERENCES travel_groups(id) ON DELETE CASCADE,
            FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
        )
        """
    )


# One row per password-reset OTP that's ever been issued -- a code is only
# usable once (used_at) and only for a short window (expires_at), so a stale
# or already-used code can't reset a password.
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
    # Same idea, for saving a destination from Explore Places rather than a
    # bookable package.
    execute(
        """
        CREATE TABLE IF NOT EXISTS place_wishlist_items (
            id INT AUTO_INCREMENT PRIMARY KEY,
            user_id INT NOT NULL,
            place_id INT NOT NULL,
            created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
            UNIQUE KEY uniq_user_place (user_id, place_id),
            FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE,
            FOREIGN KEY (place_id) REFERENCES explore_places(id) ON DELETE CASCADE
        )
        """
    )


def ensure_package_reviews_table():
    # One review per booking (not per traveller) -- someone who books the
    # same package twice on two different trips can review each trip
    # separately, since they're genuinely different experiences.
    execute(
        """
        CREATE TABLE IF NOT EXISTS package_reviews (
            id INT AUTO_INCREMENT PRIMARY KEY,
            package_id INT NOT NULL,
            user_id INT NOT NULL,
            booking_id INT NOT NULL,
            rating TINYINT NOT NULL,
            comment TEXT NULL,
            created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
            UNIQUE KEY uniq_booking_review (booking_id),
            FOREIGN KEY (package_id) REFERENCES packages(id) ON DELETE CASCADE,
            FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE,
            FOREIGN KEY (booking_id) REFERENCES bookings(id) ON DELETE CASCADE
        )
        """
    )


# Help Centre: a traveller's question (optionally about one booking) and the
# admin's answer. One reply per ticket keeps this simple -- a follow-up is
# just a new ticket -- and every state change also raises a notification.
def ensure_support_tickets_table():
    execute(
        """
        CREATE TABLE IF NOT EXISTS support_tickets (
            id INT AUTO_INCREMENT PRIMARY KEY,
            user_id INT NOT NULL,
            booking_id INT NULL,
            subject VARCHAR(120) NOT NULL,
            message TEXT NOT NULL,
            status VARCHAR(20) NOT NULL DEFAULT 'Open',
            admin_reply TEXT NULL,
            replied_at TIMESTAMP NULL,
            created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE,
            FOREIGN KEY (booking_id) REFERENCES bookings(id) ON DELETE SET NULL
        )
        """
    )


def ensure_booking_reminder_column():
    """reminder_sent_at makes trip reminders idempotent: a booking is only
    ever reminded about once, however often the reminder job runs."""
    columns = {row["Field"] for row in query("DESCRIBE bookings")}
    if "reminder_sent_at" not in columns:
        execute("ALTER TABLE bookings ADD COLUMN reminder_sent_at TIMESTAMP NULL")


def run_schema_migrations():
    """Creates any table/column the app needs that the database doesn't have
    yet. Safe to run repeatedly. Called on the first logged-in request, and by
    `python manage.py init-db` when setting up a fresh database."""
    ensure_notifications_table()
    ensure_user_columns()
    ensure_group_columns()
    ensure_group_messages_table()
    ensure_package_columns()
    ensure_payment_columns()
    ensure_admin_activity_table()
    ensure_refund_requests_table()
    ensure_photo_tables()
    ensure_avatar_history_table()
    ensure_wishlist_table()
    ensure_itinerary_tables()
    ensure_package_reviews_table()
    ensure_support_tickets_table()
    ensure_booking_reminder_column()
    ensure_password_reset_table()
    ensure_login_attempts_table()


@app.before_request
def prepare_notifications():
    # Also runs under `flask run`, where the __main__ startup block is skipped.
    if "user_id" in session and not app.config.get("NOTIFICATIONS_READY"):
        run_schema_migrations()
        app.config["NOTIFICATIONS_READY"] = True


def notify_admins(message, link=None):
    for admin in query("SELECT id FROM users WHERE role = 1"):
        notify(admin["id"], message, link)


# CSRF PROTECTION
# One token per session (not one per form) -- generated the first time any
# page asks for it, reused for every form after that. A form posts it back
# as a hidden "csrf_token" field; check_csrf() below rejects any POST whose
# token doesn't match what's in the session, which stops another site from
# submitting one of our forms on a logged-in user's behalf.
CSRF_EXEMPT_ENDPOINTS = {
    # Stripe calls this directly (server-to-server), never through a browser
    # session, so it has no session cookie or CSRF token to check -- it's
    # authenticated instead by Stripe's own request signature.
    "stripe_webhook",
}


def csrf_token():
    return session.get("csrf_token", "")


app.jinja_env.globals["csrf_token"] = csrf_token


@app.before_request
def ensure_csrf_token():
    # Set on every request, not just when a form happens to render -- a page
    # with no visible form on a given load could otherwise leave the session
    # without a token for check_csrf() below to compare against later.
    session.setdefault("csrf_token", secrets.token_hex(32))


@app.before_request
def check_csrf():
    if request.method == "POST" and request.endpoint not in CSRF_EXEMPT_ENDPOINTS:
        expected = session.get("csrf_token", "")
        submitted = request.form.get("csrf_token", "")
        if not expected or not secrets.compare_digest(expected, submitted):
            abort(400)


@app.context_processor
def inject_notifications():
    if "user_id" not in session:
        return {}
    try:
        context = {
            "unread_notification_count": query(
                "SELECT COUNT(*) AS total FROM notifications WHERE user_id = %s AND is_read = 0",
                (session["user_id"],), fetch="one",
            )["total"],
        }
        if session.get("user") == 0:
            # Shown as a small badge on the sidebar's Wishlist link.
            context["wishlist_count"] = query(
                """
                SELECT (SELECT COUNT(*) FROM wishlist_items WHERE user_id = %s)
                     + (SELECT COUNT(*) FROM place_wishlist_items WHERE user_id = %s) AS total
                """,
                (session["user_id"], session["user_id"]), fetch="one",
            )["total"]
        elif session.get("user") == 1:
            context["open_support_count"] = query(
                "SELECT COUNT(*) AS total FROM support_tickets WHERE status = 'Open'", fetch="one",
            )["total"]
        return context
    except Exception:
        # Badges are decoration -- if the database is having a bad moment they
        # must not stop a page (including the error page itself) from rendering.
        app.logger.warning("Could not load sidebar counts", exc_info=True)
        return {}


# SECURITY HEADERS
def _origin_of(url):
    parts = urlsplit(url)
    return f"{parts.scheme}://{parts.netloc}" if parts.scheme and parts.netloc else ""


# Content-Security-Policy. The templates still use inline <script>/<style>
# blocks, so 'unsafe-inline' is kept for those -- but scripts can only be
# loaded from this site and Highcharts, nothing can be embedded as a plugin,
# <base> can't be hijacked, and forms can only post to this site or eSewa.
CONTENT_SECURITY_POLICY = "; ".join([
    "default-src 'self'",
    "script-src 'self' 'unsafe-inline' https://code.highcharts.com",
    "style-src 'self' 'unsafe-inline'",
    "img-src 'self' data: https:",
    "font-src 'self' data:",
    "connect-src 'self'",
    "object-src 'none'",
    "base-uri 'self'",
    "frame-ancestors 'none'",
    "form-action 'self' " + (_origin_of(ESEWA_FORM_URL) or "https://rc-epay.esewa.com.np"),
])


@app.after_request
def add_security_headers(response):
    response.headers.setdefault("Content-Security-Policy", CONTENT_SECURITY_POLICY)
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("X-Frame-Options", "DENY")
    response.headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
    response.headers.setdefault("Permissions-Policy", "camera=(), microphone=(), geolocation=()")
    if app.config["SESSION_COOKIE_SECURE"]:
        response.headers.setdefault("Strict-Transport-Security", "max-age=31536000")
    # A logged-in page (bookings, balance, profile) shouldn't be replayable
    # from the browser's back button on a shared computer after logging out.
    if "user_id" in session and response.mimetype == "text/html":
        response.headers.setdefault("Cache-Control", "no-store")
    return response


# ERROR PAGES
ERROR_MESSAGES = {
    400: ("Something went wrong with that request",
          "The form may have expired. Go back, reload the page and try again."),
    403: ("You don't have access to this page", "If you think this is a mistake, contact support."),
    404: ("We couldn't find that page", "The link may be old, or the page may have moved."),
    405: ("That action isn't allowed here", "Go back and use the buttons on the page instead."),
    413: ("That file is too large", "Please upload something smaller (profile photos must be under 3 MB)."),
    429: ("Too many attempts", "Please wait a few minutes and try again."),
    500: ("Something went wrong on our side",
          "It's been logged and we'll look into it. Please try again in a moment."),
}


def render_error(status_code):
    title, detail = ERROR_MESSAGES.get(status_code, ERROR_MESSAGES[500])
    home_endpoint = "admin_dashboard" if session.get("user") == 1 else ("dashboard" if "user_id" in session else "landing")
    return render_template(
        "error.html", status_code=status_code, title=title, detail=detail, home_url=url_for(home_endpoint),
    ), status_code


@app.errorhandler(HTTPException)
def handle_http_error(error):
    # Redirects (3xx) raised via abort() should still be redirects.
    if error.code and 300 <= error.code < 400:
        return error
    return render_error(error.code or 500)


@app.errorhandler(Exception)
def handle_unexpected_error(error):
    # Full traceback goes to logs/gogather.log; the visitor just sees a
    # friendly page instead of a stack trace. Debug mode keeps Werkzeug's
    # interactive debugger for local development.
    if app.debug or app.testing:
        raise error
    app.logger.exception("Unhandled error on %s %s", request.method, request.path)
    return render_error(500)


# HEALTH CHECK
# For uptime monitors and load balancers: 200 when the app can reach the
# database, 503 when it can't. No login needed and no user data returned.
@app.route("/healthz")
def healthz():
    try:
        query("SELECT 1 AS ok", fetch="one")
    except Exception:
        app.logger.exception("Health check: database unreachable")
        return jsonify(status="error", database="down"), 503
    return jsonify(status="ok", database="up")


@app.route("/robots.txt")
def robots_txt():
    return Response(
        "User-agent: *\nDisallow: /admin\nDisallow: /dashboard\nDisallow: /my_bookings\nDisallow: /profile\n",
        mimetype="text/plain",
    )


@app.route("/favicon.ico")
def favicon():
    return redirect(url_for("static", filename="favicon.png"), code=301)


# PASSWORD STRENGTH
# Same rule everywhere a password is set (register, reset, change) --
# mirrors the client-side pattern in register.html so the server can't be
# tricked into accepting a weaker password than the form allows.
PASSWORD_PATTERN = re.compile(r"(?=.*[a-z])(?=.*[A-Z])(?=.*[0-9])(?=.*[^A-Za-z0-9]).{8,}")
PASSWORD_REQUIREMENTS_MESSAGE = (
    "Password must be at least 8 characters long and contain an uppercase letter, "
    "lowercase letter, number, and special character."
)


def is_strong_password(password):
    return bool(PASSWORD_PATTERN.fullmatch(password or ""))


# SAFE REDIRECTS
def safe_next(target, fallback):
    """Forms carry a hidden 'next' so a button can return you to the page you
    were on. Only same-site paths are honoured -- otherwise a crafted form
    could bounce a logged-in user to a look-alike site (open redirect)."""
    if target and target.startswith("/") and not target.startswith("//") and "\\" not in target:
        return target
    return fallback


def redirect_to_login():
    """Sends a signed-out visitor to the login page. For a page reached
    directly -- a bookmark, a shared link, or a session that simply timed
    out -- this remembers where they were headed (GET requests only) so
    login() can send them back there instead of always landing on the
    generic dashboard."""
    if request.method == "GET":
        return redirect(url_for("login", next=request.full_path.rstrip("?")))
    return redirect(url_for("login"))


# RATE LIMITING
# A small in-memory sliding window, for the endpoints that cost something to
# abuse (signup, password reset, support tickets). Same trade-off as the login
# throttle above: counts live in this process, so with several worker
# processes each keeps its own count -- see DEPLOYMENT.md.
_rate_hits = {}


def rate_limited(bucket, key, limit, window_seconds):
    """True if `key` has already made `limit` calls to `bucket` in the last
    `window_seconds`; otherwise records this call and returns False."""
    now = time.time()
    entry = (bucket, key)
    recent = [stamp for stamp in _rate_hits.get(entry, []) if now - stamp < window_seconds]
    if len(recent) >= limit:
        _rate_hits[entry] = recent
        return True
    recent.append(now)
    _rate_hits[entry] = recent
    if len(_rate_hits) > 5000:
        # Drop keys with nothing recent so a flood of one-off visitors can't
        # grow this dict without bound.
        for stale in [k for k, v in _rate_hits.items() if not v or now - v[-1] > 3600]:
            _rate_hits.pop(stale, None)
    return False


# LANDING PAGE

@app.route("/")
def landing():
    return render_template("landing.html")


@app.route("/privacy")
def privacy():
    return render_template("privacy.html")


@app.route("/terms")
def terms():
    return render_template(
        "terms.html",
        cancellation_fee_percent=CANCELLATION_FEE_PERCENT,
        service_fee_percent=SERVICE_FEE_PERCENT,
    )


# LOGIN ATTEMPT THROTTLING
# Keyed by username+IP so one person mistyping their own password doesn't
# lock out everyone else on the same network, and so guessing many usernames
# from one IP is still slowed down. Counts are kept in the database
# (login_attempts table) so a lockout survives an app
# restart and is shared by every worker process. If the database can't be
# reached the in-memory dict below takes over, so a DB hiccup never turns
# into "nobody can log in".
LOGIN_ATTEMPT_LIMIT = 5
LOGIN_LOCKOUT_SECONDS = 300

_login_attempts = {}  # fallback only -- see above
_login_table_ready = False


def ensure_login_attempts_table():
    execute(
        """
        CREATE TABLE IF NOT EXISTS login_attempts (
            throttle_key VARCHAR(190) NOT NULL PRIMARY KEY,
            failed_count INT NOT NULL DEFAULT 0,
            locked_until DATETIME NULL,
            updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP
        )
        """
    )


def _prepare_login_attempts():
    # Login is the one page that runs before run_schema_migrations() (which
    # waits for a signed-in user), so this table is created on first use.
    global _login_table_ready
    if not _login_table_ready:
        ensure_login_attempts_table()
        _login_table_ready = True


def _login_throttle_key(username):
    return f"{username.strip().lower()}:{request.remote_addr}"[:190]


def _login_locked_seconds_left(key):
    try:
        _prepare_login_attempts()
        row = query(
            "SELECT TIMESTAMPDIFF(SECOND, NOW(), locked_until) AS remaining "
            "FROM login_attempts WHERE throttle_key = %s AND locked_until IS NOT NULL",
            (key,), fetch="one",
        )
        return max(int(row["remaining"]), 0) if row and row["remaining"] is not None else 0
    except Exception:
        app.logger.warning("Login throttle: database unavailable, using in-memory fallback", exc_info=True)
        entry = _login_attempts.get(key)
        if not entry or not entry["locked_until"]:
            return 0
        remaining = entry["locked_until"] - time.time()
        return int(remaining) if remaining > 0 else 0


def _register_failed_login(key):
    try:
        _prepare_login_attempts()
        # locked_until is assigned before failed_count on purpose: MySQL
        # applies ON DUPLICATE KEY assignments left to right, and the lock
        # decision has to see the count from before this failure is added.
        execute(
            """
            INSERT INTO login_attempts (throttle_key, failed_count) VALUES (%s, 1)
            ON DUPLICATE KEY UPDATE
                locked_until = IF(failed_count + 1 >= %s,
                                  DATE_ADD(NOW(), INTERVAL %s SECOND), locked_until),
                failed_count = IF(failed_count + 1 >= %s, 0, failed_count + 1)
            """,
            (key, LOGIN_ATTEMPT_LIMIT, LOGIN_LOCKOUT_SECONDS, LOGIN_ATTEMPT_LIMIT),
        )
    except Exception:
        app.logger.warning("Login throttle: database unavailable, using in-memory fallback", exc_info=True)
        entry = _login_attempts.setdefault(key, {"count": 0, "locked_until": 0})
        entry["count"] += 1
        if entry["count"] >= LOGIN_ATTEMPT_LIMIT:
            entry["locked_until"] = time.time() + LOGIN_LOCKOUT_SECONDS
            entry["count"] = 0


def _clear_login_attempts(key):
    _login_attempts.pop(key, None)
    try:
        _prepare_login_attempts()
        execute("DELETE FROM login_attempts WHERE throttle_key = %s", (key,))
    except Exception:
        app.logger.warning("Login throttle: could not clear attempts", exc_info=True)


# LOGIN PAGE

@app.route("/login", methods=["GET", "POST"])
def login():

    error = None
    next_url = safe_next(request.values.get("next"), "")

    if request.method == "POST":

        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")
        throttle_key = _login_throttle_key(username)
        locked_seconds = _login_locked_seconds_left(throttle_key)

        if locked_seconds:
            error = (
                f"Too many failed login attempts. Please try again in "
                f"{locked_seconds // 60 + 1} minute(s)."
            )
            return render_template("login.html", error=error, next_url=next_url)

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

            _clear_login_attempts(throttle_key)

            # Start from a clean session so nothing from before login (a
            # pre-login CSRF token, a pending "next" value) is carried over,
            # then re-issue a CSRF token and apply the idle timeout.
            session.clear()
            session["csrf_token"] = secrets.token_hex(32)
            session.permanent = True

            # Store login information
            session["user_id"] = logged_in_user["id"]
            session["username"] = logged_in_user["username"]
            session["user"] = logged_in_user["role"]

            # USER = 0 → TRAVELLER

            if logged_in_user["role"] == 0:

                return redirect(next_url or url_for("dashboard"))


            # USER = 1 → ADMIN

            elif logged_in_user["role"] == 1:

                return redirect(next_url or url_for("admin_dashboard"))

        # LOGIN FAILED

        else:

            _register_failed_login(throttle_key)
            error = "Invalid username or password."

    return render_template(
        "login.html",
        error=error,
        next_url=next_url,
    )


# REGISTER PAGE

@app.route("/register", methods=["GET", "POST"])
def register():

    # Registration is reachable before any session exists, so it can't rely
    # on the login-gated before_request hook to have already run this --
    # same reason forgot_password()/wishlist routes call their own
    # ensure_*() directly instead of assuming it already happened.
    ensure_user_columns()

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

        accept_terms = request.form.get("accept_terms") == "on"

        # Sign-ups are throttled per connection so the form can't be used to
        # mass-create accounts.

        if rate_limited("register", request.remote_addr, 10, 3600):

            error = "Too many sign-up attempts from this connection. Please try again in an hour."

        # REQUIRED FIELDS

        elif not (
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

        # PASSWORD STRENGTH

        elif not is_strong_password(password):

            error = PASSWORD_REQUIREMENTS_MESSAGE

        # PHONE FORMAT

        elif phone and not (phone.isdigit() and len(phone) == 10):

            error = "Phone number must be a 10 digit number."

        # TERMS ACCEPTANCE
        # Checked here too, not just via the checkbox's `required` attribute
        # in the template -- a form submitted directly (curl, a modified
        # page) shouldn't be able to skip agreeing to the Terms & Conditions
        # and Refund Policy.

        elif not accept_terms:

            error = "You must accept the Terms & Conditions and Refund Policy to register."

        else:

            try:

                # New registrations are always travellers (role = 0).

                execute(
                    """
                    INSERT INTO users
                        (full_name, username, email, phone, password_hash, role, terms_accepted_at)
                    VALUES (%s, %s, %s, %s, %s, %s, NOW())
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

                return redirect_to_login()

    return render_template(
        "register.html",
        error=error
    )


# FORGOT / RESET PASSWORD (one-time code sent by email)
#
# There's no SMS gateway wired up in this app, so a real "code texted to your
# phone" flow can't actually deliver anything. The OTP is still generated and
# stored the way it would be for SMS delivery (a one-time 6-digit code with a
# short expiry, tied to the phone number on file), it's just handed back on
# the page instead of texted -- swap that hand-back for an actual SMS send
# (Twilio, Sparrow SMS, etc.) before going live.

OTP_VALID_MINUTES = 10


def find_user_for_reset(identifier):
    return query(
        "SELECT id, email FROM users WHERE phone = %s OR email = %s LIMIT 1",
        (identifier, identifier), fetch="one",
    )


@app.route("/forgot-password", methods=["GET", "POST"])
def forgot_password():

    ensure_password_reset_table()

    if request.method == "POST":

        # Accepts the phone number or the email on the account.
        identifier = request.form.get("phone", "").strip()

        if rate_limited("forgot", request.remote_addr, 5, 900) or (
            identifier and rate_limited("forgot-account", identifier.lower(), 3, 900)
        ):

            flash("Too many reset requests. Please wait a few minutes and try again.", "error")
            return render_template("forgot_password.html"), 429

        user = find_user_for_reset(identifier) if identifier else None

        if user:

            otp_code = f"{secrets.randbelow(1000000):06d}"

            # Only the newest code works -- asking again retires the old ones.
            execute(
                "UPDATE password_reset_tokens SET used_at = NOW() WHERE user_id = %s AND used_at IS NULL",
                (user["id"],),
            )
            execute(
                f"""
                INSERT INTO password_reset_tokens (user_id, token, expires_at)
                VALUES (%s, %s, NOW() + INTERVAL {OTP_VALID_MINUTES} MINUTE)
                """,
                (user["id"], otp_code),
            )

            # The code goes to the email address already on the account --
            # never back to whoever filled in this form, or knowing someone's
            # phone number would be enough to take over their account. (With
            # no SMTP configured in development, mailer.py logs it instead.)
            send_email(
                user["email"],
                "Your GoGather password reset code",
                f"Your password reset code is {otp_code}.\n\n"
                f"It expires in {OTP_VALID_MINUTES} minutes and can be used once. "
                "If you didn't ask for this, you can ignore this email -- your password hasn't changed.",
            )

        # Same response and same next page whether or not the account exists,
        # otherwise this form could be used to check who is registered.
        flash(
            f"If that account exists, we've emailed a 6-digit code to the address on it. "
            f"It expires in {OTP_VALID_MINUTES} minutes.",
            "success",
        )
        return redirect(url_for("verify_otp", phone=identifier))

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

        # A 6-digit code has only a million possibilities, so guesses are
        # capped per account and per connection.
        if rate_limited("otp-account", phone.lower(), 6, 900) or rate_limited("otp-ip", request.remote_addr, 20, 900):

            return render_template(
                "verify_otp.html", error="Too many attempts. Please wait a few minutes and request a new code.", phone=phone,
            ), 429

        user = find_user_for_reset(phone) if phone else None

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

        elif not is_strong_password(password):

            error = PASSWORD_REQUIREMENTS_MESSAGE

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
            return redirect_to_login()

    return render_template("verify_otp.html", error=error, phone=phone)


# TRIP REMINDERS
# A confirmed trip gets one reminder (in-app + email) when it's a few days
# away. `python manage.py send-reminders` runs this from cron / Task
# Scheduler; maybe_send_trip_reminders() also runs it at most once an hour
# when someone opens their dashboard, so reminders still go out on a server
# with no scheduler set up.
REMINDER_DAYS_AHEAD = 3
_last_reminder_run = 0


def send_trip_reminders(days_ahead=REMINDER_DAYS_AHEAD):
    due = query(
        """
        SELECT id, user_id, package_name, travel_date FROM bookings
        WHERE status = 'Confirmed' AND reminder_sent_at IS NULL
          AND travel_date >= CURDATE() AND travel_date <= CURDATE() + INTERVAL %s DAY
        """,
        (days_ahead,),
    )
    for booking in due:
        # Marked first, so a failure while notifying can never produce a
        # second reminder for the same trip on the next run.
        execute("UPDATE bookings SET reminder_sent_at = NOW() WHERE id = %s", (booking["id"],))
        days_left = (booking["travel_date"] - date.today()).days
        when = "today" if days_left == 0 else "tomorrow" if days_left == 1 else f"in {days_left} days"
        notify(
            booking["user_id"],
            f"Reminder: your trip '{booking['package_name']}' starts {when} "
            f"({booking['travel_date'].strftime('%d %b %Y')}). Have a great journey!",
            "/my_bookings",
            email_subject=f"Your GoGather trip starts {when}",
        )
    return len(due)


def maybe_send_trip_reminders():
    global _last_reminder_run
    if time.time() - _last_reminder_run < 3600:
        return
    _last_reminder_run = time.time()
    try:
        send_trip_reminders()
    except Exception:
        app.logger.exception("Trip reminder run failed")


# DASHBOARD GUIDANCE
def build_next_step(user_id):
    """One clear 'what should I do now?' for the traveller's dashboard, plus a
    4-step progress strip (Save -> Book -> Pay -> Travel). Everything is
    derived from what's already in the database -- nothing new to keep in sync."""
    totals = query(
        """
        SELECT COUNT(*) AS booked,
               COALESCE(SUM(payment_status = 'Paid'), 0) AS paid,
               COALESCE(SUM(status = 'Completed'), 0) AS completed
        FROM bookings WHERE user_id = %s AND status <> 'Cancelled'
        """,
        (user_id,), fetch="one",
    )
    saved_packages = query(
        "SELECT COUNT(*) AS total FROM wishlist_items WHERE user_id = %s", (user_id,), fetch="one",
    )["total"]
    saved_places = query(
        "SELECT COUNT(*) AS total FROM place_wishlist_items WHERE user_id = %s", (user_id,), fetch="one",
    )["total"]
    unpaid = query(
        """
        SELECT id, package_name FROM bookings
        WHERE user_id = %s AND status = 'Confirmed' AND payment_status = 'Unpaid' AND travel_date >= CURDATE()
        ORDER BY travel_date LIMIT 1
        """,
        (user_id,), fetch="one",
    )
    upcoming = query(
        """
        SELECT id, package_name, travel_date, status FROM bookings
        WHERE user_id = %s AND status IN ('Pending', 'Confirmed') AND travel_date >= CURDATE()
        ORDER BY travel_date LIMIT 1
        """,
        (user_id,), fetch="one",
    )

    steps = [
        ("Save a trip", saved_packages + saved_places > 0 or totals["booked"] > 0),
        ("Book", totals["booked"] > 0),
        ("Pay", totals["paid"] > 0),
        ("Travel", totals["completed"] > 0),
    ]

    if unpaid:
        title = f"Pay for '{unpaid['package_name']}'"
        text = "Your booking is confirmed -- complete the payment to secure it."
        url, button = url_for("my_bookings"), "Pay now"
    elif upcoming and upcoming["status"] == "Confirmed":
        days_left = (upcoming["travel_date"] - date.today()).days
        when = "today" if days_left == 0 else "tomorrow" if days_left == 1 else f"in {days_left} days"
        title = f"'{upcoming['package_name']}' starts {when}"
        text = f"Departing {upcoming['travel_date'].strftime('%d %b %Y')}. Check your ticket and trip details."
        url, button = url_for("my_bookings"), "View my booking"
    elif upcoming:
        title = f"'{upcoming['package_name']}' is waiting for confirmation"
        text = "An admin reviews every booking. You'll get a notification as soon as it's confirmed."
        url, button = url_for("my_bookings"), "View my booking"
    elif saved_packages:
        title = "Ready to book?"
        text = f"You've saved {saved_packages} package{'' if saved_packages == 1 else 's'}. Pick a travel date and lock one in."
        url, button = url_for("wishlist"), "See saved packages"
    elif saved_places:
        title = "Turn your saved places into a trip"
        text = f"You've saved {saved_places} destination{'' if saved_places == 1 else 's'}. See which packages visit them."
        url, button = url_for("wishlist"), "See saved places"
    elif totals["booked"]:
        title = "Where to next?"
        text = "Browse packages for your next adventure."
        url, button = url_for("packages"), "Browse packages"
    else:
        title = "Start by exploring Nepal"
        text = "Browse destinations, tap the heart on the ones you like, then book a package that visits them."
        url, button = url_for("explore_places"), "Explore places"

    return {"title": title, "text": text, "url": url, "button": button, "steps": steps}


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

    maybe_send_trip_reminders()

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

    return render_template(
        "dashboard.html",

        my_bookings_count=bookings_row["total"],

        my_groups_count=groups_row["total"],

        traveller_balance=traveller_balance,

        featured_packages=query(
            "SELECT id, name, duration_days, hotel_type, transport_mode, price "
            "FROM packages WHERE is_featured = 1 ORDER BY id"
        ),

        popular_destinations=get_places()[:4],

        next_step=build_next_step(session["user_id"]),

        username=session["username"]
    )


# TRAVELLER REPORTS

@app.route("/reports")
def traveller_reports():

    if "username" not in session:
        return redirect_to_login()

    if session.get("user") != 0:
        return redirect(url_for("admin_dashboard"))

    bookings = query(
        """
        SELECT package_name, price, travellers, status, payment_status, travel_date, created_at
        FROM bookings
        WHERE user_id = %s
        ORDER BY created_at
        """,
        (session["user_id"],),
    )

    today = date.today()
    upcoming_count = sum(1 for b in bookings if b["travel_date"] >= today)
    past_count = len(bookings) - upcoming_count
    paid_value = sum(b["price"] or 0 for b in bookings if b["payment_status"] == "Paid")
    paid_count = sum(1 for b in bookings if b["payment_status"] == "Paid")

    packages = {}
    months = {}
    for booking in bookings:
        name = booking["package_name"] or "Unnamed package"
        package = packages.setdefault(name, {"name": name, "bookings": 0, "paid": 0})
        package["bookings"] += 1
        if booking["payment_status"] == "Paid":
            package["paid"] += booking["price"] or 0
        month = booking["created_at"].strftime("%Y-%m")
        months[month] = months.get(month, 0) + 1

    # Show a continuous timeline, including months without bookings.
    first_month = (bookings[0]["created_at"].date() if bookings else today).replace(day=1)
    timeline = []
    month = first_month
    while month <= today:
        timeline.append({"label": month.strftime("%b %Y"), "count": months.get(month.strftime("%Y-%m"), 0)})
        month = (month.replace(day=28) + timedelta(days=4)).replace(day=1)

    groups_row = query(
        "SELECT COUNT(*) AS total FROM group_members WHERE user_id = %s",
        (session["user_id"],),
        fetch="one",
    )

    return render_template(
        "traveller_reports.html",
        total=len(bookings),
        upcoming_count=upcoming_count,
        past_count=past_count,
        paid_value=paid_value,
        paid_count=paid_count,
        groups_count=groups_row["total"],
        timeline=timeline,
        top_packages=sorted(packages.values(), key=lambda p: (-p["bookings"], p["name"]))[:5],
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

    auto_complete_past_bookings()
    maybe_send_trip_reminders()

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

    # Gross value of bookings that are paid and not refunded -- this is
    # money that moved through GoGather, not GoGather's own revenue: most
    # of each price is owed to the supplier that package was booked with.
    gross_booking_value = query(
        "SELECT COALESCE(SUM(price), 0) AS total FROM bookings WHERE payment_status = 'Paid'", fetch="one"
    )["total"]

    # GoGather's actual margin on those bookings -- what the traveller paid
    # minus what's owed to the supplier (packages.cost_price, frozen onto
    # the booking at booking time so a later cost change doesn't rewrite
    # the margin on a trip that already happened). This already includes
    # the service fee below, since that fee is folded into `price` -- it's
    # queried separately only so the dashboard can show it as its own line.
    booking_margin = query(
        "SELECT COALESCE(SUM(price - cost_price), 0) AS total FROM bookings WHERE payment_status = 'Paid'", fetch="one"
    )["total"]

    total_service_fees = query(
        "SELECT COALESCE(SUM(service_fee), 0) AS total FROM bookings WHERE payment_status = 'Paid'", fetch="one"
    )["total"]

    # Profit kept from the 20% cancellation fee policy -- only counted once
    # a refund actually reaches 'Refunded', since a Requested/Approved/
    # Refund Pending request hasn't had money move yet. This has to be
    # added separately rather than folded into the query above: once a
    # booking is refunded, its payment_status flips away from 'Paid', so
    # its margin drops out of booking_margin entirely -- the 20% GoGather
    # actually kept from that booking would otherwise vanish from the
    # total instead of being counted.
    total_cancellation_fees = query(
        "SELECT COALESCE(SUM(cancellation_fee), 0) AS total FROM refund_requests WHERE status = 'Refunded'", fetch="one"
    )["total"]

    # Real profit: margin kept on paid bookings plus fees kept on cancelled
    # ones -- not the full price travellers paid, most of which passes
    # through to the supplier.
    total_revenue = booking_margin + total_cancellation_fees
    margin_percent = round(100 * booking_margin / gross_booking_value) if gross_booking_value else 0

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

        total_revenue=total_revenue,

        gross_booking_value=gross_booking_value,

        margin_percent=margin_percent,

        total_service_fees=total_service_fees,

        total_cancellation_fees=total_cancellation_fees,

        pending_bookings=pending_bookings,

        cancel_requests=cancel_requests,

        pending_groups=pending_groups,

        pending_group_members=pending_group_members,

        recent_travellers=recent_travellers,

        traveller_performance=traveller_performance
    )


PLACE_CATEGORIES = ("Cultural", "Adventure", "Wildlife", "Trekking", "Nature")
PLACE_BUDGETS = ("Budget", "Moderate", "Premium")

STATIC_IMAGE_EXTENSIONS = (".jpg", ".jpeg", ".png", ".webp")


def list_static_images():
    """Photo pickers (destination cover photos, package/place gallery photos)
    all choose from images already sitting in static/ rather than accepting
    uploads, so there's one place-name whitelist to validate against."""
    return sorted(path.name for path in Path(app.static_folder).iterdir()
                  if path.is_file() and path.suffix.lower() in STATIC_IMAGE_EXTENSIONS)


@app.route("/admin/places", methods=["GET", "POST"])
def manage_places():
    if "username" not in session:
        return redirect_to_login()
    if session.get("user") != 1:
        return redirect(url_for("dashboard"))

    all_places = get_places()
    images = list_static_images()
    selected = request.args.get("edit")
    editing = next((place for place in all_places if place["slug"] == selected), None)
    if selected and editing is None:
        abort(404)
    form = dict(editing) if editing else {}
    error = None
    if request.method == "POST":
        action = request.form.get("action")
        slug = request.form.get("slug", "")
        existing = next((place for place in all_places if place["slug"] == slug), None)
        if action not in ("add", "edit", "delete"):
            abort(400)
        if action in ("edit", "delete") and existing is None:
            abort(404)
        if action == "delete":
            remove_place(slug)
            log_admin_activity("place.deleted", "place", slug, existing["name"])
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
                save_place(form, creating=action == "add")
            except IntegrityError:
                error = "A destination with this name already exists. Please choose another name."
            else:
                log_admin_activity(f"place.{'created' if action == 'add' else 'updated'}", "place", form["slug"], form["name"])
                flash(f'{form["name"]} has been {"updated" if editing else "added"}.', "success")
                return redirect(url_for("manage_places"))

    search = request.args.get("search", "").strip()
    places = [
        place for place in all_places
        if search.casefold() in " ".join(
            (place["name"], place["region"], place["category"])
        ).casefold()
    ]

    page = max(request.args.get("page", 1, type=int) or 1, 1)
    total_pages = max((len(places) + PAGE_SIZE - 1) // PAGE_SIZE, 1)
    page = min(page, total_pages)
    places_page = places[(page - 1) * PAGE_SIZE: page * PAGE_SIZE]

    all_photos = query("SELECT id, place_id, image, caption FROM place_photos ORDER BY sort_order, id")
    photos_by_place = {}
    for photo in all_photos:
        photos_by_place.setdefault(photo["place_id"], []).append(photo)
    for place in places_page:
        place["photos"] = photos_by_place.get(place["id"], [])
    return render_template("manage_places.html", places=places_page, search=search,
                           total=len(all_places), regions=len({p["region"] for p in all_places}),
                           categories=PLACE_CATEGORIES, budgets=PLACE_BUDGETS, images=images,
                           form=form, editing=editing, error=error, page=page, total_pages=total_pages,
                           show_form=bool(error or editing or request.args.get("add"))), (400 if error else 200)


@app.route("/admin/places/<slug>/photos/add", methods=["POST"])
def add_place_photo(slug):

    if "username" not in session or session.get("user") != 1:
        return redirect_to_login()

    place = query("SELECT id, name FROM explore_places WHERE slug = %s", (slug,), fetch="one")
    image = request.form.get("image", "")
    caption = request.form.get("caption", "").strip()[:200]

    if place is None:
        flash("Destination not found.", "error")
    else:
        photo_count = query(
            "SELECT COUNT(*) AS total FROM place_photos WHERE place_id = %s", (place["id"],), fetch="one"
        )["total"]
        if image not in list_static_images():
            flash("Please choose a photo from the available images.", "error")
        elif photo_count >= MAX_PHOTOS_PER_ITEM:
            flash(f"A destination can have at most {MAX_PHOTOS_PER_ITEM} photos.", "error")
        else:
            execute(
                "INSERT INTO place_photos (place_id, image, caption, sort_order) VALUES (%s, %s, %s, %s)",
                (place["id"], image, caption or None, photo_count),
            )
            log_admin_activity("place.photo_added", "place", slug, place["name"], new_value=image)
            flash("Photo added to the gallery.", "success")

    return redirect(url_for("manage_places"))


@app.route("/admin/places/<slug>/photos/<int:photo_id>/delete", methods=["POST"])
def delete_place_photo(slug, photo_id):

    if "username" not in session or session.get("user") != 1:
        return redirect_to_login()

    place = query("SELECT id, name FROM explore_places WHERE slug = %s", (slug,), fetch="one")
    if place is None:
        flash("Destination not found.", "error")
    else:
        execute("DELETE FROM place_photos WHERE id = %s AND place_id = %s", (photo_id, place["id"]))
        log_admin_activity("place.photo_removed", "place", slug, place["name"])
        flash("Photo removed.", "success")

    return redirect(url_for("manage_places"))


# TRAVEL PACKAGES - TRAVELLER

def apply_package_details(package):
    """Parse the JSON itinerary columns and fall back to sane defaults for
    packages an admin hasn't filled the rich details in for yet."""
    package["tag"] = package.get("tag") or "Nepal experience"
    package["summary"] = package.get("summary") or "A thoughtfully planned Nepal trip."
    places_covered = package.pop("places_covered", None)
    for field, raw in (("places", places_covered), ("activities", package.get("activities")),
                       ("includes", package.get("includes"))):
        if isinstance(raw, (str, bytes, bytearray)):
            try:
                package[field] = json.loads(raw)
            except ValueError:
                package[field] = []
        else:
            package[field] = raw or []


# Package list filters. Duration buckets are (min_days, max_days); None = no upper limit.
PACKAGE_DURATION_FILTERS = {"1-3": (1, 3, "1-3 days"), "4-7": (4, 7, "4-7 days"), "8+": (8, None, "8+ days")}
PACKAGE_SORTS = {
    "price_asc": ("Price: low to high", "price ASC"),
    "price_desc": ("Price: high to low", "price DESC"),
    "duration_asc": ("Shortest trip first", "duration_days ASC, price ASC"),
    "duration_desc": ("Longest trip first", "duration_days DESC, price ASC"),
    "rating": ("Top rated", "price ASC"),  # re-sorted by rating below
}


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
    # type=float/int quietly gives None for junk like ?min_price=abc, so a
    # mistyped URL just ignores the filter instead of raising an error.
    min_price = request.args.get("min_price", type=float)
    max_price = request.args.get("max_price", type=float)
    duration = request.args.get("duration", "")
    sort = request.args.get("sort", "price_asc")
    if sort not in PACKAGE_SORTS:
        sort = "price_asc"

    sql = ("SELECT id, name, duration_days, hotel_type, transport_mode, price, "
           "tag, summary, places_covered, activities, includes, max_seats FROM packages WHERE 1=1")
    params = []
    if search:
        # Matches on the package name or on any place it covers, so a link
        # from a destination's own page (Explore Places) finds every
        # package that visits it, not just one whose name happens to
        # contain that place's name.
        sql += " AND (name LIKE %s OR places_covered LIKE %s)"
        params.extend([f"%{search}%", f"%{search}%"])
    if hotel_type:
        sql += " AND hotel_type = %s"
        params.append(hotel_type)
    if transport_mode:
        sql += " AND transport_mode = %s"
        params.append(transport_mode)
    if min_price is not None:
        sql += " AND price >= %s"
        params.append(min_price)
    if max_price is not None:
        sql += " AND price <= %s"
        params.append(max_price)
    if duration in PACKAGE_DURATION_FILTERS:
        min_days, max_days, _label = PACKAGE_DURATION_FILTERS[duration]
        sql += " AND duration_days >= %s"
        params.append(min_days)
        if max_days is not None:
            sql += " AND duration_days <= %s"
            params.append(max_days)
    sql += " ORDER BY " + PACKAGE_SORTS[sort][1]  # from the fixed dict above, never from user input

    package_rows = query(sql, tuple(params))
    rating_summary = get_package_rating_summary()
    for package in package_rows:
        apply_package_details(package)
        summary = rating_summary.get(package["id"])
        package["rating_avg"] = round(float(summary["avg_rating"]), 1) if summary else None
        package["review_count"] = summary["review_count"] if summary else 0

    if sort == "rating":
        package_rows.sort(key=lambda p: (p["rating_avg"] or 0, p["review_count"]), reverse=True)

    return render_template(
        "packages.html", packages=package_rows, search=search,
        selected_hotel=hotel_type, selected_transport=transport_mode,
        min_price=min_price, max_price=max_price, selected_duration=duration, selected_sort=sort,
        duration_filters=PACKAGE_DURATION_FILTERS, sort_options=PACKAGE_SORTS,
        wishlisted_ids=get_wishlisted_package_ids(session["user_id"]),
    )


def get_itinerary(day_table, activity_table, parent_column, parent_id):
    """Builds [{day_number, title, activities: [{time, activity}, ...]}, ...]
    from the two-level itinerary tables, for either a package or a place."""
    rows = query(
        f"""
        SELECT d.day_number, d.title, a.activity_time, a.activity
        FROM {day_table} d
        JOIN {activity_table} a ON a.day_id = d.id
        WHERE d.{parent_column} = %s
        ORDER BY d.day_number, a.sort_order
        """,
        (parent_id,),
    )
    days = []
    for row in rows:
        if not days or days[-1]["day_number"] != row["day_number"]:
            days.append({"day_number": row["day_number"], "title": row["title"], "activities": []})
        days[-1]["activities"].append({"time": row["activity_time"], "activity": row["activity"]})
    return days


def get_editable_itinerary(day_table, activity_table, parent_column, parent_id):
    """Like get_itinerary(), but keeps each day's and activity's row id so
    the admin editor can target a specific one to delete."""
    days = query(
        f"SELECT id, day_number, title FROM {day_table} WHERE {parent_column} = %s ORDER BY day_number",
        (parent_id,),
    )
    for day in days:
        day["activities"] = query(
            f"SELECT id, activity_time, activity FROM {activity_table} WHERE day_id = %s ORDER BY sort_order, id",
            (day["id"],),
        )
    return days


def get_itinerary_extras(table, parent_column, parent_id):
    """The packing list / things-to-know pair for a package or place --
    stored as JSON columns, so parsed the same defensive way as the JSON
    columns on `packages` (apply_package_details)."""
    row = query(f"SELECT packing_list, things_to_know FROM {table} WHERE {parent_column} = %s", (parent_id,), fetch="one")
    if row is None:
        return {"packing_list": [], "things_to_know": []}
    extras = {}
    for field in ("packing_list", "things_to_know"):
        raw = row[field]
        if isinstance(raw, (str, bytes, bytearray)):
            try:
                extras[field] = json.loads(raw)
            except ValueError:
                extras[field] = []
        else:
            extras[field] = raw or []
    return extras


def get_package_departure_info(package_id):
    """Departure time/point and optional, additional-cost activities for a
    package's full itinerary page -- packages only, since places don't have
    a fixed departure or a bookable price to add optional extras onto."""
    row = query(
        "SELECT departure_time, departure_point, extra_expenses FROM package_itinerary_extras WHERE package_id = %s",
        (package_id,), fetch="one",
    )
    if row is None:
        return {"departure_time": None, "departure_point": None, "extra_expenses": []}
    extra_expenses = row["extra_expenses"]
    if isinstance(extra_expenses, (str, bytes, bytearray)):
        try:
            extra_expenses = json.loads(extra_expenses)
        except ValueError:
            extra_expenses = []
    return {
        "departure_time": row["departure_time"],
        "departure_point": row["departure_point"],
        "extra_expenses": extra_expenses or [],
    }


@app.route("/packages/<int:package_id>")
def package_detail(package_id):
    if "username" not in session:
        return redirect_to_login()
    if session.get("user") != 0:
        return redirect(url_for("admin_dashboard"))

    package = query(
        "SELECT id, name, duration_days, hotel_type, transport_mode, price, "
        "tag, summary, places_covered, activities, includes, max_seats FROM packages WHERE id = %s",
        (package_id,), fetch="one",
    )
    if package is None:
        return redirect(url_for("packages"))
    apply_package_details(package)
    photos = query(
        "SELECT id, image, caption FROM package_photos WHERE package_id = %s ORDER BY sort_order, id",
        (package_id,),
    )
    reviews = query(
        """
        SELECT r.rating, r.comment, r.created_at, u.full_name
        FROM package_reviews r JOIN users u ON u.id = r.user_id
        WHERE r.package_id = %s ORDER BY r.created_at DESC
        """,
        (package_id,),
    )
    rating_avg = round(sum(r["rating"] for r in reviews) / len(reviews), 1) if reviews else None
    itinerary = get_itinerary("package_itinerary_days", "package_itinerary_activities", "package_id", package_id)
    extras = get_itinerary_extras("package_itinerary_extras", "package_id", package_id)
    departure = get_package_departure_info(package_id)
    return render_template(
        "package_detail.html", package=package, photos=photos,
        reviews=reviews, rating_avg=rating_avg,
        itinerary=itinerary, packing_list=extras["packing_list"], things_to_know=extras["things_to_know"],
        departure_time=departure["departure_time"], departure_point=departure["departure_point"],
        extra_expenses=departure["extra_expenses"],
        wishlisted_ids=get_wishlisted_package_ids(session["user_id"]),
    )


@app.route("/packages/<int:package_id>/itinerary")
def package_itinerary_page(package_id):
    if "username" not in session:
        return redirect_to_login()
    if session.get("user") != 0:
        return redirect(url_for("admin_dashboard"))

    package = query(
        "SELECT id, name, duration_days, hotel_type, transport_mode, price, tag, summary "
        "FROM packages WHERE id = %s",
        (package_id,), fetch="one",
    )
    if package is None:
        return redirect(url_for("packages"))
    apply_package_details(package)
    itinerary = get_itinerary("package_itinerary_days", "package_itinerary_activities", "package_id", package_id)
    extras = get_itinerary_extras("package_itinerary_extras", "package_id", package_id)
    departure = get_package_departure_info(package_id)
    return render_template(
        "package_itinerary.html", package=package, itinerary=itinerary,
        packing_list=extras["packing_list"], things_to_know=extras["things_to_know"],
        departure_time=departure["departure_time"], departure_point=departure["departure_point"],
        extra_expenses=departure["extra_expenses"],
    )


# WISHLIST
#
# Lets a traveller save a package to look at again later without booking it
# on the spot. Saving is a toggle: the unique (user_id, package_id) key on
# wishlist_items means "is this already saved?" and "save/un-save" are the
# same lookup.

def get_wishlisted_package_ids(user_id):
    rows = query("SELECT package_id FROM wishlist_items WHERE user_id = %s", (user_id,))
    return {row["package_id"] for row in rows}


def get_wishlisted_place_ids(user_id):
    rows = query("SELECT place_id FROM place_wishlist_items WHERE user_id = %s", (user_id,))
    return {row["place_id"] for row in rows}


@app.route("/wishlist")
def wishlist():
    if "username" not in session:
        return redirect_to_login()
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

    place_rows = query(
        """
        SELECT p.id, p.slug, p.name, p.region, p.category, p.budget, p.days, p.best_time, p.image
        FROM place_wishlist_items w
        JOIN explore_places p ON p.id = w.place_id
        WHERE w.user_id = %s
        ORDER BY w.created_at DESC
        """,
        (session["user_id"],),
    )

    return render_template("wishlist.html", packages=package_rows, places=place_rows)


def wishlist_count(user_id):
    return query(
        """
        SELECT (SELECT COUNT(*) FROM wishlist_items WHERE user_id = %s)
             + (SELECT COUNT(*) FROM place_wishlist_items WHERE user_id = %s) AS total
        """,
        (user_id, user_id), fetch="one",
    )["total"]


def wishlist_response(saved, message, next_url):
    """The Save heart is submitted by JavaScript (static/ui.js) so the page
    doesn't reload: it asks for JSON and updates the button in place. Without
    JavaScript the same form still works as a normal post + redirect."""
    if request.headers.get("X-Requested-With") == "fetch":
        return jsonify(saved=saved, message=message, count=wishlist_count(session["user_id"]))
    flash(message, "success")
    return redirect(next_url)


@app.route("/wishlist/<int:package_id>/toggle", methods=["POST"])
def toggle_wishlist(package_id):
    if "username" not in session:
        return redirect_to_login()
    if session.get("user") != 0:
        return redirect(url_for("admin_dashboard"))

    existing = query(
        "SELECT id FROM wishlist_items WHERE user_id = %s AND package_id = %s",
        (session["user_id"], package_id), fetch="one",
    )

    if existing:
        execute("DELETE FROM wishlist_items WHERE id = %s", (existing["id"],))
        saved, message = False, "Removed from your wishlist."
    else:
        execute(
            "INSERT INTO wishlist_items (user_id, package_id) VALUES (%s, %s)",
            (session["user_id"], package_id),
        )
        saved, message = True, "Added to your wishlist."

    return wishlist_response(saved, message, safe_next(request.form.get("next"), url_for("packages")))


@app.route("/wishlist/place/<int:place_id>/toggle", methods=["POST"])
def toggle_place_wishlist(place_id):
    if "username" not in session:
        return redirect_to_login()
    if session.get("user") != 0:
        return redirect(url_for("admin_dashboard"))

    existing = query(
        "SELECT id FROM place_wishlist_items WHERE user_id = %s AND place_id = %s",
        (session["user_id"], place_id), fetch="one",
    )

    if existing:
        execute("DELETE FROM place_wishlist_items WHERE id = %s", (existing["id"],))
        saved, message = False, "Removed from your wishlist."
    else:
        execute(
            "INSERT INTO place_wishlist_items (user_id, place_id) VALUES (%s, %s)",
            (session["user_id"], place_id),
        )
        saved, message = True, "Added to your wishlist."

    return wishlist_response(saved, message, safe_next(request.form.get("next"), url_for("explore_places")))


# Places don't have a real numeric price or a review-based rating, so
# sorting only ever offers what the data actually supports: alphabetical,
# or the budget *tier* each place is tagged with (Budget/Moderate/Premium).
# There's deliberately no "Popular" or "Highest rated" option here -- this
# app has no popularity or review data to back either of those.
PLACE_BUDGET_RANK = {"Budget": 0, "Moderate": 1, "Premium": 2}

# A place's own name doesn't always appear in the packages that actually
# visit it (e.g. "Kathmandu Valley" the destination vs. "Kathmandu" as it's
# written inside a package's places_covered) -- these are the search terms
# that were checked to actually surface the right packages for "View
# packages" links, rather than the destination's full display name.
PLACE_PACKAGE_SEARCH_OVERRIDES = {
    "kathmandu-valley": "Kathmandu",
    "chitwan": "Chitwan",
    "everest-region": "Everest",
    "mustang": "Mustang",
}


def package_search_term(place):
    return PLACE_PACKAGE_SEARCH_OVERRIDES.get(place["slug"], place["name"])


@app.route("/explore_places")
def explore_places():

    if "username" not in session:
        return redirect_to_login()

    # Only traveller can access this page
    if session.get("user") != 0:
        return redirect(url_for("admin_dashboard"))

    search = request.args.get("search", "").strip().lower()
    category = request.args.get("category", "")
    budget = request.args.get("budget", "")
    region = request.args.get("region", "")
    sort = request.args.get("sort", "name")

    places = [
        place for place in get_places()
        if (not search or search in place["name"].lower()
            or search in place["region"].lower()
            or search in place["description"].lower()
            or any(search in activity.lower() for activity in place["activities"]))
        and (not category or place["category"] == category)
        and (not budget or place["budget"] == budget)
        and (not region or place["region"] == region)
    ]

    if sort == "budget_low":
        places.sort(key=lambda p: PLACE_BUDGET_RANK.get(p["budget"], 1))
    elif sort == "budget_high":
        places.sort(key=lambda p: PLACE_BUDGET_RANK.get(p["budget"], 1), reverse=True)
    else:
        places.sort(key=lambda p: p["name"])

    hidden_gems = [p for p in places if p["is_hidden_gem"]] if not (search or category or budget or region) else []

    for place in places:
        place["package_search_term"] = package_search_term(place)

    return render_template(
        "explore_places.html",
        places=places,
        hidden_gems=hidden_gems,
        search=search,
        selected_category=category,
        selected_budget=budget,
        selected_region=region,
        selected_sort=sort,
        wishlisted_place_ids=get_wishlisted_place_ids(session["user_id"]),
    )


@app.route("/explore_places/<slug>")
def place_detail(slug):
    if "username" not in session:
        return redirect_to_login()
    if session.get("user") != 0:
        return redirect(url_for("admin_dashboard"))

    all_places = get_places()
    place = next((item for item in all_places if item["slug"] == slug), None)
    if place is None:
        return redirect(url_for("explore_places"))

    similar_places = [
        item for item in all_places
        if item["category"] == place["category"] and item["slug"] != slug
    ][:3]
    photos = query(
        "SELECT id, image, caption FROM place_photos WHERE place_id = %s ORDER BY sort_order, id",
        (place["id"],),
    )
    place["package_search_term"] = package_search_term(place)
    # The step after "I like this place" is "how do I get there" -- so show
    # the packages that visit it right here, with a Book button, instead of
    # making the traveller go and search for them.
    place_packages = query(
        "SELECT id, name, duration_days, hotel_type, transport_mode, price "
        "FROM packages WHERE name LIKE %s OR places_covered LIKE %s ORDER BY price ASC LIMIT 3",
        (f"%{place['package_search_term']}%", f"%{place['package_search_term']}%"),
    )
    return render_template(
        "place_detail.html", place=place, similar_places=similar_places, photos=photos,
        place_packages=place_packages,
        wishlisted_place_ids=get_wishlisted_place_ids(session["user_id"]),
    )


@app.route("/explore_places/<slug>/itinerary")
def place_itinerary_page(slug):
    if "username" not in session:
        return redirect_to_login()
    if session.get("user") != 0:
        return redirect(url_for("admin_dashboard"))

    all_places = get_places()
    place = next((item for item in all_places if item["slug"] == slug), None)
    if place is None:
        return redirect(url_for("explore_places"))

    itinerary = get_itinerary("place_itinerary_days", "place_itinerary_activities", "place_id", place["id"])
    extras = get_itinerary_extras("place_itinerary_extras", "place_id", place["id"])
    return render_template(
        "place_itinerary.html", place=place, itinerary=itinerary,
        packing_list=extras["packing_list"], things_to_know=extras["things_to_know"],
    )


# BOOK PACKAGE


# Trips can be planned ahead, but not decades ahead -- two years covers any
# realistic advance booking.
MAX_BOOKING_DAYS_AHEAD = 180


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


def is_first_booking(user_id):
    """True if this traveller has never made a booking before (any status
    counts -- even a since-cancelled one still means this isn't their
    first), used to gate the one-time FIRST_BOOKING_DISCOUNT_PERCENT."""
    row = query("SELECT COUNT(*) AS total FROM bookings WHERE user_id = %s", (user_id,), fetch="one")
    return row["total"] == 0


def seats_booked_for_date(package_id, travel_date):
    """How many travellers already hold a seat on this package for this
    exact departure date. Cancelled bookings free their seats back up;
    everything else (Pending, Confirmed, Cancel Requested, Completed) still
    holds one, since the seat isn't actually free again until a booking is
    confirmed cancelled."""
    row = query(
        """
        SELECT COALESCE(SUM(travellers), 0) AS total FROM bookings
        WHERE package_id = %s AND travel_date = %s AND status != 'Cancelled'
        """,
        (package_id, travel_date), fetch="one",
    )
    return row["total"]


@app.route("/book_package", methods=["GET", "POST"])
def book_package():

    if "username" not in session:
        return redirect_to_login()

    if session.get("user") != 0:
        return redirect(url_for("admin_dashboard"))

    package_id = request.values.get("package_id", type=int)

    package = query(
        "SELECT id, name, duration_days, hotel_type, transport_mode, price, cost_price, max_seats "
        "FROM packages WHERE id = %s",
        (package_id,), fetch="one",
    ) if package_id else None

    if package is not None:
        # MySQL hands back DECIMAL columns as decimal.Decimal, which can't be
        # multiplied with the plain float SERVICE_FEE_PERCENT below (or in
        # the template) -- normalise to float right away like the rest of
        # the money handling in this file already does (see the cancellation
        # fee calculation) rather than mixing types further down.
        package["price"] = float(package["price"])
        package["cost_price"] = float(package["cost_price"] or 0)

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
        accept_policy = request.form.get("accept_policy") == "on"

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

        elif not accept_policy:

            flash("Please accept the Privacy Policy and Terms & Conditions to continue with your booking.", "error")

        elif has_overlapping_booking(session["user_id"], date.fromisoformat(travel_date), package["duration_days"]):

            # You can't be in two places at once, so a traveller's active
            # bookings (cancelled ones don't count) can't share any days.

            flash("You already have a trip booked over these dates. You can only book one package at a time for a given period.", "error")

        elif package["max_seats"] is not None and (
            seats_booked_for_date(package["id"], travel_date) + travellers > package["max_seats"]
        ):

            seats_left = max(package["max_seats"] - seats_booked_for_date(package["id"], travel_date), 0)
            flash(
                f"Only {seats_left} seat(s) left on '{package['name']}' for {travel_date}. "
                "Please choose fewer travellers or a different date.",
                "error",
            )

        else:

            base_price = package["price"] * travellers

            # A one-time welcome discount, only ever applied on the
            # traveller's very first booking -- checked right here, at the
            # moment of creating this booking, so it can never accidentally
            # apply twice.
            first_booking = is_first_booking(session["user_id"])
            discount_amount = round(base_price * FIRST_BOOKING_DISCOUNT_PERCENT / 100, 2) if first_booking else 0
            discounted_base = base_price - discount_amount
            service_fee = round(discounted_base * SERVICE_FEE_PERCENT / 100, 2)

            execute(
                """
                INSERT INTO bookings
                    (user_id, package_id, package_name, travel_date, travellers, phone, price, cost_price,
                     service_fee, discount_amount, status)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, 'Pending')
                """,
                (
                    session["user_id"],
                    package["id"],
                    package["name"],
                    travel_date,
                    travellers,
                    phone,
                    discounted_base + service_fee,
                    (package["cost_price"] or 0) * travellers,
                    service_fee,
                    discount_amount,
                ),
            )

            notify_admins(f"{session['username']} requested a booking for '{package['name']}'.", url_for("admin_bookings"))
            if first_booking:
                notify(
                    session["user_id"],
                    f"Your booking for '{package['name']}' is awaiting admin confirmation. "
                    f"Your first-booking discount of {format_npr(discount_amount)} has been applied!",
                    url_for("my_bookings"),
                )
                flash(
                    f"Your booking request has been submitted and is awaiting admin confirmation. "
                    f"As a welcome discount on your first booking, {FIRST_BOOKING_DISCOUNT_PERCENT:g}% "
                    f"({format_npr(discount_amount)}) was taken off the price.",
                    "success",
                )
            else:
                notify(session["user_id"], f"Your booking for '{package['name']}' is awaiting admin confirmation.", url_for("my_bookings"))
                flash("Your booking request has been submitted and is awaiting admin confirmation.", "success")

            return redirect(url_for("my_bookings"))

    return render_template(
        "book_package.html",
        package=package,
        packages=query(
            "SELECT id, name, duration_days, hotel_type, transport_mode, price, tag, summary, max_seats, "
            "(SELECT image FROM package_photos WHERE package_id = packages.id ORDER BY sort_order, id LIMIT 1) AS cover "
            "FROM packages ORDER BY price"
        ) if package is None else [],
        today=date.today().isoformat(),
        max_travel_date=max_travel_date,
        traveller_phone=traveller_phone,
        service_fee_percent=SERVICE_FEE_PERCENT,
        first_booking_discount_percent=FIRST_BOOKING_DISCOUNT_PERCENT,
        is_first_booking=is_first_booking(session["user_id"]),
    )


# MY BOOKINGS


@app.route("/my_bookings")
def my_bookings():

    if "username" not in session:
        return redirect_to_login()

    if session.get("user") != 0:
        return redirect(url_for("admin_dashboard"))

    auto_complete_past_bookings()

    bookings = query(
        """
        SELECT b.id, b.package_id, b.package_name, b.travel_date, b.travellers, b.phone, b.price, b.status,
               b.payment_status, b.created_at, b.discount_amount,
               r.status AS refund_status, r.reason AS refund_reason, r.amount AS refund_amount,
               r.cancellation_fee AS refund_fee,
               r.admin_notes AS refund_admin_notes, r.refund_reference AS refund_reference,
               r.updated_at AS refund_updated_at,
               pr.id AS review_id, pr.rating AS review_rating, pr.comment AS review_comment
        FROM bookings b
        LEFT JOIN refund_requests r ON r.id = (
            SELECT id FROM refund_requests WHERE booking_id = b.id ORDER BY id DESC LIMIT 1
        )
        LEFT JOIN package_reviews pr ON pr.booking_id = b.id
        WHERE b.user_id = %s
        ORDER BY b.created_at DESC
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
        cancellation_fee_percent=CANCELLATION_FEE_PERCENT,
    )


@app.route("/my_bookings/<int:booking_id>/cancel", methods=["POST"])
def cancel_booking(booking_id):

    if "user_id" not in session or session.get("user") != 0:
        return redirect_to_login()

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


# E-TICKET
# A one-page PDF confirmation for a paid, confirmed (or completed) booking --
# only available once payment has actually gone through, since a ticket for
# an unpaid booking wouldn't mean anything.

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


# REVIEWS & RATINGS
#
# A traveller can rate and comment on a package once their booking for it is
# marked Completed -- one review per booking (see package_reviews' unique
# key), and submitting again on the same booking edits that review instead
# of creating a second one.

def get_package_rating_summary():
    rows = query(
        "SELECT package_id, AVG(rating) AS avg_rating, COUNT(*) AS review_count "
        "FROM package_reviews GROUP BY package_id"
    )
    return {row["package_id"]: row for row in rows}


@app.route("/my_bookings/<int:booking_id>/review", methods=["POST"])
def submit_review(booking_id):

    if "user_id" not in session or session.get("user") != 0:
        return redirect_to_login()

    booking = query(
        "SELECT id, package_id, status FROM bookings WHERE id = %s AND user_id = %s",
        (booking_id, session["user_id"]), fetch="one",
    )

    try:
        rating = int(request.form.get("rating", "0"))
    except ValueError:
        rating = 0
    comment = request.form.get("comment", "").strip()[:1000]

    if booking is None or booking["package_id"] is None:
        flash("Booking not found.", "error")
    elif booking["status"] != "Completed":
        flash("You can only review a trip once it's marked completed.", "error")
    elif not 1 <= rating <= 5:
        flash("Please choose a rating between 1 and 5 stars.", "error")
    else:
        existing = query(
            "SELECT id FROM package_reviews WHERE booking_id = %s", (booking_id,), fetch="one"
        )
        if existing:
            execute(
                "UPDATE package_reviews SET rating = %s, comment = %s WHERE id = %s",
                (rating, comment or None, existing["id"]),
            )
            flash("Your review has been updated.", "success")
        else:
            execute(
                "INSERT INTO package_reviews (package_id, user_id, booking_id, rating, comment) "
                "VALUES (%s, %s, %s, %s, %s)",
                (booking["package_id"], session["user_id"], booking_id, rating, comment or None),
            )
            flash("Thanks for your review!", "success")

    return redirect(url_for("my_bookings"))


@app.route("/admin/reviews/<int:review_id>/delete", methods=["POST"])
def delete_review(review_id):

    if "username" not in session or session.get("user") != 1:
        return redirect_to_login()

    review = query("SELECT id, package_id FROM package_reviews WHERE id = %s", (review_id,), fetch="one")
    if review is None:
        flash("Review not found.", "error")
    else:
        execute("DELETE FROM package_reviews WHERE id = %s", (review_id,))
        log_admin_activity("review.removed", "package", review["package_id"], notes=f"Removed review #{review_id}")
        flash("Review removed.", "success")

    return redirect(safe_next(request.form.get("next"), url_for("admin_packages")))


@app.route("/my_bookings/<int:booking_id>/ticket")
def booking_ticket(booking_id):

    if "username" not in session:
        return redirect_to_login()
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


# PAYMENTS (eSewa + Stripe)
# Payment is only offered once admin has confirmed a booking -- that's the
# existing fraud gate (see admin_bookings / update_booking_status above),
# and payment shouldn't be collectible on a request that hasn't been vetted.


def get_payable_booking(booking_id):
    booking = query(
        "SELECT id, user_id, package_name, price, status, payment_status, eula_accepted_at "
        "FROM bookings WHERE id = %s AND user_id = %s",
        (booking_id, session["user_id"]), fetch="one",
    )
    if not booking or booking["status"] != "Confirmed" or booking["payment_status"] == "Paid":
        return None
    return booking


@app.route("/pay/<int:booking_id>/agreement", methods=["GET", "POST"])
def payment_agreement(booking_id):
    """EULA gate: the traveller must accept it before being sent to eSewa/Stripe."""
    if "user_id" not in session:
        return redirect_to_login()

    booking = get_payable_booking(booking_id)
    if booking is None:
        abort(404)

    method = request.values.get("method", "")
    if method not in ("esewa", "stripe"):
        abort(404)

    if request.method == "POST":
        if request.form.get("accept_eula") != "on":
            flash("You must accept the End-User License Agreement to continue with payment.", "error")
        else:
            execute("UPDATE bookings SET eula_accepted_at = NOW() WHERE id = %s", (booking_id,))
            return redirect(url_for("pay_with_esewa" if method == "esewa" else "pay_with_stripe", booking_id=booking_id))

    return render_template(
        "eula.html",
        booking=booking,
        method=method,
        cancellation_fee_percent=CANCELLATION_FEE_PERCENT,
        service_fee_percent=SERVICE_FEE_PERCENT,
    )


@app.route("/pay/esewa/<int:booking_id>")
def pay_with_esewa(booking_id):
    if "user_id" not in session:
        return redirect_to_login()

    if not esewa_is_trusted():
        flash("eSewa payments are not available right now. Please use card payment or contact support.", "error")
        return redirect(url_for("my_bookings"))

    booking = get_payable_booking(booking_id)
    if booking is None:
        abort(404)
    if not booking["eula_accepted_at"]:
        return redirect(url_for("payment_agreement", booking_id=booking_id, method="esewa"))

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
        return redirect_to_login()

    if not esewa_is_trusted():
        flash("eSewa payments are not available right now.", "error")
        return redirect(url_for("my_bookings"))

    try:
        payload = json.loads(base64.b64decode(request.args.get("data", "")))
        if not isinstance(payload, dict):
            raise ValueError("unexpected payload")
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
    if not signed_fields or not hmac.compare_digest(
        esewa_sign(expected_message, ESEWA_SECRET_KEY), str(payload.get("signature", ""))
    ):
        flash("Payment could not be verified.", "error")
        return redirect(url_for("my_bookings"))

    try:
        status = requests.get(
            ESEWA_STATUS_URL,
            params={
                "product_code": payload.get("product_code"),
                "total_amount": payload.get("total_amount"),
                "transaction_uuid": payload.get("transaction_uuid"),
            },
            timeout=10,
        ).json()
    except (requests.RequestException, ValueError):
        app.logger.warning("eSewa status check failed", exc_info=True)
        flash("We couldn't reach eSewa to confirm your payment. If you were charged, please contact support.", "error")
        return redirect(url_for("my_bookings"))

    booking = query(
        "SELECT id, user_id, package_name, price FROM bookings WHERE transaction_uuid = %s",
        (payload.get("transaction_uuid"),), fetch="one",
    )

    # The amount eSewa says was paid must be what this booking costs, and it
    # must be our merchant account that took it -- a valid payment for some
    # other amount or product can't settle this booking.
    amount_matches = False
    if booking:
        try:
            paid = Decimal(str(payload.get("total_amount", "")).replace(",", ""))
            amount_matches = paid == Decimal(str(booking["price"]))
        except InvalidOperation:
            amount_matches = False
    if payload.get("product_code") != ESEWA_PRODUCT_CODE:
        amount_matches = False

    if status.get("status") == "COMPLETE" and booking and amount_matches:
        execute("UPDATE bookings SET payment_status = 'Paid' WHERE id = %s", (booking["id"],))
        notify(
            booking["user_id"], f"Payment received for booking #{booking['id']} ({booking['package_name']}).",
            url_for("my_bookings"), email_subject=f"Payment received for booking #{booking['id']}",
        )
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
        return redirect_to_login()

    booking = get_payable_booking(booking_id)
    if booking is None:
        abort(404)
    if not booking["eula_accepted_at"]:
        return redirect(url_for("payment_agreement", booking_id=booking_id, method="stripe"))

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
    # With no webhook secret configured, a signature can be computed by
    # anyone (HMAC with an empty key), so nothing would be authenticated.
    # Refuse every call until the secret is set.
    if not STRIPE_WEBHOOK_SECRET:
        app.logger.error("Stripe webhook called but STRIPE_WEBHOOK_SECRET is not set; rejecting")
        return "", 503

    payload = request.get_data()
    sig_header = request.headers.get("Stripe-Signature", "")

    try:
        event = stripe.Webhook.construct_event(payload, sig_header, STRIPE_WEBHOOK_SECRET)
    except (ValueError, stripe.error.SignatureVerificationError):
        return "", 400

    if event["type"] == "checkout.session.completed":
        session_obj = event["data"]["object"]
        booking_id = (session_obj.get("metadata") or {}).get("booking_id")
        # Only a session Stripe actually collected money for settles a booking.
        if not booking_id or session_obj.get("payment_status") != "paid":
            return "", 200
        booking = query(
            "SELECT id, user_id, package_name FROM bookings WHERE id = %s AND stripe_session_id = %s",
            (booking_id, session_obj["id"]), fetch="one",
        )
        if booking:
            execute("UPDATE bookings SET payment_status = 'Paid' WHERE id = %s", (booking["id"],))
            notify(
                booking["user_id"], f"Payment received for booking #{booking['id']} ({booking['package_name']}).",
                url_for("my_bookings"), email_subject=f"Payment received for booking #{booking['id']}",
            )

    return "", 200


# TRAVEL GROUPS
MAX_GROUP_DAYS_AHEAD = 180
@app.route("/travel_groups")
def travel_groups():

    if "username" not in session:
        return redirect_to_login()

    if session.get("user") != 0:
        return redirect(url_for("admin_dashboard"))

    destination = request.args.get("destination", "").strip()
    sql = """
        SELECT tg.id, tg.name, tg.destination, tg.travel_date, tg.max_members,
               tg.budget_range, tg.description, tg.status, tg.communication_mode,
               u.username AS created_by,
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

    # A traveller is already "in" a group if they've joined it, requested to
    # join it, or created it (the creator is auto-joined on creation). Those
    # belong on the "My groups" tab; everything else is browsable/available.
    my_groups = [g for g in groups if g["has_joined"] or g["has_requested"]]
    available_groups = [g for g in groups if not g["has_joined"] and not g["has_requested"]]

    max_travel_date = (date.today() + timedelta(days=MAX_GROUP_DAYS_AHEAD)).isoformat()
    return render_template(
        "travel_groups.html",
        available_groups=available_groups,
        my_groups=my_groups,
        destination=destination,
        today=date.today().isoformat(),
        max_travel_date=max_travel_date,
    )



@app.route("/travel_groups/create", methods=["POST"])
def create_travel_group():
    if "user_id" not in session or session.get("user") != 0:
        return redirect_to_login()

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
        return redirect_to_login()

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


# GROUP CHAT
# communication_mode: 2 = two-way (any confirmed member can post),
# 1 = one-way (only the group creator can post), 0 = blocked (no one can).

def get_chat_list(user_id):
    """Every group the user can chat in (creator or confirmed member), newest
    activity first, each with a preview of its latest message."""
    chats = query(
        """
        SELECT g.id, g.name, g.destination,
               (SELECT m.message FROM group_messages m WHERE m.group_id = g.id ORDER BY m.id DESC LIMIT 1) AS last_message,
               (SELECT m.created_at FROM group_messages m WHERE m.group_id = g.id ORDER BY m.id DESC LIMIT 1) AS last_at,
               (SELECT u.full_name FROM group_messages m JOIN users u ON u.id = m.user_id
                 WHERE m.group_id = g.id ORDER BY m.id DESC LIMIT 1) AS last_sender
        FROM travel_groups g
        WHERE g.created_by = %s
           OR g.id IN (SELECT group_id FROM group_members WHERE user_id = %s AND status = 'Confirmed')
        ORDER BY last_at IS NULL, last_at DESC, g.name ASC
        """,
        (user_id, user_id),
    )
    today = date.today()
    for chat in chats:
        last_at = chat["last_at"]
        if last_at is None:
            chat["last_label"] = ""
        elif last_at.date() == today:
            chat["last_label"] = last_at.strftime("%I:%M %p").lstrip("0")
        elif (today - last_at.date()).days == 1:
            chat["last_label"] = "Yesterday"
        else:
            chat["last_label"] = last_at.strftime("%d/%m/%Y")
    return chats


@app.route("/chats")
def chats():
    if "user_id" not in session or session.get("user") != 0:
        return redirect_to_login()
    return render_template("group_chat.html", group=None, chat_list=get_chat_list(session["user_id"]))


@app.route("/travel_groups/<int:group_id>/chat", methods=["GET", "POST"])
def group_chat(group_id):
    if "user_id" not in session or session.get("user") != 0:
        return redirect_to_login()

    group = query(
        "SELECT id, name, destination, description, created_by, communication_mode FROM travel_groups WHERE id = %s",
        (group_id,), fetch="one",
    )
    if group is None:
        flash("That travel group is no longer available.", "error")
        return redirect(url_for("travel_groups"))

    is_member = query(
        "SELECT id FROM group_members WHERE group_id = %s AND user_id = %s AND status = 'Confirmed'",
        (group_id, session["user_id"]), fetch="one",
    ) is not None
    is_creator = group["created_by"] == session["user_id"]

    if not (is_member or is_creator):
        flash("You need to be a confirmed member of this group to see its chat.", "error")
        return redirect(url_for("travel_groups"))

    can_post = (
        group["communication_mode"] == 2 and is_member
        or group["communication_mode"] == 1 and is_creator
    )

    if request.method == "POST":
        if not can_post:
            flash("You don't have permission to post in this group's chat.", "error")
        else:
            message = request.form.get("message", "").strip()
            if not message:
                flash("Please write a message before sending.", "error")
            else:
                execute(
                    "INSERT INTO group_messages (group_id, user_id, message) VALUES (%s, %s, %s)",
                    (group_id, session["user_id"], message[:1000]),
                )
        return redirect(url_for("group_chat", group_id=group_id))

    messages = query(
        """
        SELECT gmsg.id, gmsg.user_id, gmsg.message, gmsg.created_at, u.full_name, u.username
        FROM group_messages gmsg
        JOIN users u ON u.id = gmsg.user_id
        WHERE gmsg.group_id = %s
        ORDER BY gmsg.id ASC
        """,
        (group_id,),
    )

    members = query(
        """
        SELECT u.id, u.full_name, u.username
        FROM group_members gm
        JOIN users u ON u.id = gm.user_id
        WHERE gm.group_id = %s AND gm.status = 'Confirmed'
        ORDER BY (u.id = %s) DESC, u.full_name ASC
        """,
        (group_id, group["created_by"]),
    )

    return render_template(
        "group_chat.html",
        group=group,
        messages=messages,
        members=members,
        can_post=can_post,
        is_creator=is_creator,
        chat_list=get_chat_list(session["user_id"]),
    )


# Real-time-feeling delivery for the chat, done as polling rather than
# WebSockets: the browser asks this endpoint "anything after message #X?"
# every few seconds. Each call is a normal, synchronous HTTP request/
# response -- the app never holds a connection open or pushes data on its
# own. It only ever returns messages from a group the caller is actually
# in, same as the page itself.
@app.route("/travel_groups/<int:group_id>/chat/poll")
def group_chat_poll(group_id):
    if "user_id" not in session or session.get("user") != 0:
        return jsonify([]), 401

    group = query("SELECT created_by FROM travel_groups WHERE id = %s", (group_id,), fetch="one")
    if group is None:
        return jsonify([]), 404

    is_member = query(
        "SELECT id FROM group_members WHERE group_id = %s AND user_id = %s AND status = 'Confirmed'",
        (group_id, session["user_id"]), fetch="one",
    ) is not None
    is_creator = group["created_by"] == session["user_id"]

    if not (is_member or is_creator):
        return jsonify([]), 403

    after_id = request.args.get("after_id", 0, type=int)

    rows = query(
        """
        SELECT gmsg.id, gmsg.user_id, gmsg.message, gmsg.created_at, u.full_name
        FROM group_messages gmsg
        JOIN users u ON u.id = gmsg.user_id
        WHERE gmsg.group_id = %s AND gmsg.id > %s
        ORDER BY gmsg.id ASC
        """,
        (group_id, after_id),
    )

    return jsonify([
        {
            "id": row["id"],
            "is_own": row["user_id"] == session["user_id"],
            "user_id": row["user_id"],
            "full_name": row["full_name"],
            "message": row["message"],
            "time": row["created_at"].strftime("%I:%M %p"),
        }
        for row in rows
    ])


@app.route("/travel_groups/<int:group_id>/leave", methods=["POST"])
def leave_travel_group(group_id):
    if "user_id" not in session or session.get("user") != 0:
        return redirect_to_login()

    group = query("SELECT id, name, created_by FROM travel_groups WHERE id = %s", (group_id,), fetch="one")
    if group is None:
        flash("That travel group is no longer available.", "error")
        return redirect(url_for("travel_groups"))

    if group["created_by"] == session["user_id"]:
        flash("As the group organiser, you can't leave your own group.", "error")
        return redirect(url_for("group_chat", group_id=group_id))

    membership = query(
        "SELECT id FROM group_members WHERE group_id = %s AND user_id = %s AND status = 'Confirmed'",
        (group_id, session["user_id"]), fetch="one",
    )
    if membership is None:
        flash("You're not a member of this group.", "error")
        return redirect(url_for("travel_groups"))

    execute("DELETE FROM group_members WHERE id = %s", (membership["id"],))
    notify(group["created_by"], f"{session['username']} left the group '{group['name']}'.", url_for("group_chat", group_id=group_id))
    flash(f"You have left '{group['name']}'.", "success")
    return redirect(url_for("travel_groups"))


# MANAGE TRAVEL GROUPS - ADMIN

@app.route("/admin/travel_groups")
def admin_travel_groups():

    if "username" not in session:
        return redirect_to_login()

    if session.get("user") != 1:
        return redirect(url_for("dashboard"))

    groups = query(
        """
        SELECT tg.id, tg.name, tg.destination, tg.travel_date, tg.max_members,
               tg.budget_range, tg.description, tg.status, tg.created_at, tg.communication_mode,
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
        return redirect_to_login()

    status = request.form.get("status", "")

    if status not in ("Confirmed", "Rejected"):
        flash("Please choose a valid group status.", "error")
    else:
        group = query("SELECT created_by, name, status FROM travel_groups WHERE id = %s", (group_id,), fetch="one")
        execute("UPDATE travel_groups SET status = %s WHERE id = %s", (status, group_id))
        if group and group["status"] != status:
            notify(group["created_by"], f"Your travel group '{group['name']}' was {status.lower()}.", url_for("travel_groups"))
            log_admin_activity(
                "travel_group.status_updated", "travel_group", group_id, group["name"],
                old_value=group["status"], new_value=status,
            )
        flash("Travel group status updated.", "success")

    return redirect(url_for("admin_travel_groups"))


@app.route("/admin/travel_groups/<int:group_id>/update-communication", methods=["POST"])
def update_group_communication(group_id):

    if "username" not in session or session.get("user") != 1:
        return redirect_to_login()

    try:
        mode = int(request.form.get("communication_mode", ""))
    except ValueError:
        mode = None

    if mode not in (0, 1, 2):
        flash("Please choose a valid communication mode.", "error")
    else:
        group = query("SELECT created_by, name FROM travel_groups WHERE id = %s", (group_id,), fetch="one")
        execute("UPDATE travel_groups SET communication_mode = %s WHERE id = %s", (mode, group_id))
        if group:
            label = {2: "two-way", 1: "one-way", 0: "blocked"}[mode]
            notify(group["created_by"], f"Communication in '{group['name']}' was set to {label}.", url_for("travel_groups"))
            log_admin_activity("travel_group.communication_updated", "travel_group", group_id, group["name"], new_value=label)
        flash("Group communication mode updated.", "success")

    return redirect(url_for("admin_travel_groups"))


@app.route("/admin/group_members/<int:member_id>/update-status", methods=["POST"])
def update_group_member_status(member_id):

    if "username" not in session or session.get("user") != 1:
        return redirect_to_login()

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
            log_admin_activity(
                "group_member.status_updated", "group_member", member_id, member["group_name"],
                old_value=member["status"], new_value=status,
            )
        flash("Membership request updated.", "success")

    return redirect(url_for("admin_travel_groups"))


# PROFILE
AVATAR_EXTENSIONS = {"png", "jpg", "jpeg", "webp"}
MAX_AVATAR_SIZE = 3 * 1024 * 1024  # 3 MB


def avatar_extension(filename):
    ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
    return ext if ext in AVATAR_EXTENSIONS else None


def detect_image_type(data):
    """The real image format from the file's first bytes, or None. The
    filename is user-controlled, so only the content is trusted -- a renamed
    HTML/script file won't pass as a PNG."""
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "png"
    if data.startswith(b"\xff\xd8\xff"):
        return "jpg"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "webp"
    return None


def save_avatar(user_id, filename, data):
    """Saves each upload under a unique name (avatar_<user_id>_<timestamp>.<ext>)
    so previous avatars stay on the server instead of being deleted. The
    extension comes from the detected image type, not the uploaded filename."""
    ext = detect_image_type(data) or avatar_extension(filename)
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
    if "terms_accepted_at" not in columns:
        # A record that this account actually agreed to the Terms &
        # Conditions / Refund Policy at registration -- not just a UI
        # checkbox with nothing kept afterwards. Existing accounts predate
        # this requirement, so they're left NULL rather than backfilled
        # with a false "accepted" timestamp.
        execute("ALTER TABLE users ADD COLUMN terms_accepted_at TIMESTAMP NULL")


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
        return redirect_to_login()

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

        elif avatar_data is not None and detect_image_type(avatar_data) is None:

            flash("That file isn't a valid PNG, JPG or WEBP image.", "error")

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


@app.route("/profile/avatar/<int:history_id>/restore", methods=["POST"])
def restore_avatar(history_id):

    if "username" not in session:
        return redirect_to_login()

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


# MANAGE TRAVELLERS - ADMIN

@app.route("/profile/password", methods=["POST"])
def change_password():

    if "username" not in session:
        return redirect_to_login()

    if session.get("user") != 0:
        return redirect(url_for("admin_dashboard"))

    current_password = request.form.get("current_password", "")
    new_password = request.form.get("new_password", "")
    confirm_password = request.form.get("confirm_password", "")

    user = query("SELECT password_hash FROM users WHERE id = %s", (session["user_id"],), fetch="one")

    if rate_limited("change-password", str(session["user_id"]), 5, 900):

        flash("Too many attempts. Please wait a few minutes and try again.", "error")

    elif not user or not check_password_hash(user["password_hash"], current_password):

        flash("Your current password is incorrect.", "error")

    elif new_password != confirm_password:

        flash("New password and Confirm Password don't match.", "error")

    elif not is_strong_password(new_password):

        flash(PASSWORD_REQUIREMENTS_MESSAGE, "error")

    elif current_password == new_password:

        flash("Your new password must be different from your current one.", "error")

    else:

        execute(
            "UPDATE users SET password_hash = %s WHERE id = %s",
            (generate_password_hash(new_password), session["user_id"]),
        )
        notify(session["user_id"], "Your password was changed. If this wasn't you, reset it right away.", url_for("profile"))
        flash("Your password has been changed.", "success")

    return redirect(url_for("profile"))


@app.route("/admin/travellers", methods=["GET", "POST"])
def manage_travellers():

    # User must be logged in
    if "username" not in session:

        return redirect_to_login()

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

    page = max(request.args.get("page", 1, type=int) or 1, 1)
    total_travellers = query("SELECT COUNT(*) AS total FROM users WHERE role = 0", fetch="one")["total"]
    total_pages = max((total_travellers + PAGE_SIZE - 1) // PAGE_SIZE, 1)
    page = min(page, total_pages)

    travellers = query(
        """
        SELECT id, full_name, username, email, phone, created_at
        FROM users
        WHERE role = 0
        ORDER BY created_at DESC
        LIMIT %s OFFSET %s
        """,
        (PAGE_SIZE, (page - 1) * PAGE_SIZE),
    )

    return render_template(
        "manage_travellers.html", travellers=travellers, page=page, total_pages=total_pages
    )


@app.route("/admin/travellers/<int:user_id>/update", methods=["POST"])
def update_traveller(user_id):

    if "username" not in session or session.get("user") != 1:
        return redirect_to_login()

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
        return redirect_to_login()

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


def read_package_extras(form):
    """Pull the itinerary fields (comma-separated lists, tag, summary,
    featured flag) an admin fills in alongside the core package details."""
    def as_list(field):
        return [item.strip() for item in form.get(field, "").split(",") if item.strip()]

    max_seats_raw = form.get("max_seats", "").strip()
    try:
        max_seats = int(max_seats_raw) if max_seats_raw else None
    except ValueError:
        max_seats = None
    if max_seats is not None and max_seats < 1:
        max_seats = None

    return {
        "tag": form.get("tag", "").strip(),
        "summary": form.get("summary", "").strip(),
        "places_covered": json.dumps(as_list("places_covered")),
        "activities": json.dumps(as_list("activities")),
        "includes": json.dumps(as_list("includes")),
        "is_featured": 1 if form.get("is_featured") else 0,
        "max_seats": max_seats,
    }


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

            extras = read_package_extras(request.form)
            new_package_id = execute(
                """
                INSERT INTO packages
                    (name, duration_days, hotel_type, transport_mode, price, cost_price,
                     is_featured, tag, summary, places_covered, activities, includes, max_seats)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                """,
                (name, duration_days, hotel_type, transport_mode, price, cost_price_for(price),
                 extras["is_featured"], extras["tag"], extras["summary"],
                 extras["places_covered"], extras["activities"], extras["includes"], extras["max_seats"]),
            )
            log_admin_activity("package.created", "package", new_package_id, name)

            flash("Package added.", "success")

        return redirect(url_for("admin_packages"))

    page = max(request.args.get("page", 1, type=int) or 1, 1)
    total_packages = query("SELECT COUNT(*) AS total FROM packages", fetch="one")["total"]
    total_pages = max((total_packages + PAGE_SIZE - 1) // PAGE_SIZE, 1)
    page = min(page, total_pages)

    packages = query(
        "SELECT id, name, duration_days, hotel_type, transport_mode, price, cost_price, "
        "is_featured, tag, summary, places_covered, activities, includes, max_seats "
        "FROM packages ORDER BY price LIMIT %s OFFSET %s",
        (PAGE_SIZE, (page - 1) * PAGE_SIZE),
    )
    all_photos = query("SELECT id, package_id, image, caption FROM package_photos ORDER BY sort_order, id")
    photos_by_package = {}
    for photo in all_photos:
        photos_by_package.setdefault(photo["package_id"], []).append(photo)
    for package in packages:
        apply_package_details(package)
        package["photos"] = photos_by_package.get(package["id"], [])
        package["itinerary"] = get_editable_itinerary(
            "package_itinerary_days", "package_itinerary_activities", "package_id", package["id"]
        )
        package["itinerary_extras"] = get_itinerary_extras(
            "package_itinerary_extras", "package_id", package["id"]
        )
        package["itinerary_extras"].update(get_package_departure_info(package["id"]))
        package["reviews"] = query(
            """
            SELECT r.id, r.rating, r.comment, r.created_at, u.full_name
            FROM package_reviews r JOIN users u ON u.id = r.user_id
            WHERE r.package_id = %s ORDER BY r.created_at DESC
            """,
            (package["id"],),
        )

    return render_template(
        "admin_packages.html", packages=packages, images=list_static_images(),
        default_margin_percent=DEFAULT_MARGIN_PERCENT, page=page, total_pages=total_pages,
    )


@app.route("/admin/packages/<int:package_id>/update", methods=["POST"])
def update_package(package_id):

    if "username" not in session or session.get("user") != 1:
        return redirect_to_login()

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

        extras = read_package_extras(request.form)
        execute(
            """
            UPDATE packages
            SET name = %s, duration_days = %s, hotel_type = %s, transport_mode = %s, price = %s, cost_price = %s,
                is_featured = %s, tag = %s, summary = %s, places_covered = %s, activities = %s, includes = %s,
                max_seats = %s
            WHERE id = %s
            """,
            (name, duration_days, hotel_type, transport_mode, price, cost_price_for(price),
             extras["is_featured"], extras["tag"], extras["summary"],
             extras["places_covered"], extras["activities"], extras["includes"], extras["max_seats"], package_id),
        )
        log_admin_activity("package.updated", "package", package_id, name)

        flash("Package updated.", "success")

    return redirect(url_for("admin_packages"))


@app.route("/admin/packages/<int:package_id>/delete", methods=["POST"])
def delete_package(package_id):

    if "username" not in session or session.get("user") != 1:
        return redirect_to_login()

    # Bookings that referenced this package keep their own copy of the
    # package name and price, so they're unaffected (packages.id is set
    # to NULL on the booking rather than the booking being removed).

    package = query("SELECT name FROM packages WHERE id = %s", (package_id,), fetch="one")
    execute("DELETE FROM packages WHERE id = %s", (package_id,))
    if package:
        log_admin_activity("package.deleted", "package", package_id, package["name"])

    flash("Package removed.", "success")

    return redirect(url_for("admin_packages"))


MAX_PHOTOS_PER_ITEM = 12


@app.route("/admin/packages/<int:package_id>/photos/add", methods=["POST"])
def add_package_photo(package_id):

    if "username" not in session or session.get("user") != 1:
        return redirect_to_login()

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
        return redirect_to_login()

    package = query("SELECT name FROM packages WHERE id = %s", (package_id,), fetch="one")
    execute("DELETE FROM package_photos WHERE id = %s AND package_id = %s", (photo_id, package_id))
    if package:
        log_admin_activity("package.photo_removed", "package", package_id, package["name"])
    flash("Photo removed.", "success")

    return redirect(url_for("admin_packages"))


# ITINERARY EDITOR - ADMIN
# Lets an admin add/remove itinerary days and their timed activities from
# Manage Packages, instead of the itinerary only being editable by directly
# running SQL against package_itinerary_days/package_itinerary_activities.

@app.route("/admin/packages/<int:package_id>/itinerary/add-day", methods=["POST"])
def add_package_itinerary_day(package_id):
    if "username" not in session or session.get("user") != 1:
        return redirect_to_login()

    package = query("SELECT name, duration_days FROM packages WHERE id = %s", (package_id,), fetch="one")
    title = request.form.get("title", "").strip()[:150]
    try:
        day_number = int(request.form.get("day_number", "0"))
    except ValueError:
        day_number = 0

    if package is None:
        flash("Package not found.", "error")
    elif not title or not 1 <= day_number <= package["duration_days"]:
        flash(f"Day number must be between 1 and {package['duration_days'] if package else '?'}, with a title.", "error")
    else:
        try:
            execute(
                "INSERT INTO package_itinerary_days (package_id, day_number, title) VALUES (%s, %s, %s)",
                (package_id, day_number, title),
            )
            log_admin_activity("package.itinerary_day_added", "package", package_id, package["name"], new_value=f"Day {day_number}: {title}")
            flash(f"Day {day_number} added.", "success")
        except IntegrityError:
            flash(f"Day {day_number} already exists for this package.", "error")

    return redirect(url_for("admin_packages", _anchor=f"package-{package_id}"))


@app.route("/admin/packages/<int:package_id>/itinerary/<int:day_id>/delete", methods=["POST"])
def delete_package_itinerary_day(package_id, day_id):
    if "username" not in session or session.get("user") != 1:
        return redirect_to_login()

    execute("DELETE FROM package_itinerary_days WHERE id = %s AND package_id = %s", (day_id, package_id))
    flash("Itinerary day removed.", "success")

    return redirect(url_for("admin_packages", _anchor=f"package-{package_id}"))


@app.route("/admin/packages/<int:package_id>/itinerary/<int:day_id>/add-activity", methods=["POST"])
def add_package_itinerary_activity(package_id, day_id):
    if "username" not in session or session.get("user") != 1:
        return redirect_to_login()

    activity_time = request.form.get("activity_time", "").strip()[:10]
    activity = request.form.get("activity", "").strip()[:255]

    if not activity_time or not activity:
        flash("Please provide a time and an activity.", "error")
    else:
        next_order = query(
            "SELECT COALESCE(MAX(sort_order), -1) + 1 AS next_order FROM package_itinerary_activities WHERE day_id = %s",
            (day_id,), fetch="one",
        )["next_order"]
        execute(
            "INSERT INTO package_itinerary_activities (day_id, sort_order, activity_time, activity) VALUES (%s, %s, %s, %s)",
            (day_id, next_order, activity_time, activity),
        )
        flash("Activity added.", "success")

    return redirect(url_for("admin_packages", _anchor=f"package-{package_id}"))


@app.route("/admin/packages/<int:package_id>/itinerary/activity/<int:activity_id>/delete", methods=["POST"])
def delete_package_itinerary_activity(package_id, activity_id):
    if "username" not in session or session.get("user") != 1:
        return redirect_to_login()

    execute(
        """
        DELETE a FROM package_itinerary_activities a
        JOIN package_itinerary_days d ON d.id = a.day_id
        WHERE a.id = %s AND d.package_id = %s
        """,
        (activity_id, package_id),
    )
    flash("Activity removed.", "success")

    return redirect(url_for("admin_packages", _anchor=f"package-{package_id}"))


@app.route("/admin/packages/<int:package_id>/itinerary/extras", methods=["POST"])
def update_package_itinerary_extras(package_id):
    if "username" not in session or session.get("user") != 1:
        return redirect_to_login()

    packing_list = [item.strip() for item in request.form.get("packing_list", "").split(",") if item.strip()]
    things_to_know = [item.strip() for item in request.form.get("things_to_know", "").split(",") if item.strip()]
    extra_expenses = [item.strip() for item in request.form.get("extra_expenses", "").split(",") if item.strip()]
    departure_time = request.form.get("departure_time", "").strip()[:100] or None
    departure_point = request.form.get("departure_point", "").strip()[:200] or None

    execute(
        """
        INSERT INTO package_itinerary_extras
            (package_id, packing_list, things_to_know, departure_time, departure_point, extra_expenses)
        VALUES (%s, %s, %s, %s, %s, %s)
        ON DUPLICATE KEY UPDATE
            packing_list = VALUES(packing_list), things_to_know = VALUES(things_to_know),
            departure_time = VALUES(departure_time), departure_point = VALUES(departure_point),
            extra_expenses = VALUES(extra_expenses)
        """,
        (package_id, json.dumps(packing_list), json.dumps(things_to_know),
         departure_time, departure_point, json.dumps(extra_expenses)),
    )
    flash("Itinerary details updated.", "success")

    return redirect(url_for("admin_packages", _anchor=f"package-{package_id}"))


# MANAGE BOOKINGS - ADMIN

@app.route("/admin/bookings")
def admin_bookings():

    # User must be logged in
    if "username" not in session:

        return redirect_to_login()

    # Only Admin can access this page
    if session.get("user") != 1:

        return redirect(url_for("dashboard"))

    auto_complete_past_bookings()

    search = request.args.get("search", "").strip()
    status_filter = request.args.get("status", "")
    payment_filter = request.args.get("payment", "")
    page = max(request.args.get("page", 1, type=int) or 1, 1)

    where_sql = "WHERE 1=1"
    params = []

    if search:
        where_sql += " AND (u.full_name LIKE %s OR u.username LIKE %s OR b.package_name LIKE %s)"
        like = f"%{search}%"
        params.extend([like, like, like])

    # status_filter may carry more than one status (e.g. from a Reports stat
    # card linking to "Confirmed,Completed") -- the dropdown on this page
    # still only ever submits one.
    statuses = [s.strip() for s in status_filter.split(",") if s.strip()]
    if statuses:
        where_sql += " AND b.status IN (%s)" % ",".join(["%s"] * len(statuses))
        params.extend(statuses)

    if payment_filter:
        where_sql += " AND b.payment_status = %s"
        params.append(payment_filter)

    total_matching = query(
        f"SELECT COUNT(*) AS total FROM bookings b JOIN users u ON u.id = b.user_id {where_sql}",
        tuple(params), fetch="one",
    )["total"]
    total_pages = max((total_matching + PAGE_SIZE - 1) // PAGE_SIZE, 1)
    page = min(page, total_pages)

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
        {where_sql}
        ORDER BY b.created_at DESC
        LIMIT %s OFFSET %s
    """
    page_params = tuple(OPEN_REFUND_STATUSES) + tuple(params) + (PAGE_SIZE, (page - 1) * PAGE_SIZE)

    bookings = query(sql, page_params)

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
        selected_payment=payment_filter,
        pending_count=status_counts.get("Pending", 0),
        cancel_requested_count=status_counts.get("Cancel Requested", 0),
        confirmed_count=status_counts.get("Confirmed", 0),
        completed_count=status_counts.get("Completed", 0),
        cancelled_count=status_counts.get("Cancelled", 0),
        page=page,
        total_pages=total_pages,
    )


@app.route("/admin/bookings/<int:booking_id>/update-status", methods=["POST"])
def update_booking_status(booking_id):

    if "username" not in session or session.get("user") != 1:
        return redirect_to_login()

    status = request.form.get("status", "")
    search = request.form.get("search", "")
    status_filter = request.form.get("status_filter", "")
    payment_filter = request.form.get("payment", "")

    if status not in ("Confirmed", "Completed", "Cancelled"):

        flash("Please choose a valid booking status.", "error")

    elif get_open_refund_request(booking_id):

        # This booking's cancellation is a paid refund in progress -- change
        # its status from the refund request instead, so the booking status
        # and the refund's own status can't drift apart.
        flash("This booking has an open refund request. Resolve it from Manage Refunds first.", "error")

    else:

        booking = query("SELECT user_id, package_name, status FROM bookings WHERE id = %s", (booking_id,), fetch="one")
        execute("UPDATE bookings SET status = %s WHERE id = %s", (status, booking_id))
        if booking and booking["status"] != status:
            notify(
                booking["user_id"], f"Your booking #{booking_id} for '{booking['package_name']}' is now {status.lower()}.",
                url_for("my_bookings"), email_subject=f"Your GoGather booking #{booking_id} is {status.lower()}",
            )
            log_admin_activity(
                "booking.status_updated", "booking", booking_id, booking["package_name"],
                old_value=booking["status"], new_value=status,
            )

        flash("Booking status updated.", "success")

    return redirect(url_for("admin_bookings", search=search, status=status_filter, payment=payment_filter))


# MANAGE REFUNDS - ADMIN

@app.route("/admin/refunds")
def admin_refunds():

    if "username" not in session:
        return redirect_to_login()
    if session.get("user") != 1:
        return redirect(url_for("dashboard"))

    status_filter = request.args.get("status", "")

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

    
    fees_row = query(
        "SELECT COALESCE(SUM(cancellation_fee), 0) AS total, COUNT(*) AS total_count "
        "FROM refund_requests WHERE status = 'Refunded' AND cancellation_fee > 0",
        fetch="one",
    )

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
        total_fees_collected=fees_row["total"],
        total_fees_count=fees_row["total_count"],
    )


@app.route("/admin/package-margins")
def admin_package_margins():

    if "username" not in session:
        return redirect_to_login()
    if session.get("user") != 1:
        return redirect(url_for("dashboard"))

    # Where the package margin (price - cost_price) and the GoGather
    # service fee have actually come from, broken down by package -- only
    # 'Paid' bookings count, matching how booking_margin is computed for
    # the dashboard's "Total Profit" figure.
    by_package = query(
        """
        SELECT package_name,
               COUNT(*) AS booking_count,
               SUM(price) AS total_gross,
               SUM(cost_price) AS total_cost,
               SUM(price - cost_price) AS total_margin,
               SUM(service_fee) AS total_service_fee
        FROM bookings
        WHERE payment_status = 'Paid'
        GROUP BY package_name
        ORDER BY total_margin DESC
        """
    )

    total_margin = sum(row["total_margin"] for row in by_package)
    highest_margin = max((row["total_margin"] for row in by_package), default=0)

    for row in by_package:
        row["bar_percent"] = round((row["total_margin"] / highest_margin) * 100) if highest_margin else 0
        row["share_percent"] = round((row["total_margin"] / total_margin) * 100) if total_margin else 0
        row["margin_percent"] = round((row["total_margin"] / row["total_gross"]) * 100) if row["total_gross"] else 0

    return render_template(
        "admin_package_margins.html",
        by_package=by_package,
        total_margin=total_margin,
        total_service_fee=sum(row["total_service_fee"] for row in by_package),
        total_bookings=sum(row["booking_count"] for row in by_package),
        default_margin_percent=DEFAULT_MARGIN_PERCENT,
    )


@app.route("/admin/cancellation-fees")
def admin_cancellation_fees():

    if "username" not in session:
        return redirect_to_login()
    if session.get("user") != 1:
        return redirect(url_for("dashboard"))

    # Which packages the 20% cancellation fee has actually been extracted
    # from, and how much -- only 'Refunded' rows count, since that's the
    # only point money has actually moved.
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

    return render_template(
        "admin_cancellation_fees.html",
        by_package=by_package,
        total_fee=total_fee,
        total_refunds=sum(row["refund_count"] for row in by_package),
    )


@app.route("/admin/refunds/<int:refund_id>/update-status", methods=["POST"])
def update_refund_status(refund_id):

    if "username" not in session or session.get("user") != 1:
        return redirect_to_login()

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
            notify(
                refund["user_id"], f"Your refund for booking #{refund['booking_id']} has been completed.",
                url_for("my_bookings"), email_subject=f"Your refund for booking #{refund['booking_id']} is complete",
            )
        elif status == "Rejected":
            execute("UPDATE bookings SET status = 'Confirmed' WHERE id = %s", (refund["booking_id"],))
            notify(
                refund["user_id"], f"Your cancellation request for booking #{refund['booking_id']} was declined: {admin_notes}",
                url_for("my_bookings"), email_subject=f"Update on your cancellation request for booking #{refund['booking_id']}",
            )
        else:
            notify(refund["user_id"], f"Your refund request for booking #{refund['booking_id']} is now '{status}'.", url_for("my_bookings"))

        log_admin_activity(
            "refund.status_updated", "refund_request", refund_id, refund["package_name"],
            old_value=refund["status"], new_value=status, notes=admin_notes or None,
        )
        flash("Refund request updated.", "success")

    return redirect(url_for("admin_refunds", status=status_filter))


REPORT_PERIODS = {"all": "All time", "30": "Last 30 days", "90": "Last 90 days", "year": "This year"}


def report_period_bounds(period):
    """Turns a Reports period key ("30", "90", "year", "all") into a
    (period, start_date) pair -- shared by the Reports page and its CSV
    export so the two can never disagree about what a given period means."""
    if period not in REPORT_PERIODS:
        period = "all"
    today = date.today()
    start = (date(today.year, 1, 1) if period == "year" else
             today - timedelta(days=int(period) - 1) if period in ("30", "90") else None)
    return period, start


@app.route("/admin/reports")
def admin_reports():
    if "username" not in session:
        return redirect_to_login()
    if session.get("user") != 1:
        return redirect(url_for("dashboard"))

    period, start = report_period_bounds(request.args.get("period", "all"))
    periods = REPORT_PERIODS
    today = date.today()
    sql = "SELECT package_name, price, cost_price, travellers, status, payment_status, created_at FROM bookings"
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
    paid_value = 0
    paid_margin = 0
    paid_count = 0
    for booking in bookings:
        status = booking["status"]
        statuses[status] = statuses.get(status, 0) + 1
        accepted = status in ("Confirmed", "Completed")
        value = booking["price"] or 0
        if accepted:
            accepted_value += value
            accepted_travellers += booking["travellers"] or 0
        if booking["payment_status"] == "Paid":
            paid_value += value
            paid_margin += value - (booking["cost_price"] or 0)
            paid_count += 1
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
        paid_value=paid_value, paid_margin=paid_margin, paid_count=paid_count,
        needs_attention=statuses["Pending"] + statuses["Cancel Requested"],
        statuses=statuses, timeline=timeline,
        top_packages=sorted(packages.values(), key=lambda p: (-p["bookings"], p["name"]))[:5],
        travellers=query("SELECT COUNT(*) AS total FROM users WHERE role = 0", fetch="one")["total"],
        package_count=query("SELECT COUNT(*) AS total FROM packages", fetch="one")["total"],
        group_count=query("SELECT COUNT(*) AS total FROM travel_groups", fetch="one")["total"],
        place_count=len(get_places()),
    )


@app.route("/admin/reports/export.csv")
def export_reports_csv():
    if "username" not in session:
        return redirect_to_login()
    if session.get("user") != 1:
        return redirect(url_for("dashboard"))

    period, start = report_period_bounds(request.args.get("period", "all"))
    today = date.today()

    sql = (
        "SELECT b.id, b.package_name, b.travel_date, b.travellers, b.price, b.cost_price, "
        "b.status, b.payment_status, b.created_at, u.full_name AS traveller_name "
        "FROM bookings b JOIN users u ON u.id = b.user_id"
    )
    params = []
    if start:
        sql += " WHERE b.created_at >= %s AND b.created_at < %s"
        params = [start, today + timedelta(days=1)]
    sql += " ORDER BY b.created_at"
    bookings = query(sql, tuple(params))

    output = StringIO()
    writer = csv.writer(output)
    writer.writerow([
        "Booking ID", "Traveller", "Package", "Travel date", "Travellers",
        "Amount (NPR)", "Margin (NPR)", "Status", "Payment status", "Booked on",
    ])
    for booking in bookings:
        writer.writerow([
            booking["id"], booking["traveller_name"], booking["package_name"] or "",
            booking["travel_date"], booking["travellers"],
            booking["price"], (booking["price"] or 0) - (booking["cost_price"] or 0),
            booking["status"], booking["payment_status"], booking["created_at"],
        ])

    filename = f"gogather-report-{period}-{today.isoformat()}.csv"
    return Response(
        output.getvalue(),
        mimetype="text/csv",
        headers={"Content-Disposition": f"attachment; filename={filename}"},
    )


# ADMIN ACTIVITY HISTORY

ACTIVITY_ENTITY_TYPES = ("booking", "refund_request", "package", "travel_group", "group_member", "place")


@app.route("/admin/activity")
def admin_activity():
    if "username" not in session:
        return redirect_to_login()
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


# SEARCH
# One box in the top bar that finds destinations and packages together, so a
# traveller doesn't have to know which page to start from.

@app.route("/search")
def search():
    if "username" not in session:
        return redirect_to_login()
    if session.get("user") != 0:
        return redirect(url_for("admin_dashboard"))

    q = request.args.get("q", "").strip()[:80]
    place_results, package_results = [], []

    if len(q) >= 2:
        needle = q.lower()
        place_results = [
            place for place in get_places()
            if needle in place["name"].lower() or needle in place["region"].lower()
            or needle in place["description"].lower()
            or any(needle in activity.lower() for activity in place["activities"])
        ][:8]
        like = f"%{q}%"
        package_results = query(
            "SELECT id, name, duration_days, hotel_type, transport_mode, price, "
            "tag, summary, places_covered, activities, includes FROM packages "
            "WHERE name LIKE %s OR places_covered LIKE %s OR tag LIKE %s OR summary LIKE %s "
            "ORDER BY price ASC LIMIT 8",
            (like, like, like, like),
        )
        for package in package_results:
            apply_package_details(package)

    return render_template(
        "search_results.html", q=q, places=place_results, packages=package_results,
        wishlisted_ids=get_wishlisted_package_ids(session["user_id"]),
        wishlisted_place_ids=get_wishlisted_place_ids(session["user_id"]),
    )


# HELP CENTRE & SUPPORT TICKETS
SUPPORT_EMAIL = os.environ.get("SUPPORT_EMAIL", "support@gogather.com")

FAQS = [
    ("How do I book a package?",
     "Open a package, choose Book this package, pick a travel date and the number of travellers. "
     "An admin confirms it, then you can pay from My Bookings."),
    ("How do I pay?",
     "Once a booking is confirmed, open My Bookings and choose eSewa or card. You'll get a notification "
     "and an email when the payment is received."),
    ("Can I cancel a booking?",
     "Yes, from My Bookings. A cancellation fee may apply depending on how close the trip is -- "
     "see the Terms & Conditions. Paid bookings go through a refund request that an admin reviews."),
    ("I forgot my password.",
     "Use Forgot password on the sign-in page. We email a one-time code to the address on your account."),
    ("How do I save a place or package for later?",
     "Tap the heart on any destination or package. Everything you save is on your Wishlist, "
     "where you can go straight to booking."),
    ("How do travel groups work?",
     "Create a group for a trip or join an existing one. An admin approves new groups, and confirmed "
     "members can chat inside the group."),
]

SUPPORT_TICKET_LIMIT_PER_HOUR = 5


@app.route("/help")
def help_center():
    if "username" not in session:
        return redirect_to_login()
    if session.get("user") != 0:
        return redirect(url_for("admin_support"))

    tickets = query(
        """
        SELECT t.id, t.subject, t.message, t.status, t.admin_reply, t.replied_at, t.created_at, b.package_name
        FROM support_tickets t LEFT JOIN bookings b ON b.id = t.booking_id
        WHERE t.user_id = %s ORDER BY t.created_at DESC LIMIT 20
        """,
        (session["user_id"],),
    )
    bookings = query(
        "SELECT id, package_name, travel_date FROM bookings WHERE user_id = %s ORDER BY created_at DESC LIMIT 25",
        (session["user_id"],),
    )
    return render_template(
        "help.html", faqs=FAQS, tickets=tickets, bookings=bookings, support_email=SUPPORT_EMAIL,
    )


@app.route("/help/tickets", methods=["POST"])
def create_support_ticket():
    if "username" not in session:
        return redirect_to_login()
    if session.get("user") != 0:
        return redirect(url_for("admin_support"))

    subject = request.form.get("subject", "").strip()
    message = request.form.get("message", "").strip()
    booking_id = request.form.get("booking_id", type=int)

    if not subject or not message:
        flash("Please add a subject and describe what you need help with.", "error")
    elif len(subject) > 120 or len(message) > 2000:
        flash("Please keep the subject under 120 characters and the message under 2000.", "error")
    elif rate_limited("support-ticket", str(session["user_id"]), SUPPORT_TICKET_LIMIT_PER_HOUR, 3600):
        flash("You've sent several requests in the last hour. Please wait for a reply before sending more.", "error")
    else:
        # A ticket can only be tied to the sender's own booking.
        if booking_id and not query(
            "SELECT id FROM bookings WHERE id = %s AND user_id = %s", (booking_id, session["user_id"]), fetch="one",
        ):
            booking_id = None

        ticket_id = execute(
            "INSERT INTO support_tickets (user_id, booking_id, subject, message) VALUES (%s, %s, %s, %s)",
            (session["user_id"], booking_id, subject, message),
        )
        notify_admins(f"New support request from {session['username']}: {subject}", url_for("admin_support"))
        flash(f"Thanks -- your request #{ticket_id} was sent. We'll reply here and notify you.", "success")

    return redirect(url_for("help_center"))


@app.route("/admin/support")
def admin_support():
    if "username" not in session or session.get("user") != 1:
        return redirect_to_login()

    status = request.args.get("status", "")
    sql = (
        "SELECT t.id, t.subject, t.message, t.status, t.admin_reply, t.replied_at, t.created_at, "
        "u.username, u.email, b.package_name, t.booking_id "
        "FROM support_tickets t JOIN users u ON u.id = t.user_id "
        "LEFT JOIN bookings b ON b.id = t.booking_id"
    )
    params = []
    if status in ("Open", "Answered", "Closed"):
        sql += " WHERE t.status = %s"
        params.append(status)
    # Open tickets first, oldest first, so nothing waits at the bottom.
    sql += " ORDER BY (t.status = 'Open') DESC, t.created_at ASC LIMIT 100"

    return render_template("admin_support.html", tickets=query(sql, tuple(params)), selected_status=status)


@app.route("/admin/support/<int:ticket_id>/reply", methods=["POST"])
def reply_support_ticket(ticket_id):
    if "username" not in session or session.get("user") != 1:
        return redirect_to_login()

    reply = request.form.get("reply", "").strip()
    new_status = request.form.get("status", "Answered")
    ticket = query("SELECT id, user_id, subject, status FROM support_tickets WHERE id = %s", (ticket_id,), fetch="one")

    if ticket is None:
        flash("Support request not found.", "error")
    elif new_status not in ("Answered", "Closed"):
        flash("Please choose a valid status.", "error")
    elif not reply:
        flash("Please write a reply before sending.", "error")
    elif len(reply) > 2000:
        flash("Please keep the reply under 2000 characters.", "error")
    else:
        execute(
            "UPDATE support_tickets SET admin_reply = %s, status = %s, replied_at = NOW() WHERE id = %s",
            (reply, new_status, ticket_id),
        )
        notify(
            ticket["user_id"], f"Support replied to your request '{ticket['subject']}'.",
            url_for("help_center"), email_subject=f"Reply to your GoGather support request #{ticket_id}",
        )
        log_admin_activity(
            "support.replied", "support_ticket", ticket_id, ticket["subject"],
            old_value=ticket["status"], new_value=new_status,
        )
        flash("Reply sent.", "success")

    return redirect(url_for("admin_support", status=request.form.get("status_filter", "")))


# NOTIFICATIONS

@app.route("/notifications")
def notifications():
    if "user_id" not in session:
        return redirect_to_login()

    items = query(
        "SELECT id, message, link, is_read, created_at FROM notifications WHERE user_id = %s ORDER BY created_at DESC, id DESC",
        (session["user_id"],),
    )
    return render_template("notifications.html", notifications=items, is_admin=session.get("user") == 1)


@app.route("/notifications/read", methods=["POST"])
def read_notifications():
    if "user_id" not in session:
        return redirect_to_login()
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
        run_schema_migrations()
    # Off by default -- Werkzeug's interactive debugger lets anyone who can
    # reach it run arbitrary code, so it must be turned on deliberately (via
    # .env) rather than being the hardcoded default.
    app.run(debug=os.environ.get("FLASK_DEBUG", "0") == "1")
