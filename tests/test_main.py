import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from server import auth, db, media


class MainTestCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self._tmp.close()
        db.DB_PATH = Path(self._tmp.name)

        import main

        self.client = TestClient(main.app)

    def tearDown(self):
        Path(self._tmp.name).unlink(missing_ok=True)


class ProxyTests(MainTestCase):
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
