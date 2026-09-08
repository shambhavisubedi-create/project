import unittest
from datetime import date, datetime, timedelta
from decimal import Decimal
from unittest.mock import patch

from app import app


class ReportsTests(unittest.TestCase):
    def setUp(self):
        self.client = app.test_client()
        self.query = patch('app.query').start()
        self.places = patch('app.get_places', return_value=[{'name': 'Pokhara'}]).start()
        self.addCleanup(patch.stopall)
        self.query.side_effect = lambda sql, *args, **kwargs: [] if 'FROM bookings' in sql else {'total': 3}

    def login(self, role=1):
        with self.client.session_transaction() as session:
            session['username'] = 'tester'
            session['user'] = role

    def test_admin_access(self):
        self.assertTrue(self.client.get('/admin/reports').location.endswith('/login'))
        self.login(0)
        self.assertTrue(self.client.get('/admin/reports').location.endswith('/dashboard'))
        self.query.assert_not_called()

    def test_empty_report_and_dashboard_link(self):
        self.login()
        response = self.client.get('/admin/reports')
        self.assertEqual(response.status_code, 200)
        self.assertIn(b'No bookings in this period', response.data)
        self.assertIn(b'Rs 0', response.data)
        with app.test_request_context():
            html = app.jinja_env.get_template('admin_dashboard.html').render(featured_packages=[])
        self.assertIn('href="/admin/reports"', html)

    def test_values_statuses_and_ranking(self):
        self.login()
        bookings = [dict(package_name=name, price=Decimal(price), travellers=2,
                         status=status, created_at=datetime.now()) for name, price, status in [
            ('Pokhara', '1000', 'Confirmed'), ('Pokhara', '2000', 'Completed'),
            ('Everest', '8000', 'Cancelled'), ('Everest', '9000', 'Pending'),
            ('Pokhara', '5000', 'Cancel Requested')]]
        self.query.side_effect = lambda sql, *args, **kwargs: bookings if 'FROM bookings' in sql else {'total': 3}
        # Capture the real template context while still rendering the full page.
        from flask import template_rendered
        contexts = []
        def capture(sender, template, context, **extra):
            contexts.append(context)
        with template_rendered.connected_to(capture, app):
            response = self.client.get('/admin/reports')
        self.assertEqual(response.status_code, 200)
        report = contexts[0]
        self.assertEqual(report['accepted_value'], Decimal('3000'))
        self.assertEqual(report['accepted_travellers'], 4)
        self.assertEqual(report['needs_attention'], 2)
        self.assertEqual(report['total'], 5)
        self.assertEqual(report['top_packages'][0]['name'], 'Pokhara')
        self.assertEqual(report['timeline'][-1]['count'], 5)

    def test_period_filters(self):
        self.login()
        for period, start in [('30', date.today() - timedelta(days=29)),
                              ('90', date.today() - timedelta(days=89)),
                              ('year', date(date.today().year, 1, 1))]:
            self.query.reset_mock()
            self.assertEqual(self.client.get('/admin/reports?period=' + period).status_code, 200)
            sql, params = self.query.call_args_list[0].args
            self.assertIn('created_at >= %s AND created_at < %s', sql)
            self.assertEqual(params, (start, date.today() + timedelta(days=1)))
        self.query.reset_mock()
        self.assertEqual(self.client.get('/admin/reports?period=invalid').status_code, 200)
        self.assertNotIn('WHERE', self.query.call_args_list[0].args[0])


if __name__ == '__main__':
    unittest.main()
