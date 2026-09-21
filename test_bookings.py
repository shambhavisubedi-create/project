import unittest
from datetime import date, timedelta

from app import app
from db import execute, query


class SeatCapacityTests(unittest.TestCase):
    """Covers the package seat-capacity limit: bookings for the same
    package/date shouldn't be able to exceed a package's max_seats."""

    def setUp(self):
        app.config.update(TESTING=True)
        self.client = app.test_client()

        with app.app_context():
            self.user_id = execute(
                """
                INSERT INTO users (full_name, username, email, phone, password_hash, role)
                VALUES ('Seat Capacity Tester', 'seat_capacity_tester', 'seat_capacity_tester@example.com',
                        '9800000011', 'x', 0)
                """
            )
            self.package_id = execute(
                """
                INSERT INTO packages (name, duration_days, hotel_type, transport_mode, price, cost_price, max_seats)
                VALUES ('Seat Capacity Test Package', 2, 'standard', 'bus', 10000, 7500, 3)
                """
            )

        with self.client.session_transaction() as session:
            session['username'] = 'seat_capacity_tester'
            session['user'] = 0
            session['user_id'] = self.user_id

        # Loading any page primes session['csrf_token'] (see check_csrf/
        # ensure_csrf_token in app.py) -- read it back so POSTs below pass.
        self.client.get('/book_package')
        with self.client.session_transaction() as session:
            self.token = session['csrf_token']

        self.travel_date = (date.today() + timedelta(days=45)).isoformat()

    def tearDown(self):
        # Cascades: deleting the user removes their bookings and any review
        # left on those bookings (see the ON DELETE CASCADE foreign keys).
        with app.app_context():
            execute("DELETE FROM users WHERE id = %s", (self.user_id,))
            execute("DELETE FROM packages WHERE id = %s", (self.package_id,))

    def book(self, travellers):
        return self.client.post('/book_package', data={
            'csrf_token': self.token,
            'package_id': self.package_id,
            'travel_date': self.travel_date,
            'travellers': travellers,
            # The booking form requires agreeing to the Privacy Policy and Terms.
            'accept_policy': 'on',
        })

    def test_booking_within_capacity_succeeds(self):
        response = self.book(3)
        self.assertEqual(response.status_code, 302)
        with app.app_context():
            booking = query(
                "SELECT travellers FROM bookings WHERE package_id = %s AND user_id = %s",
                (self.package_id, self.user_id), fetch="one",
            )
        self.assertEqual(booking["travellers"], 3)

    def test_booking_beyond_capacity_is_rejected(self):
        first = self.book(2)
        self.assertEqual(first.status_code, 302)

        with app.app_context():
            second_user_id = execute(
                """
                INSERT INTO users (full_name, username, email, phone, password_hash, role)
                VALUES ('Second Tester', 'seat_capacity_tester_2', 'seat_capacity_tester_2@example.com',
                        '9800000012', 'x', 0)
                """
            )
        with self.client.session_transaction() as session:
            session['user_id'] = second_user_id

        try:
            second = self.book(2)  # only 1 seat left (3 - 2 already booked)
            self.assertEqual(second.status_code, 200)
            self.assertIn(b"seat", second.data.lower())
            with app.app_context():
                bookings_for_package = query(
                    "SELECT COUNT(*) AS total FROM bookings WHERE package_id = %s", (self.package_id,), fetch="one"
                )["total"]
            self.assertEqual(bookings_for_package, 1)
        finally:
            with app.app_context():
                execute("DELETE FROM users WHERE id = %s", (second_user_id,))

    def test_unlimited_package_has_no_cap(self):
        with app.app_context():
            execute("UPDATE packages SET max_seats = NULL WHERE id = %s", (self.package_id,))
        first = self.book(20)
        self.assertEqual(first.status_code, 302)


class ReviewTests(unittest.TestCase):
    """Covers the reviews/ratings feature: only a Completed booking can be
    reviewed, and resubmitting on the same booking edits the review rather
    than creating a second row (package_reviews' UNIQUE(booking_id))."""

    def setUp(self):
        app.config.update(TESTING=True)
        self.client = app.test_client()

        with app.app_context():
            self.user_id = execute(
                """
                INSERT INTO users (full_name, username, email, phone, password_hash, role)
                VALUES ('Review Tester', 'review_tester', 'review_tester@example.com', '9800000021', 'x', 0)
                """
            )
            self.package_id = execute(
                """
                INSERT INTO packages (name, duration_days, hotel_type, transport_mode, price, cost_price)
                VALUES ('Review Test Package', 3, 'standard', 'bus', 12000, 9000)
                """
            )
            self.booking_id = execute(
                """
                INSERT INTO bookings (user_id, package_id, package_name, travel_date, travellers, phone,
                                       price, status, payment_status)
                VALUES (%s, %s, 'Review Test Package', %s, 1, '9800000021', 12000, 'Pending', 'Unpaid')
                """,
                (self.user_id, self.package_id, date.today() - timedelta(days=10)),
            )

        with self.client.session_transaction() as session:
            session['username'] = 'review_tester'
            session['user'] = 0
            session['user_id'] = self.user_id

        self.client.get('/my_bookings')
        with self.client.session_transaction() as session:
            self.token = session['csrf_token']

    def tearDown(self):
        with app.app_context():
            execute("DELETE FROM users WHERE id = %s", (self.user_id,))
            execute("DELETE FROM packages WHERE id = %s", (self.package_id,))

    def submit(self, rating, comment=""):
        return self.client.post(f'/my_bookings/{self.booking_id}/review', data={
            'csrf_token': self.token,
            'rating': rating,
            'comment': comment,
        })

    def test_review_rejected_before_trip_is_completed(self):
        response = self.submit(5, "Great trip")
        self.assertEqual(response.status_code, 302)
        with app.app_context():
            review = query("SELECT id FROM package_reviews WHERE booking_id = %s", (self.booking_id,), fetch="one")
        self.assertIsNone(review)

    def test_review_accepted_once_completed_and_editing_updates_in_place(self):
        with app.app_context():
            execute("UPDATE bookings SET status = 'Completed' WHERE id = %s", (self.booking_id,))

        first = self.submit(4, "Good trip")
        self.assertEqual(first.status_code, 302)
        with app.app_context():
            review = query(
                "SELECT id, rating, comment FROM package_reviews WHERE booking_id = %s",
                (self.booking_id,), fetch="one",
            )
        self.assertEqual(review["rating"], 4)

        second = self.submit(5, "Actually, great trip")
        self.assertEqual(second.status_code, 302)
        with app.app_context():
            reviews = query(
                "SELECT id, rating, comment FROM package_reviews WHERE booking_id = %s", (self.booking_id,)
            )
        self.assertEqual(len(reviews), 1)
        self.assertEqual(reviews[0]["rating"], 5)
        self.assertEqual(reviews[0]["comment"], "Actually, great trip")

    def test_out_of_range_rating_is_rejected(self):
        with app.app_context():
            execute("UPDATE bookings SET status = 'Completed' WHERE id = %s", (self.booking_id,))
        response = self.submit(9, "Too many stars")
        self.assertEqual(response.status_code, 302)
        with app.app_context():
            review = query("SELECT id FROM package_reviews WHERE booking_id = %s", (self.booking_id,), fetch="one")
        self.assertIsNone(review)


if __name__ == '__main__':
    unittest.main()
