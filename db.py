import os
import mysql.connector
from mysql.connector import pooling
from flask import g

DB_CONFIG = {
    "host": os.environ.get("DB_HOST", "localhost"),
    "port": int(os.environ.get("DB_PORT", 3306)),
    "user": os.environ.get("DB_USER", "root"),
    "password": os.environ.get("DB_PASSWORD", ""),
    "database": os.environ.get("DB_NAME", "gogather"),
    "autocommit": False,
}

# Hosted databases such as TiDB Cloud only accept encrypted connections. Set
# DB_SSL=1 to connect over TLS and verify the server against certifi's CA
# bundle (certifi is installed with `requests`). Local XAMPP leaves it unset.
if os.environ.get("DB_SSL") == "1":
    import certifi

    DB_CONFIG.update(
        ssl_disabled=False,
        ssl_ca=certifi.where(),
        ssl_verify_cert=True,
        ssl_verify_identity=True,
    )

_pool = None


def _get_pool():
    global _pool
    if _pool is None:
        _pool = pooling.MySQLConnectionPool(
            pool_name="gogather_pool",
            pool_size=5,
            **DB_CONFIG,
        )
    return _pool


def get_db():
    if "db" not in g:
        g.db = _get_pool().get_connection()
    return g.db


def close_db(e=None):
    db = g.pop("db", None)
    if db is not None:
        db.close()


def query(sql, params=None, fetch="all"):
    conn = get_db()
    cursor = conn.cursor(dictionary=True, buffered=True)
    cursor.execute(sql, params or ())
    if fetch == "one":
        row = cursor.fetchone()
        cursor.close()
        return row
    rows = cursor.fetchall()
    cursor.close()
    return rows


def execute(sql, params=None):
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute(sql, params or ())
    conn.commit()
    last_id = cursor.lastrowid
    cursor.close()
    return last_id
