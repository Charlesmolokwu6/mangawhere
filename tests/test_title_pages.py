import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from fastapi.testclient import TestClient

from server import cache, db, title_pages

SOLO = {
    "id": 105398,
    "title": {"english": "Solo Leveling", "romaji": "Na Honjaman Level Up", "native": "나 혼자만 레벨업"},
    "synonyms": ["Only I Level Up"],
    "description": "Hunters <b>fight</b> monsters.<br><br>(Source: Webtoon)",
    "coverImage": {"extraLarge": "https://img.example/solo.jpg", "large": "https://img.example/solo-s.jpg"},
    "genres": ["Action", "Fantasy"],
    "status": "FINISHED",
    "chapters": 201,
    "countryOfOrigin": "KR",
    "isAdult": False,
    "startDate": {"year": 2018},
    "recommendations": {"nodes": [
        {"mediaRecommendation": {"id": 119257, "title": {"english": "Omniscient Reader"},
                                 "coverImage": {"large": "https://img.example/orv.jpg"}, "isAdult": False}},
        {"mediaRecommendation": {"id": 1, "title": {"english": "Adult Title"},
                                 "coverImage": {"large": ""}, "isAdult": True}},
        {"mediaRecommendation": None},
    ]},
}


class TitlePageTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self._tmp.close()
        db.DB_PATH = Path(self._tmp.name)
        import main

        cache.clear_memory()
        self.client = TestClient(main.app)
        env = patch.dict(os.environ, {}, clear=False)
        env.start()
        os.environ.pop("PUBLIC_SITE_URL", None)
        self.addCleanup(env.stop)

    def tearDown(self):
        Path(self._tmp.name).unlink(missing_ok=True)

    def _anilist(self, media):
        return patch.object(title_pages, "_anilist", AsyncMock(return_value={"Media": media}))

    def test_a_title_page_has_the_name_cover_synopsis_and_a_way_into_the_app(self):
        with self._anilist(SOLO):
            r = self.client.get("/manga/105398/solo-leveling")
        self.assertEqual(r.status_code, 200)
        page = r.text
        self.assertIn("<title>Solo Leveling – Read Manhwa Online | MangaWhere</title>", page)
        self.assertIn('og:image" content="https://img.example/solo.jpg"', page)
        self.assertIn("Hunters fight monsters.", page)
        self.assertNotIn("Source: Webtoon", page)
        self.assertIn('href="/?m=al-105398"', page)
        self.assertIn('href="/manga/119257/omniscient-reader"', page)
        self.assertNotIn("Adult Title", page)
        self.assertIn("201 chapters", page)

    def test_the_wrong_or_missing_name_redirects_to_the_right_address(self):
        with self._anilist(SOLO):
            for path in ("/manga/105398", "/manga/105398/old-name"):
                r = self.client.get(path, follow_redirects=False)
                self.assertEqual(r.status_code, 301, path)
                self.assertEqual(r.headers["location"], "/manga/105398/solo-leveling")

    def test_an_unknown_title_is_not_found(self):
        with self._anilist(None):
            self.assertEqual(self.client.get("/manga/999/x").status_code, 404)

    def test_anilist_trouble_is_a_retryable_error_not_a_crash(self):
        with patch.object(title_pages, "_anilist", AsyncMock(side_effect=RuntimeError("down"))):
            r = self.client.get("/manga/105398/solo-leveling")
        self.assertEqual(r.status_code, 503)

    def test_titles_from_anilist_are_escaped(self):
        evil = dict(SOLO, title={"english": '<script>alert(1)</script>"'}, description="a<b>c</b>")
        with self._anilist(evil):
            r = self.client.get(title_pages.path_for(evil))
        self.assertNotIn("<script>alert", r.text)
        self.assertIn("&lt;script&gt;", r.text)

    def test_before_launch_pages_are_hidden_from_search_engines(self):
        with self._anilist(SOLO):
            page = self.client.get("/manga/105398/solo-leveling").text
        self.assertIn('<meta name="robots" content="noindex">', page)
        self.assertNotIn('rel="canonical"', page)
        self.assertIn("Disallow: /manga/", self.client.get("/robots.txt").text)
        self.assertEqual(self.client.get("/sitemap.xml").status_code, 404)
        self.assertEqual(self.client.get("/sitemaps/titles-1.xml").status_code, 404)

    def test_after_launch_pages_are_indexable_and_listed(self):
        os.environ["PUBLIC_SITE_URL"] = "https://mangawhere.example/"
        with self._anilist(SOLO):
            page = self.client.get("/manga/105398/solo-leveling").text
        self.assertNotIn("noindex", page)
        self.assertIn('<link rel="canonical" href="https://mangawhere.example/manga/105398/solo-leveling">', page)
        robots = self.client.get("/robots.txt").text
        self.assertIn("Sitemap: https://mangawhere.example/sitemap.xml", robots)
        self.assertNotIn("Disallow", robots)
        index = self.client.get("/sitemap.xml").text
        self.assertEqual(index.count("<sitemap>"), title_pages.SITEMAP_FILES)
        listing = {"Page": {"media": [{"id": 105398, "title": SOLO["title"]}]}}
        with patch.object(title_pages, "_anilist", AsyncMock(return_value=listing)):
            part = self.client.get("/sitemaps/titles-1.xml").text
        self.assertIn("<loc>https://mangawhere.example/manga/105398/solo-leveling</loc>", part)
        self.assertEqual(self.client.get("/sitemaps/titles-99.xml").status_code, 404)

    def test_adult_titles_stay_out_of_search_even_after_launch(self):
        os.environ["PUBLIC_SITE_URL"] = "https://mangawhere.example"
        with self._anilist(dict(SOLO, isAdult=True)):
            page = self.client.get("/manga/105398/solo-leveling").text
        self.assertIn('content="noindex"', page)

    def test_slugs(self):
        self.assertEqual(title_pages.slugify("Re:Zero − Starting Life!"), "re-zero-starting-life")
        self.assertEqual(title_pages.slugify("나 혼자만 레벨업"), "title")


if __name__ == "__main__":
    unittest.main()
