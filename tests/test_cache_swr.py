import asyncio
import tempfile
import time
import unittest
from pathlib import Path

from server import cache, db


class StaleWhileRevalidateTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self._tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self._tmp.close()
        db.DB_PATH = Path(self._tmp.name)
        db.init_db()
        cache.clear_memory()
        self.calls = []

    def tearDown(self):
        cache.clear_memory()
        Path(self._tmp.name).unlink(missing_ok=True)

    def producer(self, value, delay=0.0, fail=False):
        async def produce():
            self.calls.append(value)
            await asyncio.sleep(delay)
            if fail:
                raise RuntimeError("site down")
            return value
        return produce

    async def age(self, raw_key, seconds, keep_ttl):
        """Make the cached entry look `seconds` old."""
        key = cache._key("find", raw_key)
        expires_at, value = cache._memory[key]
        cache._memory[key] = (time.time() - seconds + keep_ttl, value)

    async def test_first_lookup_waits_then_is_cached(self):
        v = await cache.cached_swr("find", "T", 60, 3600, self.producer({"a": 1}))
        self.assertEqual(v, {"a": 1})
        v = await cache.cached_swr("find", "T", 60, 3600, self.producer({"a": 2}))
        self.assertEqual(v, {"a": 1})
        self.assertEqual(self.calls, [{"a": 1}])

    async def test_a_stale_answer_is_served_at_once_and_refreshed_behind_the_scenes(self):
        await cache.cached_swr("find", "T", 60, 3600, self.producer({"v": "old"}))
        await self.age("T", 120, 3600)

        started = time.monotonic()
        v = await cache.cached_swr("find", "T", 60, 3600, self.producer({"v": "new"}, delay=0.2))
        self.assertLess(time.monotonic() - started, 0.1)  # didn't wait for the refresh
        self.assertEqual(v, {"v": "old"})

        await asyncio.sleep(0.3)
        v = await cache.cached_swr("find", "T", 60, 3600, self.producer({"v": "newer"}))
        self.assertEqual(v, {"v": "new"})

    async def test_only_one_refresh_runs_at_a_time(self):
        await cache.cached_swr("find", "T", 60, 3600, self.producer({"v": 1}))
        await self.age("T", 120, 3600)
        for _ in range(5):
            await cache.cached_swr("find", "T", 60, 3600, self.producer({"v": 2}, delay=0.1))
        await asyncio.sleep(0.2)
        self.assertEqual(self.calls, [{"v": 1}, {"v": 2}])

    async def test_a_failed_refresh_keeps_the_old_answer(self):
        await cache.cached_swr("find", "T", 60, 3600, self.producer({"v": "good"}))
        await self.age("T", 120, 3600)
        await cache.cached_swr("find", "T", 60, 3600, self.producer(None, fail=True))
        await asyncio.sleep(0.05)
        v = await cache.cached_swr("find", "T", 60, 3600, self.producer({"v": "x"}))
        self.assertEqual(v, {"v": "good"})

    async def test_a_miss_is_not_served_stale(self):
        v = await cache.cached_swr("find", "T", 60, 3600, self.producer(None), miss_ttl=0.05)
        self.assertIsNone(v)
        await asyncio.sleep(0.1)
        v = await cache.cached_swr("find", "T", 60, 3600, self.producer({"v": "found"}), miss_ttl=0.05)
        self.assertEqual(v, {"v": "found"})

    async def test_store_replaces_an_answer(self):
        await cache.cached_swr("find", "T", 60, 3600, self.producer({"v": "quick"}))
        await cache.store("find", "T", {"v": "full"}, 3600)
        v = await cache.cached_swr("find", "T", 60, 3600, self.producer({"v": "x"}))
        self.assertEqual(v, {"v": "full"})
        cache.clear_memory()  # and it was written through to the database
        v = await cache.cached_swr("find", "T", 60, 3600, self.producer({"v": "x"}))
        self.assertEqual(v, {"v": "full"})


if __name__ == "__main__":
    unittest.main()
