import json
import tempfile
import time
import unittest
from pathlib import Path

from fastapi.testclient import TestClient

from server import db, health


def chapters(*numbers):
    return [{"number": float(n), "url": f"u{n}", "title": ""} for n in numbers]


class HealthDbTestCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self._tmp.close()
        db.DB_PATH = Path(self._tmp.name)
        db.init_db()

    def tearDown(self):
        Path(self._tmp.name).unlink(missing_ok=True)

    def _row(self, title):
        conn = db.get_connection()
        try:
            row = conn.execute("SELECT * FROM title_health WHERE key = ?", (health.key_for(title),)).fetchone()
            return dict(row) if row else None
        finally:
            conn.close()


class StatusTests(HealthDbTestCase):
    def test_unknown_titles_are_queued_as_pending(self):
        out = health.statuses([{"title": "Solo Leveling", "alts": ["Na Honjaman Level Up"]}])
        self.assertEqual(out, {"Solo Leveling": "pending"})
        row = self._row("solo  LEVELING")  # same key, whatever the spacing/case
        self.assertEqual(row["status"], "pending")
        self.assertEqual(json.loads(row["alts"]), ["Na Honjaman Level Up"])

    def test_known_titles_return_their_status(self):
        health.statuses([{"title": "A"}, {"title": "B"}])
        health.record(health.key_for("A"), health.OK, None, "mangaread")
        health.record(health.key_for("B"), health.BROKEN, "Not found on any source", None)
        self.assertEqual(health.statuses([{"title": "A"}, {"title": "B"}]), {"A": "ok", "B": "broken"})

    def test_junk_is_ignored(self):
        out = health.statuses([{"title": ""}, {"title": "x" * 500}, {"nope": 1}, "str", {"title": 5}])
        self.assertEqual(out, {})

    def test_the_queue_is_capped(self):
        original = health.MAX_QUEUED
        health.MAX_QUEUED = 2
        try:
            health.statuses([{"title": t} for t in ("A", "B", "C")])
        finally:
            health.MAX_QUEUED = original
        self.assertIsNotNone(self._row("B"))
        self.assertIsNone(self._row("C"))


class RemoteDatabaseTests(HealthDbTestCase):
    """On Turso every statement is a network round trip, and a transaction
    left open while dozens run gets cancelled ("stream was idle for too
    long"), which is what broke the first live deploy. A status lookup
    must stay a fixed handful of statements however many titles it covers."""

    def count_statements(self, fn):
        real = db.get_connection
        count = {"n": 0}

        class Counting:
            def __init__(self, conn):
                self._conn = conn

            def execute(self, *args):
                count["n"] += 1
                return self._conn.execute(*args)

            def __getattr__(self, name):
                return getattr(self._conn, name)

        db.get_connection = lambda: Counting(real())
        try:
            fn()
        finally:
            db.get_connection = real
        return count["n"]

    def test_status_lookup_is_a_few_statements_however_many_titles(self):
        titles = [{"title": f"Title {i}", "alts": ["Alt"]} for i in range(100)]
        self.assertLessEqual(self.count_statements(lambda: health.statuses(titles)), 4)
        # Seen again the same day: reads only.
        self.assertLessEqual(self.count_statements(lambda: health.statuses(titles)), 2)
        self.assertEqual(health.summary()["pending"], 100)

    def test_a_report_is_one_statement(self):
        self.assertEqual(self.count_statements(lambda: health.report("A", [])), 1)
        self.assertEqual(self.count_statements(lambda: health.report("A", [])), 1)
        self.assertEqual(self._row("A")["reports"], 2)


class QueueTests(HealthDbTestCase):
    def test_order_is_unchecked_then_reported_then_stale(self):
        health.statuses([{"title": "Fresh"}, {"title": "Stale"}, {"title": "Reported"}])
        for t in ("Fresh", "Stale", "Reported"):
            health.record(health.key_for(t), health.OK, None, "x")
        conn = db.get_connection()
        try:
            # Backdate the checks (and the requests that led to them, which
            # always come first) so they look like they happened earlier.
            long_ago = time.time() - health.OK_RECHECK - 60
            a_while_ago = time.time() - health.REPORT_RECHECK - 60
            for key, when in (("stale", long_ago), ("reported", a_while_ago)):
                conn.execute(
                    "UPDATE title_health SET checked_at = ?, requested_at = ? WHERE key = ?",
                    (when, when - 1, key),
                )
            conn.commit()
        finally:
            conn.close()
        health.report("Reported", [])
        health.statuses([{"title": "Brand New"}])

        self.assertEqual(health.next_due()["title"], "Brand New")
        health.record("brand new", health.OK, None, "x")
        self.assertEqual(health.next_due()["title"], "Reported")
        health.record("reported", health.OK, None, "x")
        self.assertEqual(health.next_due()["title"], "Stale")
        health.record("stale", health.OK, None, "x")
        self.assertIsNone(health.next_due())

    def test_a_report_does_not_mark_a_title_broken_by_itself(self):
        health.statuses([{"title": "A"}])
        health.record("a", health.OK, None, "x")
        health.report("A", [])
        row = self._row("A")
        self.assertEqual(row["status"], "ok")
        self.assertEqual(row["reports"], 1)

    def test_summary_lists_broken_titles_with_reasons(self):
        health.statuses([{"title": "Good"}, {"title": "Bad"}, {"title": "Waiting"}])
        health.record("good", health.OK, None, "x")
        health.record("bad", health.BROKEN, "Not found on any source", None)
        out = health.summary()
        self.assertEqual((out["ok"], out["broken"], out["pending"]), (1, 1, 1))
        self.assertEqual(out["broken_titles"][0]["title"], "Bad")
        self.assertEqual(out["broken_titles"][0]["reason"], "Not found on any source")


class CheckTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.scraped = []

    def fakes(self, found, pages, good_images):
        async def find(title, alts):
            return found

        async def scrape(url):
            self.scraped.append(url)
            return {"images": pages.get(url, [])}

        async def image_ok(url):
            return url in good_images

        return find, scrape, image_ok

    async def test_working_title_is_ok(self):
        found = {"domain": "mangaread", "chapters": chapters(1, 2), "alternates": []}
        status, reason, source = await health.check(
            "T", [], *self.fakes(found, {"u2": ["img2"]}, {"img2"})
        )
        self.assertEqual((status, reason, source), (health.OK, None, "mangaread"))

    async def test_not_found_is_broken(self):
        status, reason, _ = await health.check("T", [], *self.fakes(None, {}, set()))
        self.assertEqual((status, reason), (health.BROKEN, "Not found on any source"))

    async def test_an_empty_latest_chapter_falls_back_to_the_one_before(self):
        found = {"domain": "mangaread", "chapters": chapters(1, 2, 3), "alternates": []}
        status, _, _ = await health.check("T", [], *self.fakes(found, {"u2": ["img2"]}, {"img2"}))
        self.assertEqual(status, health.OK)
        self.assertEqual(self.scraped, ["u3", "u2"])

    async def test_broken_images_fall_back_to_an_alternate_source(self):
        alt = {"domain": "comizy", "chapters": [{"number": 2.0, "url": "c2", "title": ""}]}
        found = {"domain": "mangadex", "chapters": chapters(1, 2), "alternates": [alt]}
        pages = {"u2": ["dead"], "u1": ["dead"], "c2": ["live"]}
        status, _, source = await health.check("T", [], *self.fakes(found, pages, {"live"}))
        self.assertEqual((status, source), (health.OK, "comizy"))

    async def test_reason_names_what_failed_on_each_source(self):
        alt = {"domain": "comizy", "chapters": [{"number": 2.0, "url": "c2", "title": ""}]}
        found = {"domain": "mangadex", "chapters": chapters(2), "alternates": [alt]}
        status, reason, _ = await health.check(
            "T", [], *self.fakes(found, {"u2": ["dead"]}, set())
        )
        self.assertEqual(status, health.BROKEN)
        self.assertEqual(reason, "mangadex: page images didn't load; comizy: chapter pages didn't load")


class HealthApiTests(HealthDbTestCase):
    def setUp(self):
        super().setUp()
        import main

        self.client = TestClient(main.app)

    def test_status_report_and_summary(self):
        r = self.client.post("/api/health/status", json={"titles": [{"title": "Eleceed", "alts": []}]})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json(), {"statuses": {"Eleceed": "pending"}})

        health.record("eleceed", health.BROKEN, "Not found on any source", None)
        r = self.client.post("/api/health/status", json={"titles": [{"title": "Eleceed"}]})
        self.assertEqual(r.json(), {"statuses": {"Eleceed": "broken"}})

        self.assertEqual(self.client.post("/api/health/report", json={"title": "Eleceed"}).status_code, 200)
        self.assertEqual(self._row("Eleceed")["reports"], 1)

        summary = self.client.get("/api/health").json()
        self.assertEqual(summary["broken"], 1)
        self.assertEqual(summary["broken_titles"][0]["reader_reports"], 1)

    def test_bad_bodies_are_rejected(self):
        self.assertEqual(self.client.post("/api/health/status", json={"titles": "x"}).status_code, 400)
        self.assertEqual(self.client.post("/api/health/report", json={"title": ""}).status_code, 400)
        self.assertEqual(
            self.client.post("/api/health/status", content=b"not json",
                             headers={"content-type": "application/json"}).status_code,
            400,
        )


if __name__ == "__main__":
    unittest.main()
