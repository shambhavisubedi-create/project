import unittest
from datetime import date, datetime, timedelta
from unittest.mock import patch
from app import app

class NotificationTests(unittest.TestCase):
    def setUp(self):
        self.client = app.test_client()
        self.query = patch('app.query').start()
        self.execute = patch('app.execute').start()
        # prepare_notifications() (the before_request hook) runs every
        # self-migrating ensure_*() helper through run_schema_migrations().
        # Mock that one entry point rather than listing each helper by name --
        # a list here silently goes stale whenever a migration is added, and
        # the stray query()/execute() calls then break the assertions below.
        self.ensure = patch('app.run_schema_migrations').start()
        self.addCleanup(patch.stopall)
        previous = app.config.pop('NOTIFICATIONS_READY', None)
        self.addCleanup(lambda: app.config.update(NOTIFICATIONS_READY=previous))

    def login(self, role=0, user_id=7):
        with self.client.session_transaction() as s:
            s.update(user_id=user_id, username='traveller', user=role)

    def _csrf(self):
        # Every POST now goes through check_csrf() in app.py, which compares
        # a submitted "csrf_token" field against session["csrf_token"] --
        # prime both to a known value so these mocked-query tests can still
        # reach the route logic they're actually testing.
        with self.client.session_transaction() as s:
            s['csrf_token'] = 'test-csrf-token'
        return {'csrf_token': 'test-csrf-token'}

    def test_login_required(self):
        csrf = self._csrf()
        for path in ['/notifications', '/notifications/read']:
            response = self.client.post(path, data=csrf) if path.endswith('read') else self.client.get(path)
            self.assertTrue(response.location.endswith('/login'))
        self.query.assert_not_called()
        self.execute.assert_not_called()

    def test_inbox_scoped_persistent_and_escaped_for_both_roles(self):
        for role in (0, 1):
            self.login(role)
            self.query.side_effect = lambda sql, *args, **kw: {'total': 1} if 'COUNT(*)' in sql else [dict(id=1, message='<script>bad</script>', link='/travel_groups', is_read=0, created_at=datetime.now())]
            response = self.client.get('/notifications')
            self.assertEqual(response.status_code, 200)
            self.assertIn(b'&lt;script&gt;', response.data)
            self.assertIn(b'Mark all as read', response.data)
            self.assertIn(b'ADMIN INBOX' if role else b'TRAVELLER INBOX', response.data)
            self.execute.assert_not_called()
            for call in self.query.call_args_list:
                if 'support_tickets' in call.args[0]:
                    continue  # the admin's open-request badge is a site-wide count by design
                # Every other query must be scoped to this user only (some
                # take the id more than once, e.g. the wishlist badge).
                self.assertTrue(call.args[1] and all(param == 7 for param in call.args[1]), call.args)
            self.query.reset_mock()
        self.ensure.assert_called_once()

    def test_mark_read_only_current_user(self):
        self.login(user_id=9)
        self.assertEqual(self.client.post('/notifications/read', data=self._csrf()).status_code, 302)
        self.execute.assert_called_once_with('UPDATE notifications SET is_read = 1 WHERE user_id = %s AND is_read = 0', (9,))

    def test_join_notifies_all_admins_and_requester_once(self):
        self.login()
        csrf = self._csrf()
        group = dict(name='Pokhara', max_members=6, member_count=2, status='Confirmed', travel_date=date.today()+timedelta(days=1))
        self.query.side_effect = [group, None, [{'id': 1}, {'id': 2}]]
        self.assertEqual(self.client.post('/travel_groups/3/join', data=csrf).status_code, 302)
        alerts = [c.args[1] for c in self.execute.call_args_list if 'INSERT INTO notifications' in c.args[0]]
        self.assertEqual([a[0] for a in alerts], [1, 2, 7])
        self.execute.reset_mock()
        self.query.side_effect = [group, {'id': 5, 'status': 'Pending'}]
        self.client.post('/travel_groups/3/join', data=csrf)
        self.execute.assert_not_called()

    def test_admin_decisions_notify_only_affected_traveller(self):
        self.login(role=1)
        csrf = self._csrf()
        for decision in ('Confirmed', 'Rejected'):
            self.query.side_effect = None
            self.query.return_value = dict(user_id=8, status='Pending', group_name='Pokhara')
            self.execute.reset_mock()
            self.client.post('/admin/group_members/4/update-status', data={'status': decision, **csrf})
            alerts = [c.args[1] for c in self.execute.call_args_list if 'INSERT INTO notifications' in c.args[0]]
            self.assertEqual(len(alerts), 1)
            self.assertEqual(alerts[0][0], 8)
            self.assertIn(decision.lower(), alerts[0][1])
        self.query.return_value['status'] = 'Rejected'
        self.execute.reset_mock()
        self.client.post('/admin/group_members/4/update-status', data={'status': 'Rejected', **csrf})
        self.assertFalse(any('INSERT INTO notifications' in c.args[0] for c in self.execute.call_args_list))

    def test_traveller_cannot_send_admin_decisions(self):
        self.login()
        self.assertEqual(
            self.client.post('/admin/group_members/4/update-status', data={'status': 'Confirmed', **self._csrf()}).status_code,
            302,
        )
        self.execute.assert_not_called()

    def test_group_and_booking_decisions_notify_owner(self):
        self.login(role=1)
        csrf = self._csrf()

        # update_group_status() makes exactly one query() call (the group
        # lookup) before deciding whether to notify.
        self.query.side_effect = [dict(created_by=8, name='Pokhara', status='Pending')]
        self.execute.reset_mock()
        self.assertEqual(
            self.client.post('/admin/travel_groups/3/update-status', data={'status': 'Confirmed', **csrf}).status_code,
            302,
        )
        alerts = [c.args[1] for c in self.execute.call_args_list if 'INSERT INTO notifications' in c.args[0]]
        self.assertEqual(len(alerts), 1)
        self.assertEqual(alerts[0][0], 8)
        self.assertIn('confirmed', alerts[0][1])

        # update_booking_status() makes two query() calls: get_open_refund_
        # request() first (None = no open refund in progress), then the
        # booking lookup -- unlike the group route above, so it needs its
        # own side_effect sequence rather than one shared return_value.
        self.query.side_effect = [None, dict(user_id=8, package_name='Pokhara', status='Pending')]
        self.execute.reset_mock()
        self.assertEqual(
            self.client.post('/admin/bookings/3/update-status', data={'status': 'Confirmed', **csrf}).status_code,
            302,
        )
        alerts = [c.args[1] for c in self.execute.call_args_list if 'INSERT INTO notifications' in c.args[0]]
        self.assertEqual(len(alerts), 1)
        self.assertEqual(alerts[0][0], 8)
        self.assertIn('confirmed', alerts[0][1])

    def test_missing_table_initialization_retries_after_failure(self):
        self.login()
        self.ensure.side_effect = [RuntimeError('Database unavailable'), None]
        with app.test_request_context('/'):
            from flask import session
            from app import prepare_notifications
            session['user_id'] = 7
            with self.assertRaises(RuntimeError):
                prepare_notifications()
            self.assertFalse(app.config.get('NOTIFICATIONS_READY'))
            prepare_notifications()
            self.assertTrue(app.config['NOTIFICATIONS_READY'])

if __name__ == '__main__':
    unittest.main()
