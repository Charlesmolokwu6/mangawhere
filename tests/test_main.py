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

    def test_mangadex_search_is_allowed(self):
        # The frontend's last search fallback goes through here.
        with patch("httpx.AsyncClient.request", return_value=self._upstream("application/json")):
            r = self.client.get("/", params={"url": "https://api.mangadex.org/manga?title=Hand%20Jumper"})
        self.assertEqual(r.status_code, 200)

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


class PublicFilesTests(MainTestCase):
    def test_frontend_files_are_served(self):
        for path in ("/", "/index.html", "/sw.js"):
            self.assertEqual(self.client.get(path).status_code, 200, path)

    def test_project_files_are_not_served(self):
        # The database replica, source and git metadata all sit next to
        # index.html; none of them may be downloadable.
        for path in (
            "/data/mangawhere-replica.db",
            "/data/mangawhere.db",
            "/main.py",
            "/server/db.py",
            "/requirements.txt",
            "/.git/HEAD",
            "/render.yaml",
        ):
            self.assertEqual(self.client.get(path).status_code, 404, path)


class FindApiTests(MainTestCase):
    FOUND = {"domain": "mangaread", "chapters": [{"number": 1.0, "url": "u1", "title": ""}], "alternates": []}

    def test_other_names_are_tried_when_the_title_is_not_found(self):
        searched = []

        async def fake_find(name, **kwargs):
            searched.append(name)
            return self.FOUND if name == "Backstabbed in a Backwater Dungeon" else None

        with patch("main.find_best_source", fake_find):
            response = self.client.get("/api/find", params=[
                ("title", "Chou Nankan Dungeon de 10-man nen Shugyou Shita"),
                ("alt", "超難関ダンジョンで10万年修行した結果"),  # no Latin letters: skipped
                ("alt", "Backstabbed in a Backwater Dungeon"),
                ("alt", "Never searched once one is found"),
            ])
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["domain"], "mangaread")
        self.assertEqual(searched, [
            "Chou Nankan Dungeon de 10-man nen Shugyou Shita",
            "Backstabbed in a Backwater Dungeon",
        ])

    def test_not_found_under_any_name_is_a_404(self):
        async def fake_find(name, **kwargs):
            return None

        with patch("main.find_best_source", fake_find):
            response = self.client.get("/api/find", params=[("title", "Nope"), ("alt", "Still Nope")])
        self.assertEqual(response.status_code, 404)

    def test_names_are_capped(self):
        import main

        names = main._find_names("Main", ["A1", "a1", "B2", "C3", "D4", "E5"])
        self.assertEqual(names, ["Main", "A1", "B2", "C3"])


class ImageCheckTests(unittest.IsolatedAsyncioTestCase):
    async def test_mangadex_images_are_not_fetched_from_the_server(self):
        # MangaDex's image servers 404 every request from Render but serve
        # readers' browsers; fetching from here hid titles that read fine.
        import main

        with patch.object(main, "_get_proxy_client", side_effect=AssertionError("fetched")):
            self.assertTrue(await main.image_loads(
                "https://cmdxd98sb0x3yprd.mangadex.network/data/abc/1-x.png"))
            self.assertFalse(await main.image_loads("ftp://example.com/1.png"))

    async def test_other_images_are_still_fetched(self):
        import main

        with patch.object(main, "_get_proxy_client", side_effect=RuntimeError("down")):
            self.assertFalse(await main.image_loads("https://cdn.example.com/1.png"))
            # A look-alike host doesn't get the MangaDex pass.
            self.assertFalse(await main.image_loads("https://evilmangadex.network/1.png"))


class PhoneNarrationTests(MainTestCase):
    WEBP = b"RIFF\x00\x00\x00\x00WEBPVP8 data"

    def test_page_comes_from_the_scraped_chapter_only(self):
        import main

        chapter = {"images": ["https://cdn.example.com/1.webp", "https://cdn.example.com/2.webp"]}
        with patch.object(main, "scrape_cached", AsyncMock(return_value=chapter)), \
             patch("server.storyteller.fetch_image", AsyncMock(return_value=self.WEBP)) as fetch:
            r = self.client.get("/api/narrate/page", params={"chapter_url": "https://comizy.io/x/chapter-1", "n": 1})
            self.assertEqual(r.status_code, 200)
            self.assertEqual(r.headers["content-type"], "image/webp")
            self.assertIn("max-age", r.headers["cache-control"])
            self.assertEqual(fetch.call_args.args[1], "https://cdn.example.com/2.webp")
            self.assertEqual(self.client.get("/api/narrate/page", params={
                "chapter_url": "https://comizy.io/x/chapter-1", "n": 2}).status_code, 404)
            self.assertEqual(self.client.get("/api/narrate/page", params={
                "chapter_url": "https://comizy.io/x/chapter-1", "n": -1}).status_code, 422)

    def test_page_errors(self):
        import main

        with patch.object(main, "scrape_cached", AsyncMock(return_value={"images": []})):
            self.assertEqual(self.client.get("/api/narrate/page", params={
                "chapter_url": "https://unknown.example/c/1", "n": 0}).status_code, 404)
        with patch.object(main, "scrape_cached", AsyncMock(return_value={"images": ["https://a/1.png"]})), \
             patch("server.storyteller.fetch_image", AsyncMock(return_value=None)):
            self.assertEqual(self.client.get("/api/narrate/page", params={
                "chapter_url": "https://comizy.io/x/chapter-1", "n": 0}).status_code, 502)

    def test_lines_become_speakable_bubbles(self):
        def line(y, text, x=100):
            return [[[x, y], [x + 300, y], [x + 300, y + 30], [x, y + 30]], text, 0.95]

        r = self.client.post("/api/narrate/lines", json={"lines": [
            line(100, "MY GOD, THISGUY CAN'T STOP"),
            line(135, "GETTING HIMSELF HURT."),
            line(600, "WHAT DO YOU WANT?"),
            line(900, "KRR", x=20),
        ]})
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertEqual(body["lines"], [
            {"text": "My god, this guy can't stop getting himself hurt.", "delivery": "says", "y": 100},
            {"text": "What do you want?", "delivery": "asks", "y": 600},
        ])
        self.assertTrue(body["effects"])

    def test_manga_pages_are_read_right_to_left(self):
        def bubble(x, y, text):
            return [[[x, y], [x + 200, y], [x + 200, y + 30], [x, y + 30]], text, 0.95]

        # A manga page: two bubbles side by side in the top row, a third below.
        page = [bubble(50, 100, "THEN WHO ARE YOU?"), bubble(500, 90, "I CAME HERE ALONE."),
                bubble(60, 400, "WE SHOULD LEAVE NOW."), bubble(520, 410, "WAIT FOR THE OTHERS.")]
        said = lambda body: [l["text"] for l in body["lines"]]
        rtl = self.client.post("/api/narrate/lines", json={"lines": page, "order": "rtl"}).json()
        self.assertEqual(said(rtl), ["I came here alone.", "Then who are you?", "Wait for the others.", "We should leave now."])
        # Anything else (manhwa, manhua) stays top to bottom.
        plain = self.client.post("/api/narrate/lines", json={"lines": page}).json()
        self.assertEqual(said(plain), ["I came here alone.", "Then who are you?", "We should leave now.", "Wait for the others."])

    def test_bad_lines_are_rejected(self):
        for body in ({"lines": "x"}, {"lines": [["box", "t", 1]]}, {"lines": [[[[0, 0]] * 4, "x" * 301, 1]]},
                     {"lines": [[[[0, 0]] * 4, "ok", 1]] * 801}, ["not", "a", "dict"]):
            self.assertEqual(self.client.post("/api/narrate/lines", json=body).status_code, 400, body)

    def test_worker_and_config(self):
        r = self.client.get("/ocr-worker.js")
        self.assertEqual(r.status_code, 200)
        self.assertIn("javascript", r.headers["content-type"])
        self.assertTrue(self.client.get("/api/config").json()["phone_narration"])
