import tempfile
import unittest
from pathlib import Path

from fastapi.testclient import TestClient

from server import db


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


if __name__ == "__main__":
    unittest.main()
