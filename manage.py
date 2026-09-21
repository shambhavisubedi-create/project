"""GoGather operations toolbox -- setting up and looking after a deployment.

    python manage.py check             preflight: is this environment ready to go live?
    python manage.py init-db           create the database + tables on a fresh server
    python manage.py create-admin      create an administrator account
    python manage.py send-reminders    email/notify travellers whose trip is close (run daily)
    python manage.py backup            dump the database to backups/ (run nightly)

Everything reads the same .env as the app. Standard library only.
"""

import argparse
import getpass
import os
import re
import shutil
import subprocess
import sys
from datetime import datetime
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

ROOT = Path(__file__).resolve().parent


def _db_settings():
    return {
        "host": os.environ.get("DB_HOST", "localhost"),
        "port": os.environ.get("DB_PORT", "3306"),
        "user": os.environ.get("DB_USER", "root"),
        "password": os.environ.get("DB_PASSWORD", ""),
        "name": os.environ.get("DB_NAME", "gogather"),
    }


# ---------------------------------------------------------------- check
def cmd_check(_args):
    problems, warnings = [], []
    ok = lambda text: print(f"  [ ok ] {text}")

    secret = os.environ.get("SECRET_KEY", "")
    if not secret:
        problems.append("SECRET_KEY is not set.")
    elif len(secret) < 32:
        problems.append("SECRET_KEY is shorter than 32 characters -- generate a longer random one.")
    else:
        ok("SECRET_KEY is set")

    production = os.environ.get("APP_ENV", "development").lower() == "production"
    if production:
        ok("APP_ENV=production")
    else:
        problems.append("APP_ENV is not 'production' (secure cookies and stricter defaults are off).")

    if os.environ.get("FLASK_DEBUG", "0") == "1":
        problems.append("FLASK_DEBUG=1 -- the interactive debugger must never be on in production.")
    else:
        ok("Debug mode is off")

    if os.environ.get("APP_BASE_URL", "").startswith("https://"):
        ok("APP_BASE_URL uses https")
    else:
        warnings.append("APP_BASE_URL is not an https:// address -- links in emails will be wrong/insecure.")

    if os.environ.get("SMTP_HOST"):
        ok("SMTP is configured (emails will be sent)")
    else:
        problems.append("SMTP_HOST is not set -- password-reset codes and booking emails cannot be delivered.")

    if not os.environ.get("STRIPE_SECRET_KEY") or not os.environ.get("STRIPE_WEBHOOK_SECRET"):
        warnings.append("Stripe keys are missing -- card payments will not work.")
    if not os.environ.get("ESEWA_SECRET_KEY") or "rc-epay" in os.environ.get("ESEWA_FORM_URL", "rc-epay"):
        warnings.append("eSewa is still on its public SANDBOX (set ESEWA_SECRET_KEY, ESEWA_FORM_URL and "
                        "ESEWA_STATUS_URL to production values) -- no real money can be collected.")

    if os.environ.get("TRUST_PROXY") != "1":
        warnings.append("TRUST_PROXY is not 1 -- fine without a reverse proxy; behind nginx it must be 1 "
                        "or every visitor shares one IP for rate limiting.")

    try:
        from app import app
        from db import query

        with app.app_context():
            query("SELECT 1 AS ok", fetch="one")
            admins = query("SELECT COUNT(*) AS total FROM users WHERE role = 1", fetch="one")["total"]
        ok("Database connection works")
        if admins:
            ok(f"{admins} admin account(s) exist")
        else:
            problems.append("No admin account yet -- run: python manage.py create-admin")
    except Exception as error:
        problems.append(f"Could not start the app / reach the database: {error}")

    uploads = ROOT / "static" / "uploads"
    if uploads.is_dir() and os.access(uploads, os.W_OK):
        ok("static/uploads is writable")
    else:
        warnings.append("static/uploads is missing or not writable -- photo uploads will fail.")

    print()
    for warning in warnings:
        print(f"  [warn] {warning}")
    for problem in problems:
        print(f"  [FAIL] {problem}")
    print()
    if problems:
        print(f"{len(problems)} problem(s) to fix before going live.")
        return 1
    print("Ready to go live." if not warnings else "No blocking problems (review the warnings above).")
    return 0


# -------------------------------------------------------------- init-db
def cmd_init_db(_args):
    import mysql.connector

    settings = _db_settings()
    schema_file = ROOT / "schema.sql"
    if not schema_file.is_file():
        print("schema.sql not found.")
        return 1

    # Connect without a database first so the database itself can be created.
    connection = mysql.connector.connect(
        host=settings["host"], port=int(settings["port"]), user=settings["user"], password=settings["password"],
    )
    cursor = connection.cursor()
    cursor.execute(
        f"CREATE DATABASE IF NOT EXISTS `{settings['name']}` CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci"
    )
    cursor.execute(f"USE `{settings['name']}`")

    # schema.sql is plain CREATE TABLE IF NOT EXISTS statements, one per ';'.
    sql = "\n".join(
        line for line in schema_file.read_text(encoding="utf-8").splitlines() if not line.startswith("--")
    )
    for statement in sql.split(";\n"):
        statement = statement.strip().rstrip(";")
        if statement:
            cursor.execute(statement)
    connection.commit()
    cursor.close()
    connection.close()
    print(f"Tables ready in database '{settings['name']}'.")

    # Anything the app adds on top of the base schema, plus the starter
    # destinations (seeded the first time places are read).
    from app import app, run_schema_migrations
    from place_store import get_places

    with app.app_context():
        run_schema_migrations()
        print(f"{len(get_places())} destinations available.")
    print("Next: python manage.py create-admin")
    return 0


# --------------------------------------------------------- create-admin
def cmd_create_admin(args):
    from werkzeug.security import generate_password_hash

    from app import app, run_schema_migrations
    from db import execute, query

    username = args.username or input("Admin username: ").strip()
    email = args.email or input("Admin email: ").strip()
    full_name = args.full_name or input("Full name: ").strip() or username
    password = os.environ.get("ADMIN_PASSWORD") or getpass.getpass("Password (min 10 characters): ")

    if not username or not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", email):
        print("A username and a valid email are required.")
        return 1
    if len(password) < 10:
        print("Use a password of at least 10 characters for an admin account.")
        return 1

    with app.app_context():
        run_schema_migrations()
        if query("SELECT id FROM users WHERE username = %s OR email = %s", (username, email), fetch="one"):
            print("That username or email already exists.")
            return 1
        execute(
            "INSERT INTO users (full_name, username, email, password_hash, role) VALUES (%s, %s, %s, %s, 1)",
            (full_name, username, email, generate_password_hash(password)),
        )
    print(f"Admin '{username}' created. Log in at /login.")
    return 0


# -------------------------------------------------------- send-reminders
def cmd_send_reminders(args):
    from app import app, run_schema_migrations, send_trip_reminders

    with app.app_context():
        run_schema_migrations()
        count = send_trip_reminders(args.days)
    print(f"Sent {count} trip reminder(s).")
    return 0


# ---------------------------------------------------------------- backup
def cmd_backup(args):
    dump = os.environ.get("MYSQLDUMP_PATH") or shutil.which("mysqldump")
    if not dump:
        print("mysqldump not found. Install the MySQL client tools or set MYSQLDUMP_PATH in .env.")
        return 1

    settings = _db_settings()
    folder = ROOT / "backups"
    folder.mkdir(exist_ok=True)
    target = folder / f"{settings['name']}_{datetime.now():%Y%m%d_%H%M%S}.sql"

    # The password goes through the environment, not the command line, so it
    # doesn't show up in the process list.
    env = dict(os.environ, MYSQL_PWD=settings["password"])
    with open(target, "wb") as out:
        result = subprocess.run(
            [dump, "-h", settings["host"], "-P", settings["port"], "-u", settings["user"],
             "--single-transaction", "--routines", settings["name"]],
            stdout=out, stderr=subprocess.PIPE, env=env,
        )
    if result.returncode != 0:
        target.unlink(missing_ok=True)
        print("Backup failed:", result.stderr.decode(errors="replace").strip())
        return 1

    print(f"Backup written: {target} ({target.stat().st_size // 1024} KB)")

    # Keep the newest N so the disk doesn't fill up.
    old = sorted(folder.glob(f"{settings['name']}_*.sql"))[:-args.keep]
    for path in old:
        path.unlink()
    if old:
        print(f"Removed {len(old)} old backup(s); keeping the newest {args.keep}.")
    print("Reminder: copy backups OFF this server, and test a restore now and then.")
    return 0


def main():
    parser = argparse.ArgumentParser(description="GoGather operations toolbox")
    commands = parser.add_subparsers(dest="command", required=True)

    commands.add_parser("check", help="preflight check for going live").set_defaults(run=cmd_check)
    commands.add_parser("init-db", help="create database and tables").set_defaults(run=cmd_init_db)

    admin = commands.add_parser("create-admin", help="create an administrator")
    admin.add_argument("--username")
    admin.add_argument("--email")
    admin.add_argument("--full-name", dest="full_name")
    admin.set_defaults(run=cmd_create_admin)

    reminders = commands.add_parser("send-reminders", help="send upcoming-trip reminders")
    reminders.add_argument("--days", type=int, default=3, help="remind this many days ahead (default 3)")
    reminders.set_defaults(run=cmd_send_reminders)

    backup = commands.add_parser("backup", help="dump the database to backups/")
    backup.add_argument("--keep", type=int, default=14, help="how many backups to keep (default 14)")
    backup.set_defaults(run=cmd_backup)

    args = parser.parse_args()
    sys.exit(args.run(args))


if __name__ == "__main__":
    main()
