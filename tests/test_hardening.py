import asyncio
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from fastapi.testclient import TestClient

from scrapers import core
from server import auth, db


class PublicSiteTests(unittest.TestCase):
    def test_the_source_is_chosen_by_the_site_name_only(self):
        self.assertEqual(core._detect_domain("https://comizy.io/x/chapter-1"), "comizy")
        self.assertEqual(core._detect_domain("http://127.0.0.1:8000/sw.js?x=toongod"), "unknown")
        self.assertEqual(core._detect_domain("http://evil.example/asura/x"), "unknown")

    def test_private_addresses_are_never_fetched(self):
        for url in ("http://127.0.0.1:8000/sw.js?x=toongod", "http://169.254.169.254/latest/meta-data",
                    "http://localhost/toongod", "http://10.0.0.5/comizy", "http://[::1]/x",
                    "file:///etc/passwd", "gopher://comizy.io/"):
            with self.assertRaises(core.NotAPublicSite, msg=url):
                asyncio.run(core.ensure_public_site(url))

    def test_a_public_name_pointing_inside_is_refused_too(self):
        loop_answer = [(2, 1, 6, "", ("127.0.0.1", 443))]
        with patch("asyncio.base_events.BaseEventLoop.getaddrinfo", AsyncMock(return_value=loop_answer)):
            with self.assertRaises(core.NotAPublicSite):
                asyncio.run(core.ensure_public_site("https://toongod.example/x"))
        with patch("asyncio.base_events.BaseEventLoop.getaddrinfo",
                   AsyncMock(return_value=[(2, 1, 6, "", ("104.21.3.4", 443))])):
            asyncio.run(core.ensure_public_site("https://comizy.io/x"))


class ApiTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self._tmp.close()
        db.DB_PATH = Path(self._tmp.name)
        import main

        self.main = main
        main._rate_counts.clear()
        db.init_db()
        self.client = TestClient(main.app)

    def tearDown(self):
        self.main._rate_counts.clear()
        Path(self._tmp.name).unlink(missing_ok=True)

    def test_scraping_an_internal_address_is_a_400_and_nothing_is_fetched(self):
        with patch.object(core, "scrape_toongod", AsyncMock()) as scrape:
            r = self.client.get("/api/scrape", params={"url": "http://127.0.0.1:8000/sw.js?x=toongod"})
            self.assertEqual(r.status_code, 400)
            r = self.client.get("/api/series", params={"url": "http://169.254.169.254/?toongod"})
            self.assertEqual(r.status_code, 400)
        scrape.assert_not_awaited()

    def test_malformed_json_is_a_400_not_a_500(self):
        for path in ("/api/register", "/api/login", "/api/oauth/google"):
            r = self.client.post(path, content=b"{not json", headers={"Content-Type": "application/json"})
            self.assertEqual(r.status_code, 400, path)
            r = self.client.post(path, json=[1, 2])
            self.assertEqual(r.status_code, 400, path)

    def test_tracking_a_title_without_a_key_says_so(self):
        token = auth.register({"email": "w@example.com", "password": "password123", "elapsed": 5},
                              lambda *_: True)["token"]
        h = {"Authorization": "Bearer " + token}
        self.assertEqual(self.client.post("/api/watch", json={"title": {}}, headers=h).status_code, 400)
        self.assertEqual(self.client.post("/api/watch", json={"title": "x"}, headers=h).status_code, 400)
        r = self.client.post("/api/watch", json={"title": {"key": "al-1", "name": "X"}}, headers=h)
        self.assertEqual(r.json(), {"ok": True})

    def test_logins_hash_off_the_event_loop(self):
        with patch("asyncio.to_thread", wraps=asyncio.to_thread) as to_thread:
            self.client.post("/api/login", json={"email": "nobody@example.com", "password": "x"})
        self.assertTrue(any(c.args and c.args[0] is auth.login for c in to_thread.call_args_list))

    def test_hammering_an_endpoint_is_limited_per_address(self):
        limit = self.main.RATE_LIMITS["account"][0]
        codes = [self.client.post("/api/login", json={"email": "a@b.c", "password": "x"}).status_code
                 for _ in range(limit + 3)]
        self.assertNotIn(429, codes[:limit])
        self.assertEqual(codes[-1], 429)
        r = self.client.post("/api/login", json={})
        self.assertIn("Retry-After", r.headers)
        self.assertEqual(r.headers["access-control-allow-origin"], "*")
        # Other groups, and reading, aren't affected.
        self.assertNotEqual(self.client.get("/api/comments", params={"key": "x"}).status_code, 429)


if __name__ == "__main__":
    unittest.main()
