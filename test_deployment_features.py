import re
import unittest
from datetime import date, timedelta
from pathlib import Path
from unittest.mock import patch

import app as app_module
import mailer
from app import app, safe_next
from db import execute, query
from werkzeug.security import check_password_hash, generate_password_hash


class BaseCase(unittest.TestCase):
    """Creates a throwaway traveller (and admin) in the dev database and
    removes everything again afterwards, like the other test files."""

    def setUp(self):
        app.config.update(TESTING=True)
        app_module._rate_hits.clear()
        self.client = app.test_client()
        self.package_ids = []

        with app.app_context():
            app_module.run_schema_migrations()
            self.user_id = execute(
                """
                INSERT INTO users (full_name, username, email, phone, password_hash, role)
                VALUES ('Dep Tester', 'dep_test_user', 'dep_test_user@example.com', '9800000101', %s, 0)
                """,
                (generate_password_hash('old-password-1'),),
            )
            self.admin_id = execute(
                """
                INSERT INTO users (full_name, username, email, phone, password_hash, role)
                VALUES ('Dep Admin', 'dep_test_admin', 'dep_test_admin@example.com', '9800000102', 'x', 1)
                """
            )

    def tearDown(self):
        with app.app_context():
            for package_id in self.package_ids:
                execute("DELETE FROM packages WHERE id = %s", (package_id,))
            execute("DELETE FROM users WHERE id IN (%s, %s)", (self.user_id, self.admin_id))

    def login(self, client, as_admin=False):
        with client.session_transaction() as session:
            session['username'] = 'dep_test_admin' if as_admin else 'dep_test_user'
            session['user'] = 1 if as_admin else 0
            session['user_id'] = self.admin_id if as_admin else self.user_id
        client.get('/admin-dashboard' if as_admin else '/dashboard')
        with client.session_transaction() as session:
            return session['csrf_token']

    def add_package(self, name, price, days):
        with app.app_context():
            package_id = execute(
                """
                INSERT INTO packages (name, duration_days, hotel_type, transport_mode, price, cost_price)
                VALUES (%s, %s, 'standard', 'bus', %s, %s)
                """,
                (name, days, price, price * 0.7),
            )
        self.package_ids.append(package_id)
        return package_id


class HardeningTests(BaseCase):
    def test_health_check_ok_and_reports_database_down(self):
        response = self.client.get('/healthz')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()['database'], 'up')

        with patch('app.query', side_effect=RuntimeError('db down')):
            down = self.client.get('/healthz')
        self.assertEqual(down.status_code, 503)
        self.assertEqual(down.get_json()['status'], 'error')

    def test_security_headers_and_cookie_flags(self):
        response = self.client.get('/healthz')
        self.assertEqual(response.headers['X-Content-Type-Options'], 'nosniff')
        self.assertEqual(response.headers['X-Frame-Options'], 'DENY')
        self.assertIn('Referrer-Policy', response.headers)
        self.assertTrue(app.config['SESSION_COOKIE_HTTPONLY'])
        self.assertEqual(app.config['SESSION_COOKIE_SAMESITE'], 'Lax')

    def test_logged_in_pages_are_not_cached(self):
        self.login(self.client)
        self.assertEqual(self.client.get('/dashboard').headers.get('Cache-Control'), 'no-store')

    def test_unknown_page_shows_friendly_404_not_a_traceback(self):
        response = self.client.get('/definitely-not-a-page')
        self.assertEqual(response.status_code, 404)
        self.assertIn(b'find that page', response.data)
        self.assertNotIn(b'Traceback', response.data)

    def test_error_page_renders_for_server_errors(self):
        with app.test_request_context('/'):
            body, status = app_module.render_error(500)
        self.assertEqual(status, 500)
        self.assertIn('Something went wrong on our side', body)

    def test_bad_csrf_token_gets_friendly_400(self):
        self.login(self.client)
        response = self.client.post('/wishlist/place/1/toggle', data={'csrf_token': 'wrong'})
        self.assertEqual(response.status_code, 400)
        self.assertIn(b'form may have expired', response.data)

    def test_safe_next_only_allows_same_site_paths(self):
        self.assertEqual(safe_next('/packages?x=1', '/f'), '/packages?x=1')
        for bad in ('https://evil.example', '//evil.example', '/\\evil.example', 'javascript:alert(1)', '', None):
            self.assertEqual(safe_next(bad, '/f'), '/f', bad)

    def test_wishlist_toggle_cannot_redirect_off_site(self):
        token = self.login(self.client)
        with app.app_context():
            place_id = query("SELECT id FROM explore_places LIMIT 1", fetch="one")['id']
        response = self.client.post(
            f'/wishlist/place/{place_id}/toggle', data={'csrf_token': token, 'next': 'https://evil.example/x'},
        )
        self.assertEqual(response.status_code, 302)
        self.assertNotIn('evil.example', response.headers['Location'])

    def test_rate_limiter_blocks_after_limit(self):
        with app.test_request_context('/'):
            results = [app_module.rate_limited('unit', 'k', 3, 60) for _ in range(5)]
            other_key = app_module.rate_limited('unit', 'other', 3, 60)
        self.assertEqual(results, [False, False, False, True, True])
        self.assertFalse(other_key)

    def test_schema_sql_covers_every_table_the_app_creates(self):
        schema = Path('schema.sql').read_text(encoding='utf-8')
        created = set()
        for source in ('app.py', 'place_store.py'):
            created |= set(re.findall(r'CREATE TABLE IF NOT EXISTS (\w+)', Path(source).read_text(encoding='utf-8')))
        created |= {'users', 'packages', 'bookings', 'travel_groups', 'group_members'}
        for table in created:
            self.assertIn(f'`{table}`', schema, f'schema.sql is missing {table}')


class MailerTests(unittest.TestCase):
    def test_no_smtp_means_nothing_is_sent(self):
        with patch.dict('os.environ', {}, clear=False) as env:
            env.pop('SMTP_HOST', None)
            self.assertFalse(mailer.send_email('a@example.com', 'Hi', 'Body'))
        self.assertFalse(mailer.send_email('', 'Hi', 'Body'))

    def test_smtp_configured_queues_a_message(self):
        sent = []

        class InlineThread:
            def __init__(self, target, args, daemon):
                self.target, self.args = target, args

            def start(self):
                self.target(*self.args)

        with patch.dict('os.environ', {'SMTP_HOST': 'smtp.test', 'SMTP_FROM': 'from@test'}), \
                patch.object(mailer.threading, 'Thread', InlineThread), \
                patch.object(mailer, '_deliver', side_effect=lambda message: sent.append(message)):
            self.assertTrue(mailer.send_email('to@example.com', 'Subject', 'Body'))
        self.assertEqual(sent[0]['To'], 'to@example.com')
        self.assertEqual(sent[0]['From'], 'from@test')


class PasswordResetTests(BaseCase):
    def test_code_is_emailed_not_shown_and_response_is_identical_for_unknown_accounts(self):
        with patch('app.send_email') as send:
            known = self.client.post('/forgot-password', data={
                'csrf_token': self.get_token(), 'phone': '9800000101'}, follow_redirects=True)
            unknown = self.client.post('/forgot-password', data={
                'csrf_token': self.get_token(), 'phone': '9800000999'}, follow_redirects=True)

        self.assertEqual(send.call_count, 1)
        address, _subject, body = send.call_args[0]
        self.assertEqual(address, 'dep_test_user@example.com')
        code = re.search(r'code is (\d{6})', body).group(1)

        # The code must never appear in the page handed back to whoever asked.
        self.assertNotIn(code.encode(), known.data)
        self.assertEqual(known.status_code, unknown.status_code)
        self.assertIn(b'If that account exists', known.data)
        self.assertIn(b'If that account exists', unknown.data)

    def test_email_address_works_and_new_code_retires_the_old_one(self):
        codes = []
        with patch('app.send_email', side_effect=lambda to, subject, body: codes.append(
                re.search(r'code is (\d{6})', body).group(1))):
            for _ in range(2):
                self.client.post('/forgot-password', data={
                    'csrf_token': self.get_token(), 'phone': 'dep_test_user@example.com'})
        self.assertEqual(len(codes), 2)
        with app.app_context():
            active = query(
                "SELECT COUNT(*) AS n FROM password_reset_tokens WHERE user_id = %s AND used_at IS NULL",
                (self.user_id,), fetch="one")['n']
        self.assertEqual(active, 1)

    def test_full_reset_flow_and_guess_limit(self):
        codes = []
        with patch('app.send_email', side_effect=lambda to, subject, body: codes.append(
                re.search(r'code is (\d{6})', body).group(1))):
            self.client.post('/forgot-password', data={'csrf_token': self.get_token(), 'phone': '9800000101'})

        wrong = '000000' if codes[0] != '000000' else '111111'
        statuses = [
            self.client.post('/verify-otp', data={
                'csrf_token': self.get_token(), 'phone': '9800000101', 'otp': wrong,
                'password': 'Brand-New-Pass1!', 'confirm_password': 'Brand-New-Pass1!'}).status_code
            for _ in range(7)
        ]
        self.assertEqual(statuses[:6], [200] * 6)
        self.assertEqual(statuses[6], 429)   # guessing is capped

        app_module._rate_hits.clear()
        done = self.client.post('/verify-otp', data={
            'csrf_token': self.get_token(), 'phone': '9800000101', 'otp': codes[0],
            'password': 'Brand-New-Pass1!', 'confirm_password': 'Brand-New-Pass1!'})
        self.assertEqual(done.status_code, 302)
        with app.app_context():
            row = query("SELECT password_hash FROM users WHERE id = %s", (self.user_id,), fetch="one")
        self.assertTrue(check_password_hash(row['password_hash'], 'Brand-New-Pass1!'))

    def get_token(self):
        self.client.get('/forgot-password')
        with self.client.session_transaction() as session:
            return session['csrf_token']


class ChangePasswordTests(BaseCase):
    def change(self, token, current, new, confirm=None):
        return self.client.post('/profile/password', data={
            'csrf_token': token, 'current_password': current, 'new_password': new,
            'confirm_password': confirm if confirm is not None else new})

    def hash_matches(self, password):
        with app.app_context():
            row = query("SELECT password_hash FROM users WHERE id = %s", (self.user_id,), fetch="one")
        return check_password_hash(row['password_hash'], password)

    def test_wrong_current_short_and_mismatch_are_rejected(self):
        token = self.login(self.client)
        self.change(token, 'not-my-password', 'Valid-New-Pass1!')
        self.change(token, 'old-password-1', 'short')
        self.change(token, 'old-password-1', 'Valid-New-Pass1!', 'different-pass')
        self.assertTrue(self.hash_matches('old-password-1'))

    def test_correct_change_works(self):
        token = self.login(self.client)
        response = self.change(token, 'old-password-1', 'Valid-New-Pass1!')
        self.assertEqual(response.status_code, 302)
        self.assertTrue(self.hash_matches('Valid-New-Pass1!'))


class TravellerExperienceTests(BaseCase):
    def test_wishlist_ajax_returns_json_and_count(self):
        token = self.login(self.client)
        package_id = self.add_package('DepTest Wishlist Package', 5000, 3)
        headers = {'X-Requested-With': 'fetch'}

        saved = self.client.post(f'/wishlist/{package_id}/toggle', data={'csrf_token': token}, headers=headers)
        self.assertEqual(saved.get_json()['saved'], True)
        self.assertEqual(saved.get_json()['count'], 1)

        removed = self.client.post(f'/wishlist/{package_id}/toggle', data={'csrf_token': token}, headers=headers)
        self.assertEqual(removed.get_json()['saved'], False)
        self.assertEqual(removed.get_json()['count'], 0)

    def test_package_filters_and_sorting(self):
        self.login(self.client)
        self.add_package('ZZFilter Cheap Short', 1111, 2)
        self.add_package('ZZFilter Dear Long', 9999, 9)

        def names(**params):
            html = self.client.get('/packages', query_string=dict(search='ZZFilter', **params)).get_data(as_text=True)
            return re.findall(r'<h2>(ZZFilter[^<]+)</h2>', html)

        self.assertEqual(names(), ['ZZFilter Cheap Short', 'ZZFilter Dear Long'])
        self.assertEqual(names(sort='price_desc'), ['ZZFilter Dear Long', 'ZZFilter Cheap Short'])
        self.assertEqual(names(min_price='5000'), ['ZZFilter Dear Long'])
        self.assertEqual(names(max_price='5000'), ['ZZFilter Cheap Short'])
        self.assertEqual(names(duration='1-3'), ['ZZFilter Cheap Short'])
        self.assertEqual(names(duration='8+'), ['ZZFilter Dear Long'])
        # Junk values are ignored, not an error.
        self.assertEqual(names(min_price='abc', sort='hack; DROP TABLE'), ['ZZFilter Cheap Short', 'ZZFilter Dear Long'])

    def test_search_finds_packages_and_places(self):
        self.login(self.client)
        self.add_package('ZZSearchable Trek', 7000, 5)
        html = self.client.get('/search', query_string={'q': 'ZZSearchable'}).get_data(as_text=True)
        self.assertIn('ZZSearchable Trek', html)
        self.assertIn('Book', html)
        self.assertIn('Kathmandu', self.client.get('/search', query_string={'q': 'kathmandu'}).get_data(as_text=True))
        self.assertIn('at least 2 letters', self.client.get('/search', query_string={'q': 'a'}).get_data(as_text=True))

    def test_admin_cannot_use_traveller_search(self):
        self.login(self.client, as_admin=True)
        self.assertEqual(self.client.get('/search?q=nepal').status_code, 302)

    def test_next_step_guides_through_the_journey(self):
        with app.test_request_context('/'):
            fresh = app_module.build_next_step(self.user_id)
            self.assertEqual(fresh['title'], 'Start by exploring Nepal')
            self.assertEqual([done for _, done in fresh['steps']], [False, False, False, False])

            place_id = query("SELECT id FROM explore_places LIMIT 1", fetch="one")['id']
            execute("INSERT INTO place_wishlist_items (user_id, place_id) VALUES (%s, %s)", (self.user_id, place_id))
            saved = app_module.build_next_step(self.user_id)
            self.assertEqual(saved['title'], 'Turn your saved places into a trip')
            self.assertTrue(saved['steps'][0][1])

            package_id = self.add_package('DepTest Next Step', 4000, 2)
            execute(
                """
                INSERT INTO bookings (user_id, package_id, package_name, travel_date, travellers, phone, price,
                                      status, payment_status)
                VALUES (%s, %s, 'DepTest Next Step', %s, 1, '9800000101', 4000, 'Confirmed', 'Unpaid')
                """,
                (self.user_id, package_id, date.today() + timedelta(days=20)),
            )
            unpaid = app_module.build_next_step(self.user_id)
            self.assertIn('Pay for', unpaid['title'])
            self.assertEqual(unpaid['button'], 'Pay now')

    def test_main_traveller_pages_render(self):
        self.login(self.client)
        for path in ('/dashboard', '/packages', '/explore_places', '/wishlist', '/help', '/profile',
                     '/my_bookings', '/search?q=nepal', '/forgot-password'):
            response = self.client.get(path)
            self.assertEqual(response.status_code, 200, path)
        with app.app_context():
            slug = query("SELECT slug FROM explore_places LIMIT 1", fetch="one")['slug']
        detail = self.client.get(f'/explore_places/{slug}')
        self.assertEqual(detail.status_code, 200)
        self.assertIn(b'id="packages"', detail.data)


class SupportTests(BaseCase):
    def test_ticket_reply_flow(self):
        user = app.test_client()
        admin = app.test_client()
        user_token = self.login(user)
        admin_token = self.login(admin, as_admin=True)

        # A booking that belongs to someone else must not be attachable.
        with app.app_context():
            package_id = self.add_package('DepTest Support Package', 3000, 2)
            other_booking = execute(
                """
                INSERT INTO bookings (user_id, package_id, package_name, travel_date, travellers, phone, price)
                VALUES (%s, %s, 'DepTest Support Package', %s, 1, '9800000102', 3000)
                """,
                (self.admin_id, package_id, date.today() + timedelta(days=30)),
            )

        user.post('/help/tickets', data={
            'csrf_token': user_token, 'subject': 'Charged twice', 'message': 'Please check my payment.',
            'booking_id': other_booking})
        with app.app_context():
            ticket = query("SELECT id, booking_id, status FROM support_tickets WHERE user_id = %s",
                           (self.user_id,), fetch="one")
        self.assertIsNotNone(ticket)
        self.assertIsNone(ticket['booking_id'])
        self.assertEqual(ticket['status'], 'Open')

        self.assertIn(b'Charged twice', admin.get('/admin/support').data)

        with patch('app.send_email') as send:
            admin.post(f"/admin/support/{ticket['id']}/reply", data={
                'csrf_token': admin_token, 'reply': 'Refunded, sorry!', 'status': 'Closed'})
        self.assertEqual(send.call_args[0][0], 'dep_test_user@example.com')

        page = user.get('/help').get_data(as_text=True)
        self.assertIn('Refunded, sorry!', page)
        self.assertIn('Closed', page)
        with app.app_context():
            self.assertEqual(
                query("SELECT COUNT(*) AS n FROM notifications WHERE user_id = %s AND message LIKE 'Support replied%%'",
                      (self.user_id,), fetch="one")['n'], 1)

    def test_validation_and_hourly_limit(self):
        user = app.test_client()
        token = self.login(user)
        user.post('/help/tickets', data={'csrf_token': token, 'subject': '', 'message': ''})
        for number in range(6):
            user.post('/help/tickets', data={'csrf_token': token, 'subject': f'S{number}', 'message': 'm'})
        with app.app_context():
            count = query("SELECT COUNT(*) AS n FROM support_tickets WHERE user_id = %s",
                          (self.user_id,), fetch="one")['n']
        self.assertEqual(count, app_module.SUPPORT_TICKET_LIMIT_PER_HOUR)

    def test_traveller_cannot_open_admin_inbox_or_reply(self):
        user = app.test_client()
        token = self.login(user)
        self.assertEqual(user.get('/admin/support').status_code, 302)
        self.assertEqual(user.post('/admin/support/1/reply', data={'csrf_token': token, 'reply': 'x'}).status_code, 302)


class ReminderTests(BaseCase):
    def test_each_trip_is_reminded_once(self):
        with app.app_context():
            already_pending = {row['id'] for row in query("SELECT id FROM bookings WHERE reminder_sent_at IS NULL")}
            package_id = self.add_package('DepTest Reminder Package', 3000, 2)
            soon = execute(
                """
                INSERT INTO bookings (user_id, package_id, package_name, travel_date, travellers, phone, price, status)
                VALUES (%s, %s, 'DepTest Reminder Package', %s, 1, '9800000101', 3000, 'Confirmed')
                """,
                (self.user_id, package_id, date.today() + timedelta(days=2)),
            )
            far = execute(
                """
                INSERT INTO bookings (user_id, package_id, package_name, travel_date, travellers, phone, price, status)
                VALUES (%s, %s, 'DepTest Reminder Package', %s, 1, '9800000101', 3000, 'Confirmed')
                """,
                (self.user_id, package_id, date.today() + timedelta(days=30)),
            )
            try:
                with patch('app.send_email') as send:
                    app_module.send_trip_reminders()
                    app_module.send_trip_reminders()   # second run must not repeat it
                reminders = query(
                    "SELECT COUNT(*) AS n FROM notifications WHERE user_id = %s AND message LIKE 'Reminder:%%'",
                    (self.user_id,), fetch="one")['n']
                self.assertEqual(reminders, 1)
                self.assertEqual(sum(1 for c in send.call_args_list if c[0][0] == 'dep_test_user@example.com'), 1)
                self.assertIsNotNone(query("SELECT reminder_sent_at FROM bookings WHERE id = %s", (soon,), fetch="one")['reminder_sent_at'])
                self.assertIsNone(query("SELECT reminder_sent_at FROM bookings WHERE id = %s", (far,), fetch="one")['reminder_sent_at'])
            finally:
                # The job looks at every booking in the dev database -- put back
                # the reminder flag on any real ones this run touched.
                if already_pending:
                    marks = ", ".join(["%s"] * len(already_pending))
                    execute(f"UPDATE bookings SET reminder_sent_at = NULL WHERE id IN ({marks})", tuple(already_pending))


if __name__ == '__main__':
    unittest.main()
