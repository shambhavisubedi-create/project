"""Persistent destination catalogue, stored in MySQL alongside the rest of the app's data."""
import json
import sqlite3
from pathlib import Path

from flask import current_app
from mysql.connector import IntegrityError

from db import execute, query

# Fallback seed used only if the explore_places table is empty and there's
# no legacy SQLite catalogue to import from (e.g. a brand new database).
SEED_PLACES = [
    {"slug": "kathmandu-valley", "name": "Kathmandu Valley", "region": "Bagmati", "category": "Cultural", "budget": "Moderate", "days": "2–3 days", "weather": "Mild", "best_time": "October–December, February–April", "image": "ktm.webp", "description": "A living heritage valley of courtyards, stupas, temples and vibrant local markets.", "highlights": ["Swayambhunath Stupa", "Patan Durbar Square", "Boudhanath"], "activities": ["Heritage walks", "Food tours", "Photography"]},
    {"slug": "pokhara", "name": "Pokhara", "region": "Gandaki", "category": "Adventure", "budget": "Moderate", "days": "3–4 days", "weather": "Mild", "best_time": "September–November, March–May", "image": "pokhara.jpg", "description": "A relaxed lakeside city framed by Annapurna views, perfect for soft adventure and slow mornings.", "highlights": ["Phewa Lake", "World Peace Pagoda", "Sarangkot"], "activities": ["Boating", "Paragliding", "Café hopping"]},
    {"slug": "chitwan", "name": "Chitwan National Park", "region": "Bagmati", "category": "Wildlife", "budget": "Moderate", "days": "2–3 days", "weather": "Warm", "best_time": "October–March", "image": "chitwan.jpg", "description": "A subtropical escape for guided jungle safaris, Tharu culture and quiet riverside sunsets.", "highlights": ["Jeep safari", "Rapti River", "Tharu villages"], "activities": ["Wildlife safari", "Canoeing", "Cultural show"]},
    {"slug": "lumbini", "name": "Lumbini", "region": "Lumbini", "category": "Cultural", "budget": "Budget", "days": "1–2 days", "weather": "Warm", "best_time": "October–March", "image": "lumbini.jpg", "description": "The birthplace of Lord Buddha, offering peaceful monastic zones and reflective cultural travel.", "highlights": ["Maya Devi Temple", "Sacred Garden", "Monastic Zone"], "activities": ["Pilgrimage walk", "Meditation", "Cycling"]},
    {"slug": "everest-region", "name": "Everest Region", "region": "Koshi", "category": "Trekking", "budget": "Premium", "days": "12–16 days", "weather": "Cold", "best_time": "March–May, October–November", "image": "Everest-Region-Nepal.jpeg", "description": "An unforgettable high-Himalayan journey through Sherpa villages, suspension bridges and mountain panoramas.", "highlights": ["Namche Bazaar", "Tengboche", "Everest Base Camp"], "activities": ["Trekking", "Mountain photography", "Sherpa culture"]},
    {"slug": "nagarkot", "name": "Nagarkot", "region": "Bagmati", "category": "Nature", "budget": "Budget", "days": "1–2 days", "weather": "Cool", "best_time": "October–April", "image": "nagarkot.jpg", "description": "A hilltop retreat near Kathmandu known for Himalayan sunrises and calm weekend stays.", "highlights": ["Sunrise viewpoint", "Panoramic trails", "Bhaktapur nearby"], "activities": ["Hiking", "Sunrise viewing", "Relaxation"]},
    {"slug": "bandipur", "name": "Bandipur", "region": "Gandaki", "category": "Cultural", "budget": "Moderate", "days": "1–2 days", "weather": "Mild", "best_time": "September–May", "image": "Bandipur-Nepal.jpg", "description": "A beautifully preserved hill town where Newari architecture meets wide views of the central Himalaya.", "highlights": ["Bandipur Bazaar", "Tundikhel", "Siddha Cave"], "activities": ["Village walks", "Cave visit", "Local cuisine"]},
    {"slug": "mustang", "name": "Upper Mustang", "region": "Gandaki", "category": "Adventure", "budget": "Premium", "days": "7–10 days", "weather": "Dry", "best_time": "May–October", "image": "Upper-Mustang.jpg", "description": "A dramatic trans-Himalayan landscape of ochre cliffs, ancient monasteries and walled villages.", "highlights": ["Lo Manthang", "Muktinath", "Kali Gandaki Valley"], "activities": ["Jeep tour", "Trekking", "Monastery visits"]},
    {"slug": "rara-lake", "name": "Rara Lake", "region": "Karnali", "category": "Nature", "budget": "Moderate", "days": "5–7 days", "weather": "Cool", "best_time": "April–June, September–October", "image": "rara.jpg", "description": "A remote alpine lake with clear blue water, pine forests and some of Nepal's most peaceful scenery.", "highlights": ["Rara National Park", "Lakeside trail", "Talcha"], "activities": ["Hiking", "Camping", "Nature photography"]},
    {"slug": "janakpur", "name": "Janakpur", "region": "Madhesh", "category": "Cultural", "budget": "Budget", "days": "1–2 days", "weather": "Warm", "best_time": "October–March", "image": "janakpur.jpg", "description": "A colourful Mithila city centred on Janaki Mandir, sacred tradition and renowned local art.", "highlights": ["Janaki Mandir", "Mithila art", "Ganga Sagar"], "activities": ["Temple visit", "Art workshop", "Food exploration"]},
]


def _ensure_table():
    execute("""
        CREATE TABLE IF NOT EXISTS explore_places (
            id INT AUTO_INCREMENT PRIMARY KEY,
            slug VARCHAR(100) NOT NULL UNIQUE,
            name VARCHAR(150) NOT NULL,
            region VARCHAR(80) NOT NULL,
            category VARCHAR(50) NOT NULL,
            budget VARCHAR(50) NOT NULL,
            days VARCHAR(50) NOT NULL,
            weather VARCHAR(50) NOT NULL,
            best_time VARCHAR(150) NOT NULL,
            image VARCHAR(255) NOT NULL,
            description TEXT NOT NULL,
            highlights JSON NOT NULL,
            activities JSON NOT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
    """)

    # "How to reach" and "hidden gem" were added after the table's first
    # release -- self-migrate them in, and backfill real content for the
    # places that ship with this app the one time the columns are new.
    columns = {row["Field"] for row in query("DESCRIBE explore_places")}
    new_extra_columns = "how_to_reach" not in columns
    if new_extra_columns:
        execute("ALTER TABLE explore_places ADD COLUMN how_to_reach VARCHAR(255) NULL")
        execute("ALTER TABLE explore_places ADD COLUMN is_hidden_gem TINYINT(1) NOT NULL DEFAULT 0")

    # A one-time marker (rather than "reseed whenever the table is empty")
    # so that deleting every place doesn't cause them to silently come back.
    execute("CREATE TABLE IF NOT EXISTS explore_places_seeded (id TINYINT PRIMARY KEY)")
    try:
        execute("INSERT INTO explore_places_seeded (id) VALUES (1)")
    except IntegrityError:
        pass
    else:
        for place in _legacy_sqlite_places() or SEED_PLACES:
            _insert(place)

    if new_extra_columns:
        _backfill_place_extras()


def _backfill_place_extras():
    """Real Nepal travel logistics (not filler) for how each destination is
    actually reached, plus a curated 'hidden gem' flag for the less
    mainstream destinations already in SEED_PLACES -- an editorial call an
    admin would make, not a computed metric."""
    how_to_reach = {
        "kathmandu-valley": "Fly into Tribhuvan International Airport, or take a long-distance bus or private vehicle from other major cities.",
        "pokhara": "Tourist buses and domestic flights run regularly from Kathmandu (around 200km, 6-7 hours by road or a 25-minute flight); also reachable by private vehicle.",
        "chitwan": "Tourist buses and private vehicles from Kathmandu or Pokhara (around 5-6 hours); a short domestic flight to Bharatpur is also available.",
        "lumbini": "Domestic flight to Bhairahawa (Gautam Buddha Airport), or a long-distance bus/private vehicle from Kathmandu or Pokhara (around 7-9 hours by road).",
        "everest-region": "No road access -- fly from Kathmandu to Lukla, then trek in. Flights are frequently delayed by weather, so build in spare days.",
        "nagarkot": "A short drive of around 1.5-2 hours by private vehicle or local bus from Kathmandu.",
        "bandipur": "A short detour off the Kathmandu-Pokhara highway by bus or private vehicle, around 4-5 hours from Kathmandu.",
        "mustang": "Fly or drive to Jomsom, then continue by jeep into the restricted area -- a special permit is required in advance.",
        "rara-lake": "No road access -- fly from Kathmandu or Nepalgunj to the remote Talcha airstrip, then trek in.",
        "janakpur": "Domestic flight to Janakpur Airport, or a long-distance bus/private vehicle from Kathmandu, around 8-9 hours by road.",
    }
    hidden_gems = {"bandipur", "rara-lake", "janakpur"}
    for slug, note in how_to_reach.items():
        execute(
            "UPDATE explore_places SET how_to_reach = %s, is_hidden_gem = %s WHERE slug = %s",
            (note, 1 if slug in hidden_gems else 0, slug),
        )


def _legacy_sqlite_places():
    """One-time import from the old SQLite-backed catalogue (before places
    moved into MySQL), so any edits an admin made there aren't lost."""
    path = Path(current_app.config.get(
        "PLACES_DATABASE", Path(current_app.instance_path) / "places.sqlite3"
    ))
    if not path.exists():
        return None
    connection = sqlite3.connect(path)
    try:
        rows = connection.execute("SELECT data FROM places ORDER BY rowid").fetchall()
    except sqlite3.OperationalError:
        return None
    finally:
        connection.close()
    return [json.loads(row[0]) for row in rows] or None


def _insert(place):
    execute(
        """
        INSERT INTO explore_places
            (slug, name, region, category, budget, days, weather, best_time, image, description, highlights, activities)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        """,
        (
            place["slug"], place["name"], place["region"], place["category"], place["budget"],
            place["days"], place["weather"], place["best_time"], place["image"], place["description"],
            json.dumps(place["highlights"]), json.dumps(place["activities"]),
        ),
    )


def _row_to_place(row):
    place = dict(row)
    for field in ("highlights", "activities"):
        value = place.get(field)
        place[field] = json.loads(value) if isinstance(value, (str, bytes, bytearray)) else (value or [])
    return place


def get_places():
    _ensure_table()
    rows = query("SELECT * FROM explore_places ORDER BY id")
    return [_row_to_place(row) for row in rows]


def save_place(place, creating=False):
    _ensure_table()
    if creating:
        _insert(place)
    else:
        execute(
            """
            UPDATE explore_places
            SET name = %s, region = %s, category = %s, budget = %s, days = %s, weather = %s,
                best_time = %s, image = %s, description = %s, highlights = %s, activities = %s
            WHERE slug = %s
            """,
            (
                place["name"], place["region"], place["category"], place["budget"], place["days"],
                place["weather"], place["best_time"], place["image"], place["description"],
                json.dumps(place["highlights"]), json.dumps(place["activities"]), place["slug"],
            ),
        )


def remove_place(slug):
    _ensure_table()
    execute("DELETE FROM explore_places WHERE slug = %s", (slug,))
