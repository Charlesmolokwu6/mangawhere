import asyncio
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from fastapi.testclient import TestClient

from server import cache, db


class CacheTestCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self._tmp.close()
        self._old_path = db.DB_PATH
        db.DB_PATH = Path(self._tmp.name)
        db.init_db()
        cache.clear_memory()

    def tearDown(self):
        db.DB_PATH = self._old_path
        Path(self._tmp.name).unlink(missing_ok=True)
        cache.clear_memory()


class CachedTests(CacheTestCase):
    def test_a_value_is_produced_once_then_reused(self):
        produce = AsyncMock(return_value={"chapters": [1]})

        async def run():
            first = await cache.cached("series", "https://x/a", 60, produce)
            second = await cache.cached("series", "https://x/a", 60, produce)
            return first, second

        self.assertEqual(asyncio.run(run()), ({"chapters": [1]}, {"chapters": [1]}))
        produce.assert_awaited_once()

    def test_concurrent_requests_for_one_key_share_a_single_fetch(self):
        calls = 0

        async def slow():
            nonlocal calls
            calls += 1
            await asyncio.sleep(0.05)
            return "result"

        async def run():
            return await asyncio.gather(*(cache.cached("find", "Gosu", 60, slow) for _ in range(50)))

        self.assertEqual(asyncio.run(run()), ["result"] * 50)
        self.assertEqual(calls, 1)

    def test_it_survives_a_restart_via_the_database(self):
        asyncio.run(cache.cached("find", "Gosu", 60, AsyncMock(return_value={"domain": "mangafreak"})))
        cache.clear_memory()  # what a restart loses
        produce = AsyncMock(return_value="fresh")
        self.assertEqual(asyncio.run(cache.cached("find", "Gosu", 60, produce)), {"domain": "mangafreak"})
        produce.assert_not_awaited()

    def test_keys_ignore_case_and_surrounding_spaces(self):
        asyncio.run(cache.cached("find", "Solo Leveling", 60, AsyncMock(return_value="hit")))
        produce = AsyncMock(return_value="miss")
        self.assertEqual(asyncio.run(cache.cached("find", "  solo leveling ", 60, produce)), "hit")

    def test_empty_results_are_not_remembered_unless_asked(self):
        produce = AsyncMock(return_value=[])
        asyncio.run(cache.cached("series", "https://x/b", 60, produce))
        asyncio.run(cache.cached("series", "https://x/b", 60, produce))
        self.assertEqual(produce.await_count, 2)

    def test_misses_can_be_remembered_briefly(self):
        produce = AsyncMock(return_value=None)
        asyncio.run(cache.cached("find", "Nothing", 60, produce, miss_ttl=600))
        asyncio.run(cache.cached("find", "Nothing", 60, produce, miss_ttl=600))
        produce.assert_awaited_once()

    def test_errors_are_not_cached(self):
        produce = AsyncMock(side_effect=[RuntimeError("site down"), "ok"])
        with self.assertRaises(RuntimeError):
            asyncio.run(cache.cached("find", "Gosu", 60, produce))
        self.assertEqual(asyncio.run(cache.cached("find", "Gosu", 60, produce)), "ok")

    def test_entries_expire(self):
        asyncio.run(cache.cached("find", "Gosu", 60, AsyncMock(return_value="old")))
        with patch("time.time", return_value=time.time() + 61):
            self.assertEqual(asyncio.run(cache.cached("find", "Gosu", 60, AsyncMock(return_value="new"))), "new")

    def test_expired_rows_are_purged(self):
        asyncio.run(cache.cached("find", "Gosu", 1, AsyncMock(return_value="x")))
        with patch("time.time", return_value=time.time() + 5):
            db.purge_expired()
        conn = db.get_connection()
        try:
            self.assertEqual(conn.execute("SELECT COUNT(*) AS n FROM scrape_cache").fetchone()["n"], 0)
        finally:
            conn.close()


class EndpointCachingTests(CacheTestCase):
    def setUp(self):
        super().setUp()
        import main

        self.main = main
        self.client = TestClient(main.app)

    def test_find_searches_the_sites_once_for_many_readers(self):
        found = {"domain": "mangafreak", "series_url": "https://m/gosu", "chapters": [{"number": 1.0, "url": "u", "title": ""}]}
        with patch.object(self.main, "find_best_source", AsyncMock(return_value=found)) as search:
            for _ in range(5):
                r = self.client.get("/api/find", params={"title": "Gosu"})
                self.assertEqual(r.status_code, 200)
                self.assertEqual(r.json()["domain"], "mangafreak")
        search.assert_awaited_once()
        self.assertIn("max-age", r.headers["cache-control"])

    def test_a_chapter_that_failed_to_load_is_retried(self):
        with patch.object(self.main, "scrape_chapter", AsyncMock(side_effect=[
            {"domain": "comizy", "images": []},
            {"domain": "comizy", "images": ["https://x1.cmzcdn.org/a.webp"]},
        ])) as scrape:
            first = self.client.get("/api/scrape", params={"url": "https://comizy.io/x/chapter-1"})
            second = self.client.get("/api/scrape", params={"url": "https://comizy.io/x/chapter-1"})
            third = self.client.get("/api/scrape", params={"url": "https://comizy.io/x/chapter-1"})
        self.assertEqual(first.json()["images"], [])
        self.assertNotIn("cache-control", first.headers)
        self.assertEqual(second.json()["images"], ["https://x1.cmzcdn.org/a.webp"])
        self.assertEqual(third.json()["images"], ["https://x1.cmzcdn.org/a.webp"])
        self.assertEqual(scrape.await_count, 2)

    def test_kaliscan_chapters_expire_quickly_because_their_image_links_do(self):
        self.assertLess(self.main.KALISCAN_CHAPTER_TTL, 11 * 3600)
