import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from fastapi.testclient import TestClient

from server import auth, db, media


class MainTestCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self._tmp.close()
        db.DB_PATH = Path(self._tmp.name)

        import main
        from server import cache

        cache.clear_memory()

        self.client = TestClient(main.app)

    def tearDown(self):
        Path(self._tmp.name).unlink(missing_ok=True)


class ProxyTests(MainTestCase):
    def _upstream(self, content_type, status=200):
        response = AsyncMock()
        response.status_code = status
        response.content = b"\xff\xd8image-bytes"
        response.headers = {"content-type": content_type}
        return response

    def test_proxied_images_are_cacheable_and_sent_with_comizys_referer(self):
        with patch("httpx.AsyncClient.request", return_value=self._upstream("image/webp")) as request:
            r = self.client.get("/", params={"url": "https://x3.cmzcdn.org/e/abc.webp"})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.headers["cache-control"], "public, max-age=604800, immutable")
        self.assertEqual(request.call_args.kwargs["headers"]["Referer"], "https://comizy.io/")

    def test_failed_or_non_image_responses_are_not_marked_cacheable(self):
        with patch("httpx.AsyncClient.request", return_value=self._upstream("image/webp", 404)):
            r = self.client.get("/", params={"url": "https://x3.cmzcdn.org/e/missing.webp"})
        self.assertNotIn("cache-control", r.headers)
        with patch("httpx.AsyncClient.request", return_value=self._upstream("application/json")):
            r = self.client.get("/", params={"url": "https://api.jikan.moe/v4/top/manga"})
        self.assertNotIn("cache-control", r.headers)

    def test_the_proxy_reuses_one_client_across_requests(self):
        import main

        self.assertIs(main._get_proxy_client(), main._get_proxy_client())

    def test_root_without_url_serves_index(self):
        r = self.client.get("/")
        self.assertEqual(r.status_code, 200)
        self.assertIn("text/html", r.headers["content-type"])

    def test_disallowed_host_is_rejected(self):
        r = self.client.get("/", params={"url": "https://evil.example.com/steal"})
        self.assertEqual(r.status_code, 403)
        self.assertEqual(r.json()["host"], "evil.example.com")

    def test_malformed_url_is_rejected(self):
        r = self.client.get("/", params={"url": "not-a-url"})
        self.assertEqual(r.status_code, 400)

    def test_post_without_url_is_not_found(self):
        r = self.client.post("/", json={})
        self.assertEqual(r.status_code, 404)

    def test_post_to_disallowed_host_is_rejected(self):
        r = self.client.post(
            "/", params={"url": "https://evil.example.com/steal"}, json={}
        )
        self.assertEqual(r.status_code, 403)


class RegisterApiTests(MainTestCase):
    def test_register_via_custom_captcha_when_turnstile_not_configured(self):
        db.init_db()
        c = self.client.get("/api/captcha").json()
        conn = db.get_connection()
        answer = conn.execute(
            "SELECT answer FROM captchas WHERE id = ?", (c["id"],)
        ).fetchone()["answer"]
        conn.close()

        r = self.client.post(
            "/api/register",
            json={
                "email": "reader@example.com",
                "password": "password123",
                "elapsed": 5,
                "captcha_id": c["id"],
                "captcha": answer,
            },
        )
        self.assertEqual(r.status_code, 200)
        self.assertIn("token", r.json())

    def test_register_blocked_when_turnstile_configured_and_fails(self):
        import main

        db.init_db()
        with patch.object(main.turnstile, "is_configured", return_value=True), \
             patch.object(main.turnstile, "verify", return_value=False):
            r = self.client.post(
                "/api/register",
                json={"email": "blocked@example.com", "password": "password123", "elapsed": 5},
            )
        self.assertEqual(r.status_code, 200)
        self.assertIn("error", r.json())

    def test_register_succeeds_when_turnstile_configured_and_passes(self):
        import main

        db.init_db()
        with patch.object(main.turnstile, "is_configured", return_value=True), \
             patch.object(main.turnstile, "verify", return_value=True):
            r = self.client.post(
                "/api/register",
                json={
                    "email": "passed@example.com",
                    "password": "password123",
                    "elapsed": 5,
                    "turnstile_token": "some-real-looking-token",
                },
            )
        self.assertEqual(r.status_code, 200)
        self.assertIn("token", r.json())


class OAuthApiTests(MainTestCase):
    def test_google_returns_503_when_not_configured(self):
        import main

        with patch.object(main.oauth, "google_configured", return_value=False):
            r = self.client.post("/api/oauth/google", json={"id_token": "whatever"})
        self.assertEqual(r.status_code, 503)

    def test_google_returns_error_on_failed_verification(self):
        import main

        db.init_db()
        with patch.object(main.oauth, "google_configured", return_value=True), \
             patch.object(main.oauth, "verify_google", return_value=None):
            r = self.client.post("/api/oauth/google", json={"id_token": "bad-token"})
        self.assertEqual(r.status_code, 200)
        self.assertIn("error", r.json())

    def test_google_creates_a_session_on_successful_verification(self):
        import main

        db.init_db()
        profile = {
            "subject": "google-sub-1",
            "email": "reader@example.com",
            "name": "Reader",
            "picture": "",
        }
        with patch.object(main.oauth, "google_configured", return_value=True), \
             patch.object(main.oauth, "verify_google", return_value=profile):
            r = self.client.post("/api/oauth/google", json={"id_token": "good-token"})
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertIn("token", body)
        self.assertEqual(body["email"], "reader@example.com")

    def test_facebook_returns_503_when_not_configured(self):
        import main

        with patch.object(main.oauth, "facebook_configured", return_value=False):
            r = self.client.post("/api/oauth/facebook", json={"access_token": "whatever"})
        self.assertEqual(r.status_code, 503)

    def test_facebook_creates_a_session_on_successful_verification(self):
        import main

        db.init_db()
        profile = {
            "subject": "fb-sub-1",
            "email": "fbreader@example.com",
            "name": "FB Reader",
            "picture": "",
        }
        with patch.object(main.oauth, "facebook_configured", return_value=True), \
             patch.object(main.oauth, "verify_facebook", return_value=profile):
            r = self.client.post("/api/oauth/facebook", json={"access_token": "good-token"})
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertIn("token", body)
        self.assertEqual(body["email"], "fbreader@example.com")


class VideoLookupApiTests(MainTestCase):
    def test_disallowed_host_is_rejected(self):
        r = self.client.get(
            "/api/video-lookup", params={"url": "https://evil.example.com/video"}
        )
        self.assertEqual(r.status_code, 400)

    def test_malformed_url_is_rejected(self):
        r = self.client.get("/api/video-lookup", params={"url": "not-a-url"})
        self.assertEqual(r.status_code, 400)

    def test_youtube_url_is_looked_up(self):
        import main

        payload = {"title": "Someone Stop Her! ep 12", "description": "Sauce: Someone Stop Her!", "transcript": ""}
        with patch.object(main, "lookup_video", return_value=payload):
            r = self.client.get(
                "/api/video-lookup",
                params={"url": "https://www.youtube.com/watch?v=abc123"},
            )
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json(), payload)

    def test_lookup_failure_returns_502(self):
        import main

        with patch.object(main, "lookup_video", side_effect=RuntimeError("boom")):
            r = self.client.get(
                "/api/video-lookup",
                params={"url": "https://www.youtube.com/watch?v=abc123"},
            )
        self.assertEqual(r.status_code, 502)


class CommentsApiTests(MainTestCase):
    def _token(self):
        db.init_db()
        result = auth.register(
            {"email": "reader@example.com", "password": "password123", "elapsed": 5},
            lambda *_: True,
        )
        return result["token"]

    def test_posting_a_comment_requires_sign_in(self):
        r = self.client.post(
            "/api/comments", json={"title": "al-1", "chapter": "1", "body": "hi"}
        )
        self.assertEqual(r.status_code, 401)

    def test_post_then_list_roundtrip(self):
        token = self._token()
        r = self.client.post(
            "/api/comments",
            json={"title": "al-1", "chapter": "1", "body": "Loved this chapter!"},
            headers={"Authorization": "Bearer " + token},
        )
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["body"], "Loved this chapter!")

        listed = self.client.get(
            "/api/comments", params={"title": "al-1", "chapter": "1"}
        )
        self.assertEqual(listed.status_code, 200)
        rows = listed.json()["comments"]
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["body"], "Loved this chapter!")

    def test_list_requires_title_and_chapter(self):
        r = self.client.get("/api/comments", params={"title": "al-1"})
        self.assertEqual(r.status_code, 422)  # missing required query param

    def test_post_rejects_blank_body(self):
        token = self._token()
        r = self.client.post(
            "/api/comments",
            json={"title": "al-1", "chapter": "1", "body": "   "},
            headers={"Authorization": "Bearer " + token},
        )
        self.assertEqual(r.status_code, 400)


class AvatarApiTests(MainTestCase):
    def setUp(self):
        super().setUp()
        # Force "not configured" regardless of what's in the environment
        # this test happens to run in.
        self._cloud = media.CLOUDINARY_CLOUD_NAME
        self._key = media.CLOUDINARY_API_KEY
        self._secret = media.CLOUDINARY_API_SECRET
        media.CLOUDINARY_CLOUD_NAME = ""
        media.CLOUDINARY_API_KEY = ""
        media.CLOUDINARY_API_SECRET = ""

    def tearDown(self):
        media.CLOUDINARY_CLOUD_NAME = self._cloud
        media.CLOUDINARY_API_KEY = self._key
        media.CLOUDINARY_API_SECRET = self._secret
        super().tearDown()

    def _token(self):
        db.init_db()
        result = auth.register(
            {"email": "reader@example.com", "password": "password123", "elapsed": 5},
            lambda *_: True,
        )
        return result["token"]

    def test_uploading_requires_sign_in(self):
        r = self.client.post(
            "/api/avatar", files={"file": ("a.png", b"\x89PNG", "image/png")}
        )
        self.assertEqual(r.status_code, 401)

    def test_disallowed_content_type_is_rejected(self):
        token = self._token()
        r = self.client.post(
            "/api/avatar",
            files={"file": ("a.pdf", b"not-an-image", "application/pdf")},
            headers={"Authorization": "Bearer " + token},
        )
        self.assertEqual(r.status_code, 400)

    def test_upload_without_cloudinary_configured_is_a_clear_503(self):
        token = self._token()
        r = self.client.post(
            "/api/avatar",
            files={"file": ("a.png", b"\x89PNG", "image/png")},
            headers={"Authorization": "Bearer " + token},
        )
        self.assertEqual(r.status_code, 503)

    def test_me_reports_avatar_url(self):
        token = self._token()
        r = self.client.get("/api/me", headers={"Authorization": "Bearer " + token})
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertTrue(body["signed_in"])
        self.assertIsNone(body["avatar_url"])
        self.assertEqual(body["name"], "reader")


if __name__ == "__main__":
    unittest.main()
