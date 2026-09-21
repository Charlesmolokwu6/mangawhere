import tempfile
import unittest
from pathlib import Path

from server import auth, captcha, db, mu, watch


class ServerTestCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self._tmp.close()
        db.DB_PATH = Path(self._tmp.name)
        db.init_db()

    def tearDown(self):
        Path(self._tmp.name).unlink(missing_ok=True)


class CaptchaTests(ServerTestCase):
    def test_verify_accepts_correct_answer_case_insensitively(self):
        c = captcha.generate()
        conn = db.get_connection()
        answer = conn.execute(
            "SELECT answer FROM captchas WHERE id = ?", (c["id"],)
        ).fetchone()["answer"]
        conn.close()

        self.assertTrue(captcha.verify(c["id"], answer.lower()))

    def test_verify_is_single_use(self):
        c = captcha.generate()
        conn = db.get_connection()
        answer = conn.execute(
            "SELECT answer FROM captchas WHERE id = ?", (c["id"],)
        ).fetchone()["answer"]
        conn.close()

        self.assertTrue(captcha.verify(c["id"], answer))
        self.assertFalse(captcha.verify(c["id"], answer))  # already consumed

    def test_verify_rejects_wrong_answer(self):
        c = captcha.generate()
        self.assertFalse(captcha.verify(c["id"], "WRONG"))


class AuthTests(ServerTestCase):
    def _register(self, email="reader@example.com", password="password123"):
        c = captcha.generate()
        conn = db.get_connection()
        answer = conn.execute(
            "SELECT answer FROM captchas WHERE id = ?", (c["id"],)
        ).fetchone()["answer"]
        conn.close()
        return auth.register(
            {
                "email": email,
                "password": password,
                "elapsed": 5,
                "captcha_id": c["id"],
                "captcha": answer,
            },
            captcha.verify,
        )

    def test_register_then_login(self):
        result = self._register()
        self.assertIn("token", result)
        self.assertEqual(result["email"], "reader@example.com")

        login_result = auth.login(
            {"email": "reader@example.com", "password": "password123"}
        )
        self.assertIn("token", login_result)

    def test_login_wrong_password_fails(self):
        self._register()
        result = auth.login({"email": "reader@example.com", "password": "nope"})
        self.assertIn("error", result)

    def test_duplicate_email_rejected(self):
        self._register()
        result = self._register()
        self.assertIn("error", result)

    def test_honeypot_blocks_registration(self):
        c = captcha.generate()
        result = auth.register(
            {
                "email": "bot@example.com",
                "password": "password123",
                "website": "http://spam.example",
                "elapsed": 5,
                "captcha_id": c["id"],
                "captcha": "whatever",
            },
            captcha.verify,
        )
        self.assertIn("error", result)

    def test_token_resolves_to_user(self):
        result = self._register()
        user = auth.user_from_token(result["token"])
        self.assertIsNotNone(user)
        self.assertEqual(user["email"], "reader@example.com")

    def test_logout_invalidates_token(self):
        result = self._register()
        auth.logout(result["token"])
        self.assertIsNone(auth.user_from_token(result["token"]))


class WatchTests(ServerTestCase):
    def test_upsert_list_mark_read_unwatch_roundtrip(self):
        user = auth.register(
            {"email": "a@example.com", "password": "password123", "elapsed": 5},
            lambda *_: True,
        )
        user_id = auth.user_from_token(user["token"])["id"]

        watch.upsert(
            user_id,
            {
                "endpoint": "https://push.example.com/1",
                "seen_chapter": 5,
                "title": {"key": "42", "name": "Test Manga", "kind": "manga"},
            },
        )
        titles = watch.list_for_user(user_id)
        self.assertEqual(len(titles), 1)
        self.assertEqual(titles[0]["key"], "42")
        self.assertEqual(watch.count_for_user(user_id), 1)

        watch.remove(user_id, "42")
        self.assertEqual(watch.count_for_user(user_id), 0)


class MuSimilarityTests(unittest.TestCase):
    def test_identical_titles_score_one(self):
        self.assertEqual(mu.similar("Solo Leveling", "Solo Leveling"), 1.0)

    def test_unrelated_titles_score_low(self):
        self.assertLess(mu.similar("Solo Leveling", "Tower of God"), 0.3)

    def test_close_variants_score_high(self):
        self.assertGreater(mu.similar("Solo Leveling", "solo leveling "), 0.9)


if __name__ == "__main__":
    unittest.main()
