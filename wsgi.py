"""Entry point for a production web server (gunicorn / waitress), e.g.

    gunicorn wsgi:application --bind 127.0.0.1:8000 --workers 1 --threads 4
    waitress-serve --listen=127.0.0.1:8000 wsgi:application

The Flask development server (`python app.py`) is not meant for real traffic.
"""

from app import app, run_schema_migrations

# Make sure any newly added tables/columns exist before the first request.
with app.app_context():
    run_schema_migrations()

application = app
