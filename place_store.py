"""Persistent destination catalogue, seeded once from the original places."""
import json
import sqlite3
from pathlib import Path

from flask import current_app


def connect_places(seed):
    path = Path(current_app.config.get(
        "PLACES_DATABASE", Path(current_app.instance_path) / "places.sqlite3"
    ))
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path)
    with connection:
        connection.execute("CREATE TABLE IF NOT EXISTS catalogue_meta (id INTEGER PRIMARY KEY)")
        connection.execute("CREATE TABLE IF NOT EXISTS places (slug TEXT PRIMARY KEY, data TEXT NOT NULL)")
        # The marker prevents deleted destinations from returning after restart.
        inserted = connection.execute("INSERT OR IGNORE INTO catalogue_meta VALUES (1)")
        if inserted.rowcount:
            connection.executemany("INSERT INTO places VALUES (?, ?)", [
                (place["slug"], json.dumps(place, ensure_ascii=False)) for place in seed
            ])
    return connection


def get_places(seed):
    connection = connect_places(seed)
    try:
        return [json.loads(row[0]) for row in connection.execute("SELECT data FROM places ORDER BY rowid")]
    finally:
        connection.close()


def save_place(seed, place, creating=False):
    connection = connect_places(seed)
    try:
        with connection:
            if creating:
                connection.execute("INSERT INTO places VALUES (?, ?)",
                                   (place["slug"], json.dumps(place, ensure_ascii=False)))
            else:
                connection.execute("UPDATE places SET data = ? WHERE slug = ?",
                                   (json.dumps(place, ensure_ascii=False), place["slug"]))
    finally:
        connection.close()


def remove_place(seed, slug):
    connection = connect_places(seed)
    try:
        with connection:
            connection.execute("DELETE FROM places WHERE slug = ?", (slug,))
    finally:
        connection.close()
