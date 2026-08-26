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
    cursor = conn.cursor(dictionary=True)
    cursor.execute(sql, params or ())
    if fetch == "one":
        row = cursor.fetchone()
        cursor.fetchall()
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