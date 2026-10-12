import base64
import json
import os
import unittest
from unittest.mock import patch

import app as app_module
from app import app
from db import execute, query
from test_deployment_features import BaseCase
from werkzeug.security import generate_password_hash

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 1024
JPG = b"\xff\xd8\xff\xe0" + b"\x00" * 1024
GOOD_PASSWORD = "Old-Password-1!"


class ImageTypeDetectionTests(unittest.TestCase):
    def test_real_images_are_recognised_by_content(self):
        self.assertEqual(app_module.detect_image_type(PNG), "png")
        self.assertEqual(app_module.detect_image_type(JPG), "jpg")
        self.assertEqual(app_module.detect_image_type(b"RIFF\x00\x00\x00\x00WEBPVP8 "), "webp")

    def test_renamed_non_images_are_rejected(self):
        self.assertIsNone(app_module.detect_image_type(b"<script>alert(1)</script>"))
        self.assertIsNone(app_module.detect_image_type(b"<?php system($_GET['c']); ?>"))
        self.assertIsNone(app_module.detect_image_type(b""))


class SecurityHeaderTests(BaseCase):
    def test_content_security_policy_is_sent(self):
        csp = self.client.get("/healthz").headers["Content-Security-Policy"]
        for directive in ("object-src 'none'", "base-uri 'self'", "frame-ancestors 'none'"):
            self.assertIn(directive, csp)
        self.assertIn("https://code.highcharts.com", csp)

    def test_login_starts_an_expiring_session_with_a_fresh_csrf_token(self):
        with app.app_context():
            execute("UPDATE users SET password_hash = %s WHERE id = %s",
                    (generate_password_hash(GOOD_PASSWORD), self.user_id))
        self.client.get("/login")
        with self.client.session_transaction() as s:
            before = s["csrf_token"]
        response = self.client.post("/login", data={
            "csrf_token": before, "username": "dep_test_user", "password": GOOD_PASSWORD})
        self.assertEqual(response.status_code, 302)
        with self.client.session_transaction() as s:
            self.assertTrue(s.permanent)
            self.assertNotEqual(s["csrf_token"], before)
            self.assertEqual(s["user_id"], self.user_id)


class LoginThrottleTests(BaseCase):
    def attempt(self, password="wrong-password"):
        self.client.get("/login")
        with self.client.session_transaction() as s:
            token = s["csrf_token"]
        return self.client.post("/login", data={
            "csrf_token": token, "username": "dep_test_user", "password": password})

    def clear_rows(self):
        with app.app_context():
            execute("DELETE FROM login_attempts WHERE throttle_key LIKE 'dep_test_user:%'")

    def setUp(self):
        super().setUp()
        app_module._login_attempts.clear()
        self.clear_rows()
        with app.app_context():
            execute("UPDATE users SET password_hash = %s WHERE id = %s",
                    (generate_password_hash(GOOD_PASSWORD), self.user_id))

    def tearDown(self):
        self.clear_rows()
        super().tearDown()

    def test_lockout_is_stored_in_the_database_and_blocks_even_the_right_password(self):
        for _ in range(app_module.LOGIN_ATTEMPT_LIMIT):
            self.attempt()

        with app.app_context():
            row = query("SELECT locked_until FROM login_attempts WHERE throttle_key LIKE 'dep_test_user:%'",
                        fetch="one")
        self.assertIsNotNone(row["locked_until"])  # persisted, not just in memory

        app_module._login_attempts.clear()  # simulate an app restart
        response = self.attempt(GOOD_PASSWORD)
        self.assertIn(b"Too many failed login attempts", response.data)

    def test_successful_login_clears_the_counter(self):
        self.attempt()
        self.attempt(GOOD_PASSWORD)
        with app.app_context():
            left = query("SELECT COUNT(*) AS n FROM login_attempts WHERE throttle_key LIKE 'dep_test_user:%'",
                         fetch="one")["n"]
        self.assertEqual(left, 0)

    def test_falls_back_to_memory_if_the_database_is_unavailable(self):
        key = "fallback:127.0.0.1"
        with patch("app.query", side_effect=RuntimeError("db down")), \
                patch("app.execute", side_effect=RuntimeError("db down")):
            for _ in range(app_module.LOGIN_ATTEMPT_LIMIT):
                app_module._register_failed_login(key)
            self.assertGreater(app_module._login_locked_seconds_left(key), 0)
        app_module._login_attempts.clear()


class PaymentHardeningTests(BaseCase):
    def test_stripe_webhook_refuses_everything_when_no_secret_is_configured(self):
        # An empty secret means anyone can compute a valid signature.
        with patch.object(app_module, "STRIPE_WEBHOOK_SECRET", ""):
            response = self.client.post("/webhooks/stripe", data=b"{}",
                                        headers={"Stripe-Signature": "t=1,v1=abc"})
        self.assertEqual(response.status_code, 503)

    def test_stripe_webhook_rejects_a_bad_signature(self):
        with patch.object(app_module, "STRIPE_WEBHOOK_SECRET", "whsec_test"):
            response = self.client.post("/webhooks/stripe", data=b"{}",
                                        headers={"Stripe-Signature": "t=1,v1=abc"})
        self.assertEqual(response.status_code, 400)

    def test_sandbox_esewa_key_is_refused_in_production_unless_opted_in(self):
        sandbox = app_module.ESEWA_SANDBOX_SECRET
        with patch.object(app_module, "IS_PRODUCTION", True), \
                patch.object(app_module, "ESEWA_SECRET_KEY", sandbox):
            with patch.dict(os.environ, {}, clear=False):
                os.environ.pop("ESEWA_ALLOW_SANDBOX", None)
                self.assertFalse(app_module.esewa_is_trusted())
            with patch.dict(os.environ, {"ESEWA_ALLOW_SANDBOX": "1"}):
                self.assertTrue(app_module.esewa_is_trusted())
        with patch.object(app_module, "IS_PRODUCTION", True), \
                patch.object(app_module, "ESEWA_SECRET_KEY", "a-real-merchant-secret"):
            self.assertTrue(app_module.esewa_is_trusted())

    def _signed_esewa_payload(self):
        payload = {
            "transaction_code": "000ABC", "status": "COMPLETE", "total_amount": "5000.0",
            "transaction_uuid": "gogather-test-1", "product_code": app_module.ESEWA_PRODUCT_CODE,
            "signed_field_names": "transaction_code,status,total_amount,transaction_uuid,product_code",
        }
        message = ",".join(f"{f}={payload[f]}" for f in payload["signed_field_names"].split(","))
        payload["signature"] = app_module.esewa_sign(message, app_module.ESEWA_SECRET_KEY)
        return base64.b64encode(json.dumps(payload).encode()).decode()

    def _paid_status_after_esewa_return(self, booking_price):
        self.login(self.client)
        with app.app_context():
            booking_id = execute(
                "INSERT INTO bookings (user_id, package_name, travel_date, travellers, phone, price, "
                "status, payment_status, transaction_uuid) "
                "VALUES (%s, 'Sec Test', CURDATE(), 1, '9800000101', %s, 'Confirmed', 'Unpaid', 'gogather-test-1')",
                (self.user_id, booking_price),
            )
        try:
            class Status:
                def json(self):
                    return {"status": "COMPLETE"}

            with patch("app.requests.get", return_value=Status()), \
                    patch("app.notify"):
                self.client.get("/esewa/success", query_string={"data": self._signed_esewa_payload()})
            with app.app_context():
                return query("SELECT payment_status FROM bookings WHERE id = %s", (booking_id,),
                             fetch="one")["payment_status"]
        finally:
            with app.app_context():
                execute("DELETE FROM bookings WHERE id = %s", (booking_id,))

    def test_esewa_marks_paid_when_amount_matches_the_booking(self):
        self.assertEqual(self._paid_status_after_esewa_return(5000), "Paid")

    def test_esewa_does_not_mark_paid_when_a_smaller_amount_was_paid(self):
        self.assertEqual(self._paid_status_after_esewa_return(90000), "Unpaid")


if __name__ == "__main__":
    unittest.main()
