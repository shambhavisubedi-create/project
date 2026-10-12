import unittest

from app import app
from db import execute, query
from place_store import SEED_PLACES, get_places


class PlacesTests(unittest.TestCase):
    def setUp(self):
        app.config.update(TESTING=True)
        self.client = app.test_client()
        self.login(1)
        self.client.get('/admin/places')
        with self.client.session_transaction() as session:
            self.token = session['csrf_token']
        # These tests run against the same MySQL database the app uses, so
        # snapshot the real rows here and restore them in tearDown instead
        # of touching a disposable per-test database.
        with app.app_context():
            self.snapshot = query("SELECT * FROM explore_places ORDER BY id")
        self.data = dict(SEED_PLACES[0], name='Ilam Tea Gardens', action='add', csrf_token=self.token)
        self.data.update(highlights='Tea gardens, Sunrise', activities='Walking, Tea tasting')
        self.data.pop('slug', None)

    def tearDown(self):
        with app.app_context():
            execute("DELETE FROM explore_places")
            for row in self.snapshot:
                execute(
                    """
                    INSERT INTO explore_places
                        (id, slug, name, region, category, budget, days, weather, best_time, image, description, highlights, activities)
                    VALUES (%(id)s, %(slug)s, %(name)s, %(region)s, %(category)s, %(budget)s, %(days)s, %(weather)s,
                            %(best_time)s, %(image)s, %(description)s, %(highlights)s, %(activities)s)
                    """,
                    row,
                )

    def login(self, role):
        with self.client.session_transaction() as session:
            session['username'] = 'tester'
            session['user'] = role
            # Real logins always set user_id alongside username/user (see
            # login() in app.py) -- explore_places() relies on it being
            # there, so this fake login needs to match that shape too.
            session['user_id'] = 1

    def test_add_edit_delete_and_traveller_visibility(self):
        self.assertEqual(self.client.post('/admin/places', data=self.data).status_code, 302)
        self.assertEqual(self.client.get('/admin/places?edit=ilam-tea-gardens').status_code, 200)
        self.login(0)
        self.assertIn(b'Ilam Tea Gardens', self.client.get('/explore_places').data)
        self.assertEqual(self.client.get('/explore_places/ilam-tea-gardens').status_code, 200)
        self.login(1)
        self.data.update(action='edit', slug='ilam-tea-gardens', name='Ilam Hills')
        self.assertEqual(self.client.post('/admin/places', data=self.data).status_code, 302)
        with app.app_context():
            self.assertTrue(any(p['name'] == 'Ilam Hills' for p in get_places()))
        self.login(0)
        self.assertIn(b'Ilam Hills', self.client.get('/explore_places/ilam-tea-gardens').data)
        self.login(1)
        self.assertEqual(self.client.post('/admin/places', data=dict(action='delete', slug='ilam-tea-gardens', csrf_token=self.token)).status_code, 302)
        self.login(0)
        self.assertEqual(self.client.get('/explore_places/ilam-tea-gardens').status_code, 302)

    def test_validation_and_csrf(self):
        for changes in [dict(name='  '), dict(category='Unknown'), dict(image='../secret'), dict(highlights=', ,'), dict(description='x' * 2001)]:
            self.assertEqual(self.client.post('/admin/places', data=self.data | changes).status_code, 400)
        self.assertEqual(self.client.post('/admin/places', data=self.data | dict(csrf_token='wrong')).status_code, 400)
        self.client.post('/admin/places', data=self.data)
        duplicate = self.client.post('/admin/places', data=self.data)
        self.assertEqual(duplicate.status_code, 400)
        self.assertIn(b'already exists', duplicate.data)

    def test_access_search_and_missing_places(self):
        self.assertIn(b'No places found', self.client.get('/admin/places?search=zzzzzz').data)
        result = self.client.get('/admin/places?search=POKHARA').data
        self.assertIn(b'Pokhara', result)
        self.assertNotIn(b'Kathmandu Valley', result)
        self.assertEqual(self.client.get('/admin/places?add=1').status_code, 200)
        self.assertEqual(self.client.get('/admin/places?edit=missing').status_code, 404)
        self.login(0)
        self.assertTrue(self.client.get('/admin/places').location.endswith('/dashboard'))
        self.assertEqual(self.client.post('/admin/places', data=self.data).status_code, 302)
        with self.client.session_transaction() as session:
            session.clear()
        self.assertTrue(self.client.get('/admin/places').location.startswith('/login'))

    def test_empty_catalogue_stays_empty(self):
        for place in self.snapshot:
            self.client.post('/admin/places', data=dict(action='delete', slug=place['slug'], csrf_token=self.token))
        with app.app_context():
            self.assertEqual(get_places(), [])
        self.assertIn(b'Start your collection', self.client.get('/admin/places').data)


if __name__ == '__main__':
    unittest.main()
