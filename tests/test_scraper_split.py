import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

from fastapi.testclient import TestClient

from server import db


class SplitTestCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self._tmp.close()
        db.DB_PATH = Path(self._tmp.name)
        db.init_db()

        import main

        self.main = main
        self.client = TestClient(main.app)

    def tearDown(self):
        Path(self._tmp.name).unlink(missing_ok=True)


class MainServerForwardsTests(SplitTestCase):
    def _upstream(self):
        r = MagicMock()
        r.status_code = 200
        r.content = b'{"source": "x"}'
        r.headers = {"content-type": "application/json", "cache-control": "public, max-age=300",
                     "set-cookie": "nope=1"}
        return r

    def test_scrape_routes_go_to_the_scraper_node(self):
        with patch.object(self.main, "SCRAPER_URL", "https://node.example"), \
             patch.object(self.main, "SCRAPER_TOKEN", "s3cret"), \
             patch("httpx.AsyncClient.request", new=AsyncMock(return_value=self._upstream())) as request, \
             patch.object(self.main, "find_title") as find_title:
            r = self.client.get("/api/find", params={"title": "Solo Leveling", "alt": "Na Honjaman"})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json(), {"source": "x"})
        self.assertEqual(r.headers["cache-control"], "public, max-age=300")
        self.assertEqual(r.headers["access-control-allow-origin"], "*")
        self.assertNotIn("set-cookie", r.headers)
        find_title.assert_not_called()
        method, url = request.call_args.args
        self.assertEqual(method, "GET")
        self.assertEqual(url, "https://node.example/api/find?title=Solo%20Leveling&alt=Na%20Honjaman")
        self.assertEqual(request.call_args.kwargs["headers"]["X-Scraper-Token"], "s3cret")

    def test_post_bodies_are_forwarded(self):
        with patch.object(self.main, "SCRAPER_URL", "https://node.example"), \
             patch("httpx.AsyncClient.request", new=AsyncMock(return_value=self._upstream())) as request:
            self.client.post("/api/health/status", json={"titles": []})
        self.assertEqual(request.call_args.args[1], "https://node.example/api/health/status")
        self.assertEqual(request.call_args.kwargs["content"], b'{"titles": []}')

    def test_a_node_that_is_down_is_a_503_not_a_crash(self):
        with patch.object(self.main, "SCRAPER_URL", "https://node.example"), \
             patch("httpx.AsyncClient.request", new=AsyncMock(side_effect=OSError("refused"))):
            r = self.client.get("/api/scrape", params={"url": "https://a.example/ch-1"})
        self.assertEqual(r.status_code, 503)
        self.assertEqual(r.headers["access-control-allow-origin"], "*")

    def test_account_routes_stay_on_the_main_server(self):
        with patch.object(self.main, "SCRAPER_URL", "https://node.example"), \
             patch("httpx.AsyncClient.request", new=AsyncMock()) as request:
            r = self.client.get("/api/config")
        self.assertEqual(r.status_code, 200)
        request.assert_not_called()


class ScraperNodeTests(SplitTestCase):
    def test_refuses_requests_without_the_token(self):
        with patch.object(self.main, "SCRAPER_ONLY", True), \
             patch.object(self.main, "SCRAPER_TOKEN", "s3cret"):
            r = self.client.get("/api/health")
            wrong = self.client.get("/api/health", headers={"X-Scraper-Token": "guess"})
        self.assertEqual(r.status_code, 404)
        self.assertEqual(wrong.status_code, 404)

    def test_refuses_everything_when_no_token_is_set(self):
        with patch.object(self.main, "SCRAPER_ONLY", True), \
             patch.object(self.main, "SCRAPER_TOKEN", ""):
            r = self.client.get("/api/health", headers={"X-Scraper-Token": ""})
        self.assertEqual(r.status_code, 404)

    def test_serves_scraping_but_not_accounts_to_the_main_server(self):
        token = {"X-Scraper-Token": "s3cret"}
        with patch.object(self.main, "SCRAPER_ONLY", True), \
             patch.object(self.main, "SCRAPER_TOKEN", "s3cret"):
            health = self.client.get("/api/health", headers=token)
            login = self.client.post("/api/login", json={}, headers=token)
            page = self.client.get("/", headers=token)
        self.assertEqual(health.status_code, 200)
        self.assertEqual(login.status_code, 404)
        self.assertEqual(page.status_code, 404)

    def test_main_servers_requests_are_not_rate_limited_as_one_address(self):
        token = {"X-Scraper-Token": "s3cret"}
        self.main._rate_counts.clear()
        with patch.object(self.main, "SCRAPER_ONLY", True), \
             patch.object(self.main, "SCRAPER_TOKEN", "s3cret"), \
             patch.dict(self.main.RATE_LIMITS, {"scrape": (2, self.main.RATE_LIMITS["scrape"][1])}):
            codes = [self.client.get("/api/find", params={"title": ""}, headers=token).status_code
                     for _ in range(4)]
        self.main._rate_counts.clear()
        self.assertNotIn(429, codes)


class FrontendConfigTests(SplitTestCase):
    def test_serves_the_file_by_default(self):
        r = self.client.get("/config.js")
        self.assertEqual(r.status_code, 200)
        self.assertIn("MANGAWHERE_CONFIG", r.text)
        self.assertEqual(r.headers["cache-control"], "no-cache")

    def test_environment_variable_repoints_the_page(self):
        with patch.object(self.main, "PUBLIC_BACKEND_URL", "https://api.new-domain.example"):
            r = self.client.get("/config.js")
        self.assertIn('"backend": "https://api.new-domain.example"', r.text)


if __name__ == "__main__":
    unittest.main()
