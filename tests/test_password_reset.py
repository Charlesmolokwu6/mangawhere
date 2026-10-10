import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from server import auth, db, password_reset

SMTP_ENV = {"SMTP_HOST": "smtp.example.com", "MAIL_FROM": "MangaWhere <no-reply@example.com>"}
SITE = "https://charlesmolokwu6.github.io/mangawhere/"


class ResetTestCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self._tmp.close()
        self._old = db.DB_PATH
        db.DB_PATH = Path(self._tmp.name)
        db.init_db()
        self.env = patch.dict("os.environ", SMTP_ENV)
        self.env.start()
        self.sent = []
        self.mail = patch.object(password_reset, "_send_email", side_effect=lambda to, link: self.sent.append((to, link)))
        self.mail.start()
        self._make_user("reader@example.com", "old-password-1")

    def tearDown(self):
        self.mail.stop()
        self.env.stop()
        db.DB_PATH = self._old
        Path(self._tmp.name).unlink(missing_ok=True)

    def _make_user(self, email, password):
        conn = db.get_connection()
        try:
            if password:
                salt = "ab" * 16
                conn.execute(
                    "INSERT INTO users (email, name, password_hash, salt, created_at) VALUES (?, ?, ?, ?, ?)",
                    (email, email.split("@")[0], auth._hash_password(password, salt), salt, time.time()),
                )
            else:
                conn.execute(
                    "INSERT INTO users (email, name, created_at) VALUES (?, ?, ?)",
                    (email, email.split("@")[0], time.time()),
                )
            conn.commit()
        finally:
            conn.close()

    def _token_from_email(self):
        link = self.sent[-1][1]
        self.assertTrue(link.startswith(SITE + "#reset="), link)
        return link.split("#reset=", 1)[1]


class RequestResetTests(ResetTestCase):
    def test_a_known_email_gets_a_one_time_link_to_the_readers_site(self):
        result = password_reset.request_reset("Reader@Example.com ", SITE + "?m=123")
        self.assertEqual(result, {"message": password_reset.SENT_MESSAGE})
        self.assertEqual(self.sent[0][0], "reader@example.com")
        token = self._token_from_email()
        conn = db.get_connection()
        try:
            stored = conn.execute("SELECT token_hash FROM password_resets").fetchone()["token_hash"]
        finally:
            conn.close()
        self.assertNotEqual(stored, token)  # only a hash is stored

    def test_the_email_has_a_button_and_a_plain_text_link(self):
        link = SITE + "#reset=abc_DEF-123"
        message = password_reset.build_message("MangaWhere <no-reply@example.com>", "reader@example.com", link)
        plain = message.get_body(("plain",)).get_content()
        html_part = message.get_body(("html",)).get_content()
        self.assertIn(link, plain)                       # for email apps that can't show HTML
        self.assertIn(f'href="{link}"', html_part)
        self.assertIn(">Reset password</a>", html_part)
        self.assertEqual(html_part.count(link), 1)       # only behind the button, not written out
        self.assertEqual(message["To"], "reader@example.com")

    def test_an_unknown_email_gets_the_same_answer_and_no_email(self):
        result = password_reset.request_reset("nobody@example.com", SITE)
        self.assertEqual(result, {"message": password_reset.SENT_MESSAGE})
        self.assertEqual(self.sent, [])

    def test_links_only_ever_point_at_our_own_sites(self):
        result = password_reset.request_reset("reader@example.com", "https://evil.example.net/phish")
        self.assertIn("error", result)
        self.assertEqual(self.sent, [])

    def test_the_link_never_carries_the_query_string(self):
        # ?api= picks the server the page talks to; carrying it into the
        # email would let a stranger point a victim's reset page at theirs.
        password_reset.request_reset("reader@example.com", SITE + "?api=https://evil.example.net")
        self.assertNotIn("evil", self.sent[0][1])
        self.assertTrue(self.sent[0][1].startswith(SITE + "#reset="))

    def test_repeated_requests_are_capped_per_hour(self):
        for _ in range(5):
            password_reset.request_reset("reader@example.com", SITE)
        self.assertEqual(len(self.sent), password_reset.MAX_REQUESTS_PER_EMAIL_PER_HOUR)

    def test_without_email_settings_it_says_so(self):
        with patch.dict("os.environ", {"SMTP_HOST": ""}):
            self.assertFalse(password_reset.available())
            self.assertIn("error", password_reset.request_reset("reader@example.com", SITE))

    def test_a_failed_send_is_reported(self):
        self.mail.stop()
        with patch.object(password_reset, "_send_email", side_effect=OSError("smtp down")):
            result = password_reset.request_reset("reader@example.com", SITE)
        self.mail.start()
        self.assertIn("Couldn't send", result["error"])


class ResetPasswordTests(ResetTestCase):
    def test_the_link_sets_a_new_password_signs_in_and_signs_out_everywhere_else(self):
        old_session = auth.login({"email": "reader@example.com", "password": "old-password-1"})["token"]
        password_reset.request_reset("reader@example.com", SITE)
        result = password_reset.reset_password(self._token_from_email(), "new-password-2")

        self.assertEqual(result["email"], "reader@example.com")
        self.assertIsNotNone(auth.user_from_token(result["token"]))
        self.assertIsNone(auth.user_from_token(old_session))
        self.assertIn("error", auth.login({"email": "reader@example.com", "password": "old-password-1"}))
        self.assertIn("token", auth.login({"email": "reader@example.com", "password": "new-password-2"}))

    def test_a_link_works_once(self):
        password_reset.request_reset("reader@example.com", SITE)
        token = self._token_from_email()
        self.assertIn("token", password_reset.reset_password(token, "new-password-2"))
        self.assertEqual(password_reset.reset_password(token, "another-pass-3"), {"error": password_reset.BAD_LINK_MESSAGE})

    def test_an_expired_or_made_up_link_is_refused(self):
        password_reset.request_reset("reader@example.com", SITE)
        token = self._token_from_email()
        with patch("time.time", return_value=time.time() + password_reset.TOKEN_TTL + 1):
            self.assertIn("error", password_reset.reset_password(token, "new-password-2"))
        self.assertIn("error", password_reset.reset_password("made-up-token-1234567890", "new-password-2"))

    def test_short_passwords_are_refused(self):
        password_reset.request_reset("reader@example.com", SITE)
        self.assertIn("8 characters", password_reset.reset_password(self._token_from_email(), "short")["error"])

    def test_a_google_only_account_can_add_a_password(self):
        self._make_user("social@example.com", None)
        self.assertIn("Google or Facebook", auth.login({"email": "social@example.com", "password": "anything"})["error"])
        password_reset.request_reset("social@example.com", SITE)
        password_reset.reset_password(self._token_from_email(), "now-has-one-1")
        self.assertIn("token", auth.login({"email": "social@example.com", "password": "now-has-one-1"}))


class ResetApiTests(ResetTestCase):
    def setUp(self):
        super().setUp()
        import main
        from server import cache

        cache.clear_memory()
        self.client = TestClient(main.app)

    def test_endpoints_and_config_flag(self):
        self.assertTrue(self.client.get("/api/config").json()["password_reset"])
        r = self.client.post("/api/forgot-password", json={"email": "reader@example.com", "link": SITE})
        self.assertEqual(r.status_code, 200)
        r = self.client.post("/api/reset-password", json={"token": self._token_from_email(), "password": "new-password-2"})
        self.assertEqual(r.status_code, 200)
        self.assertIn("token", r.json())
        r = self.client.post("/api/reset-password", json={"token": "nope", "password": "new-password-2"})
        self.assertEqual(r.status_code, 400)
