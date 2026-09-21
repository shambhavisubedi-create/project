# Deploying GoGather

This is the path from "works on my laptop" to "real travellers can use it".
Everything below uses only what's already in the project (`manage.py`, `wsgi.py`,
`schema.sql`, `.env`), plus a web server and a MySQL/MariaDB database.

## 1. What you need

| Need | Notes |
|---|---|
| A server (VPS or hosting) | Linux is easiest; Windows works too |
| Python 3.11+ | `pip install -r requirements.txt` |
| MySQL 8 / MariaDB 10.4+ | Create a **dedicated user** for GoGather, not `root` |
| A domain name + HTTPS | Payments and login cookies require HTTPS |
| An SMTP account | For password-reset codes and booking emails (any provider: Gmail app-password, Brevo, Mailgun, your host's mail) |
| Real payment credentials | eSewa merchant + Stripe live keys (see §6) |

## 2. First-time setup

```bash
git clone <your repo> gogather && cd gogather
python -m venv .venv && source .venv/bin/activate      # Windows: .venv\Scripts\activate
pip install -r requirements.txt
pip install gunicorn                                     # Windows: pip install waitress

cp .env.example .env        # then edit .env (see §3)
python manage.py init-db    # creates the database, all tables, and the starter destinations
python manage.py create-admin
python manage.py check      # tells you exactly what is still missing
```

`init-db` is safe to run again later; it only creates what's missing.

## 3. The `.env` file for production

```
SECRET_KEY=<python -c "import secrets; print(secrets.token_hex(32))">
APP_ENV=production
FLASK_DEBUG=0
APP_BASE_URL=https://yourdomain.com
TRUST_PROXY=1                    # only if behind nginx / a load balancer

DB_HOST=localhost
DB_USER=gogather
DB_PASSWORD=<strong password>
DB_NAME=gogather

SMTP_HOST=...  SMTP_PORT=587  SMTP_USER=...  SMTP_PASSWORD=...  SMTP_FROM="GoGather <no-reply@yourdomain.com>"
SUPPORT_EMAIL=support@yourdomain.com

STRIPE_SECRET_KEY=sk_live_...    STRIPE_WEBHOOK_SECRET=whsec_...
ESEWA_PRODUCT_CODE=...           ESEWA_SECRET_KEY=...
ESEWA_FORM_URL=...               ESEWA_STATUS_URL=...     # eSewa's production endpoints (from your merchant docs)
```

Never commit `.env`. `APP_ENV=production` turns on Secure cookies and HSTS.
Without `SMTP_HOST`, password-reset codes cannot be delivered.

## 4. Run it with a real server (not `python app.py`)

```bash
# Linux/macOS
gunicorn wsgi:application --bind 127.0.0.1:8000 --workers 1 --threads 8

# Windows
waitress-serve --listen=127.0.0.1:8000 wsgi:application
```

Use **one worker process with several threads**. The login lock-out and the
rate limiters keep their counts in memory, so with several worker *processes*
each would count separately and the limits would be weaker. One worker with
threads handles a site of this size comfortably.

Keep it running with systemd (Linux) or NSSM / Task Scheduler (Windows) so it
restarts after a crash or reboot. Minimal systemd unit:

```ini
[Service]
WorkingDirectory=/srv/gogather
ExecStart=/srv/gogather/.venv/bin/gunicorn wsgi:application --bind 127.0.0.1:8000 --workers 1 --threads 8
Restart=always
User=gogather
```

## 5. HTTPS with nginx (Linux)

```nginx
server {
    server_name yourdomain.com;
    client_max_body_size 8m;                 # matches the app's 8 MB upload cap
    location /static/ { alias /srv/gogather/static/; expires 7d; }
    location / {
        proxy_pass http://127.0.0.1:8000;
        proxy_set_header Host $host;
        proxy_set_header X-Forwarded-For $remote_addr;
        proxy_set_header X-Forwarded-Proto $scheme;
    }
}
```

Then `sudo certbot --nginx -d yourdomain.com` for a free certificate.

## 6. Payments: going live

- **eSewa** — the app defaults to eSewa's public *sandbox* credentials and test
  endpoints, which cannot collect real money. Set `ESEWA_PRODUCT_CODE`,
  `ESEWA_SECRET_KEY`, `ESEWA_FORM_URL` and `ESEWA_STATUS_URL` in `.env` to the
  production values from your eSewa merchant account (no code change needed).
- **Stripe** — use live keys, and in the Stripe dashboard add a webhook to
  `https://yourdomain.com/webhooks/stripe` for `checkout.session.completed`;
  put its signing secret in `STRIPE_WEBHOOK_SECRET`.
- Test one small real payment end to end, then refund it.

## 7. Scheduled jobs

| Job | Command | When |
|---|---|---|
| Database backup | `python manage.py backup` | nightly |
| Trip reminders | `python manage.py send-reminders` | daily, morning |

Cron example:
```
0 2 * * *  cd /srv/gogather && .venv/bin/python manage.py backup
0 8 * * *  cd /srv/gogather && .venv/bin/python manage.py send-reminders
```
(Windows: Task Scheduler with the same commands.) Reminders also go out
automatically, at most hourly, whenever someone opens their dashboard, so the
site still works if you forget the cron job — but don't rely on that alone.

**A backup you have never restored is not a backup.** Copy `backups/` off the
server (another machine or cloud storage), and once, before launch, restore
one into a scratch database to prove it works:
`mysql -u root -p gogather_restore_test < backups/gogather_YYYYMMDD_HHMMSS.sql`

## 8. Monitoring

- `GET /healthz` returns `200 {"status":"ok"}` when the site can reach its
  database and `503` when it can't. Point a free uptime monitor
  (UptimeRobot, Better Stack, …) at it and have it email you.
- Errors and email problems are written to `logs/gogather.log` (rotated
  automatically). Check it after launch and after each update.

## 9. Go-live checklist

- [ ] `python manage.py check` reports no `FAIL`
- [ ] Site loads over `https://` and `http://` redirects to it
- [ ] Register → log in → forgot-password code **arrives by email**
- [ ] Book → admin confirms → pay (real small amount) → email arrives → refund it
- [ ] `/healthz` monitored; a test alert reaches you
- [ ] Backup ran once **and a restore was tested**
- [ ] Admin password is strong; no test/demo accounts remain
- [ ] Privacy Policy and Terms (`/privacy`, `/terms`) reviewed by you for your real business details
- [ ] Someone is named responsible for the support inbox (`/admin/support`) and `logs/`

## 10. Updating a live site

```bash
git pull && pip install -r requirements.txt
python manage.py backup            # always, before an update
sudo systemctl restart gogather    # new tables/columns are added automatically on start
```

## Known limits (be aware, not blockers for a small launch)

- Rate limits and the login lock-out are per process (see §4).
- Seat capacity is checked when a booking is made; two people booking the very
  last seat in the same instant could both get through.
- SMS is not implemented; codes and notices go by email.
