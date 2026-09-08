import tempfile
import unittest
from pathlib import Path

from app import app, PLACES
from place_store import get_places


class PlacesTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.previous = dict(app.config)
        app.config.update(TESTING=True, PLACES_DATABASE=str(Path(self.directory.name) / "places.db"))
        self.client = app.test_client()
        self.login(1)
        self.client.get('/admin/places')
        with self.client.session_transaction() as session:
            self.token = session['places_csrf']
        self.data = dict(PLACES[0], name='Ilam Tea Gardens', action='add', csrf_token=self.token)
        self.data.update(highlights='Tea gardens, Sunrise', activities='Walking, Tea tasting')

    def tearDown(self):
        app.config.clear()
        app.config.update(self.previous)
        self.directory.cleanup()

    def login(self, role):
        with self.client.session_transaction() as session:
            session['username'] = 'tester'
            session['user'] = role

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
            self.assertTrue(any(p['name'] == 'Ilam Hills' for p in get_places(PLACES)))
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
        self.assertTrue(self.client.get('/admin/places').location.endswith('/login'))

    def test_empty_catalogue_stays_empty(self):
        for place in PLACES:
            self.client.post('/admin/places', data=dict(action='delete', slug=place['slug'], csrf_token=self.token))
        with app.app_context():
            self.assertEqual(get_places(PLACES), [])
        self.assertIn(b'Start your collection', self.client.get('/admin/places').data)


if __name__ == '__main__':
    unittest.main()
