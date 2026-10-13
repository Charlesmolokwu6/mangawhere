import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from server import db

ONION = "mangawhereexampleexampleexampleexampleexampleexampleexample.onion"


class OnionLocationTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self._tmp.close()
        db.DB_PATH = Path(self._tmp.name)
        import main

        self.main = main
        db.init_db()
        self.client = TestClient(main.app)

    def tearDown(self):
        Path(self._tmp.name).unlink(missing_ok=True)

    def test_pages_point_tor_browser_at_the_onion_copy(self):
        with patch.object(self.main, "ONION_ADDRESS", ONION):
            r = self.client.get("/index.html?x=1")
        self.assertEqual(r.headers["onion-location"], f"http://{ONION}/index.html?x=1")

    def test_api_answers_carry_no_header(self):
        with patch.object(self.main, "ONION_ADDRESS", ONION):
            r = self.client.get("/robots.txt")
        self.assertNotIn("onion-location", r.headers)

    def test_nothing_is_added_until_an_address_is_set(self):
        with patch.object(self.main, "ONION_ADDRESS", ""):
            r = self.client.get("/index.html")
        self.assertNotIn("onion-location", r.headers)


if __name__ == "__main__":
    unittest.main()
