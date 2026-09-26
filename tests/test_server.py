import tempfile
import unittest
from pathlib import Path

from server import auth, captcha, comments, db, media, mu, push, watch
from server.webtoon import _rss_url


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

    def test_skip_captcha_bypasses_custom_captcha_when_turnstile_passed(self):
        # skip_captcha=True (Turnstile configured) means the custom captcha
        # is never consulted -- a deliberately wrong answer still succeeds
        # as long as turnstile_ok is True.
        result = auth.register(
            {
                "email": "turnstile-ok@example.com",
                "password": "password123",
                "elapsed": 5,
                "captcha_id": "bogus",
                "captcha": "wrong",
            },
            captcha.verify,
            skip_captcha=True,
            turnstile_ok=True,
        )
        self.assertIn("token", result)

    def test_skip_captcha_rejects_registration_when_turnstile_failed(self):
        result = auth.register(
            {
                "email": "turnstile-fail@example.com",
                "password": "password123",
                "elapsed": 5,
            },
            captcha.verify,
            skip_captcha=True,
            turnstile_ok=False,
        )
        self.assertIn("error", result)

    def test_honeypot_still_blocks_registration_when_turnstile_configured(self):
        result = auth.register(
            {
                "email": "bot2@example.com",
                "password": "password123",
                "website": "http://spam.example",
                "elapsed": 5,
            },
            captcha.verify,
            skip_captcha=True,
            turnstile_ok=True,
        )
        self.assertIn("error", result)


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


class PushTests(ServerTestCase):
    def test_save_subscription_without_user_is_unattached(self):
        push.save_subscription("https://push.example.com/x", "p256dh", "authkey")
        self.assertEqual(push.subscriptions_for_user(1), [])

    def test_attach_subscription_links_it_to_a_user(self):
        push.save_subscription("https://push.example.com/x", "p256dh", "authkey")
        push.attach_subscription("https://push.example.com/x", 7)
        subs = push.subscriptions_for_user(7)
        self.assertEqual(len(subs), 1)
        self.assertEqual(subs[0]["endpoint"], "https://push.example.com/x")

    def test_resubscribing_does_not_clear_an_existing_attachment(self):
        push.save_subscription("https://push.example.com/x", "p256dh", "authkey")
        push.attach_subscription("https://push.example.com/x", 7)
        # Same device re-subscribes (e.g. key rotation) without a user_id
        # in the call — must not silently detach it from the account.
        push.save_subscription("https://push.example.com/x", "p256dh2", "authkey2")
        subs = push.subscriptions_for_user(7)
        self.assertEqual(len(subs), 1)
        self.assertEqual(subs[0]["p256dh"], "p256dh2")

    def test_attach_subscription_with_no_endpoint_is_a_noop(self):
        push.attach_subscription(None, 7)  # must not raise
        self.assertEqual(push.subscriptions_for_user(7), [])


class CommentsTests(ServerTestCase):
    def test_add_then_list_roundtrip(self):
        comments.add(1, "Reader", "https://cdn.example/reader.jpg", "al-123", "12", "Great chapter!")
        rows = comments.list_for("al-123", "12")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["name"], "Reader")
        self.assertEqual(rows[0]["body"], "Great chapter!")
        self.assertEqual(rows[0]["avatar_url"], "https://cdn.example/reader.jpg")

    def test_add_with_no_avatar_is_null(self):
        comments.add(1, "Reader", None, "al-1", "1", "hi")
        rows = comments.list_for("al-1", "1")
        self.assertIsNone(rows[0]["avatar_url"])

    def test_newest_comment_listed_first(self):
        comments.add(1, "A", None, "al-1", "1", "first")
        comments.add(2, "B", None, "al-1", "1", "second")
        rows = comments.list_for("al-1", "1")
        self.assertEqual([r["body"] for r in rows], ["second", "first"])

    def test_comments_are_scoped_to_their_own_chapter(self):
        comments.add(1, "A", None, "al-1", "1", "chapter one comment")
        comments.add(1, "A", None, "al-1", "2", "chapter two comment")
        self.assertEqual(len(comments.list_for("al-1", "1")), 1)
        self.assertEqual(len(comments.list_for("al-1", "2")), 1)

    def test_add_rejects_blank_body(self):
        with self.assertRaises(ValueError):
            comments.add(1, "A", None, "al-1", "1", "   ")

    def test_list_for_missing_chapter_is_empty(self):
        self.assertEqual(comments.list_for("al-1", ""), [])


class MediaTests(unittest.TestCase):
    def setUp(self):
        self._cloud = media.CLOUDINARY_CLOUD_NAME
        self._key = media.CLOUDINARY_API_KEY
        self._secret = media.CLOUDINARY_API_SECRET

    def tearDown(self):
        media.CLOUDINARY_CLOUD_NAME = self._cloud
        media.CLOUDINARY_API_KEY = self._key
        media.CLOUDINARY_API_SECRET = self._secret

    def test_not_configured_without_all_three_env_vars(self):
        media.CLOUDINARY_CLOUD_NAME = ""
        media.CLOUDINARY_API_KEY = "x"
        media.CLOUDINARY_API_SECRET = "y"
        self.assertFalse(media.configured())

    def test_configured_with_all_three_env_vars(self):
        media.CLOUDINARY_CLOUD_NAME = "demo"
        media.CLOUDINARY_API_KEY = "x"
        media.CLOUDINARY_API_SECRET = "y"
        self.assertTrue(media.configured())

    def test_validate_avatar_rejects_disallowed_content_type(self):
        with self.assertRaises(ValueError):
            media.validate_avatar("application/pdf", b"not-an-image")

    def test_validate_avatar_rejects_oversized_file(self):
        with self.assertRaises(ValueError):
            media.validate_avatar("image/png", b"x" * (media.MAX_AVATAR_BYTES + 1))

    def test_validate_avatar_accepts_a_small_allowed_image(self):
        media.validate_avatar("image/gif", b"GIF89a")  # must not raise

    def test_signature_is_deterministic_and_secret_dependent(self):
        media.CLOUDINARY_API_SECRET = "shh"
        params = {"timestamp": "1000", "public_id": "avatars/user_1", "overwrite": "true"}
        sig1 = media._signature(params)
        sig2 = media._signature(params)
        self.assertEqual(sig1, sig2)
        media.CLOUDINARY_API_SECRET = "different"
        self.assertNotEqual(sig1, media._signature(params))


class WebtoonRssUrlTests(unittest.TestCase):
    def test_builds_rss_url_from_series_url(self):
        url = _rss_url(
            "https://www.webtoons.com/en/fantasy/tower-of-god/list?title_no=95"
        )
        self.assertEqual(
            url, "https://www.webtoons.com/en/fantasy/tower-of-god/rss?title_no=95"
        )

    def test_returns_none_for_non_webtoons_url(self):
        self.assertIsNone(_rss_url("https://example.com/series?title_no=95"))

    def test_returns_none_without_title_no(self):
        self.assertIsNone(
            _rss_url("https://www.webtoons.com/en/fantasy/tower-of-god/list")
        )


class MuSimilarityTests(unittest.TestCase):
    def test_identical_titles_score_one(self):
        self.assertEqual(mu.similar("Solo Leveling", "Solo Leveling"), 1.0)

    def test_unrelated_titles_score_low(self):
        self.assertLess(mu.similar("Solo Leveling", "Tower of God"), 0.3)

    def test_close_variants_score_high(self):
        self.assertGreater(mu.similar("Solo Leveling", "solo leveling "), 0.9)


if __name__ == "__main__":
    unittest.main()
