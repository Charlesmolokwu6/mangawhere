import tempfile
import time
import unittest
from pathlib import Path

from fastapi.testclient import TestClient

from server import db, health


def chapters(*numbers):
    return [{"number": float(n), "url": f"u{n}", "title": ""} for n in numbers]


class HealthTestCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self._tmp.close()
        db.DB_PATH = Path(self._tmp.name)
        db.init_db()
        health.reset()

    def tearDown(self):
        health.reset()
        Path(self._tmp.name).unlink(missing_ok=True)

    def entry(self, title):
        return health._titles.get(health.key_for(title))

    def backdate(self, title, checked_ago, requested_ago=None):
        e = self.entry(title)
        e["checked_at"] = time.time() - checked_ago
        e["requested_at"] = time.time() - (requested_ago if requested_ago is not None else checked_ago + 1)


class StatusTests(HealthTestCase):
    def test_unknown_titles_are_queued_as_pending(self):
        out = health.statuses([{"title": "Solo Leveling", "alts": ["Na Honjaman Level Up"]}])
        self.assertEqual(out, {"Solo Leveling": "pending"})
        e = self.entry("solo  LEVELING")  # same key, whatever the spacing/case
        self.assertEqual(e["status"], "pending")
        self.assertEqual(e["alts"], ["Na Honjaman Level Up"])

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
        self.assertIsNotNone(self.entry("B"))
        self.assertIsNone(self.entry("C"))

    def test_lookups_never_touch_the_database(self):
        # Every page that shows titles asks; on Turso each statement is a
        # network round trip (the first live deploy took 4-8s and 500ed).
        real = db.get_connection

        def forbidden():
            raise AssertionError("status lookup opened the database")

        db.get_connection = forbidden
        try:
            health.statuses([{"title": f"Title {i}"} for i in range(100)])
            health.report("Title 1", [])
            health.summary()
        finally:
            db.get_connection = real


class PersistenceTests(HealthTestCase):
    def test_results_survive_a_restart(self):
        health.statuses([{"title": "Good", "alts": ["Alt"]}, {"title": "Bad"}])
        health.record("good", health.OK, None, "comizy")
        health.record("bad", health.BROKEN, "Not found on any source", None)
        health.report("Bad", [])
        self.assertEqual(health.save(), 2)
        self.assertEqual(health.save(), 0)  # nothing changed since

        health.reset()
        health.load()
        self.assertEqual(self.entry("Good")["status"], "ok")
        self.assertEqual(self.entry("Good")["alts"], ["Alt"])
        self.assertEqual(self.entry("Bad")["reason"], "Not found on any source")
        self.assertEqual(self.entry("Bad")["reports"], 1)

        # Changed again and saved again: an update, not a second row.
        health.record("good", health.BROKEN, "comizy: page images didn't load", None)
        health.save()
        health.reset()
        health.load()
        self.assertEqual(self.entry("Good")["status"], "broken")

    def test_load_works_when_rows_come_back_without_column_names(self):
        # On the live database (libsql) the saved rows came back with no
        # column names: row["key"] raised KeyError('key') and every restart
        # started from an empty list. load() reads them by position.
        health.statuses([{"title": "A", "alts": ["Alt A"]}])
        health.record("a", health.OK, None, "comizy")
        health.save()

        real = db.get_connection

        class Conn:
            def __init__(self):
                self._conn = real()

            def execute(self, *args):
                cursor = self._conn.execute(*args)

                class Cursor:
                    def fetchall(self):
                        return [db._Row([], tuple(r)) for r in cursor.fetchall()]

                return Cursor()

            def close(self):
                self._conn.close()

        health.reset()
        db.get_connection = Conn
        try:
            health.load()
        finally:
            db.get_connection = real
        self.assertEqual(self.entry("A")["status"], "ok")
        self.assertEqual(self.entry("A")["alts"], ["Alt A"])
        self.assertEqual(self.entry("A")["source"], "comizy")

    def test_a_failed_save_is_retried(self):
        health.statuses([{"title": "A"}])
        real = db.get_connection

        def unreachable():
            raise RuntimeError("database unreachable")

        db.get_connection = unreachable
        try:
            with self.assertRaises(RuntimeError):
                health.save()
        finally:
            db.get_connection = real
        self.assertEqual(health.save(), 1)


class QueueTests(HealthTestCase):
    def test_order_is_unchecked_then_reported_then_stale(self):
        health.statuses([{"title": "Fresh"}, {"title": "Stale"}, {"title": "Reported"}])
        for t in ("Fresh", "Stale", "Reported"):
            health.record(health.key_for(t), health.OK, None, "x")
        self.backdate("Stale", health.OK_RECHECK + 60)
        self.backdate("Reported", health.REPORT_RECHECK + 60)
        health.report("Reported", [])
        health.statuses([{"title": "Brand New"}])

        self.assertEqual(health.next_due()["title"], "Brand New")
        health.record("brand new", health.OK, None, "x")
        self.assertEqual(health.next_due()["title"], "Reported")
        health.record("reported", health.OK, None, "x")
        self.assertEqual(health.next_due()["title"], "Stale")
        health.record("stale", health.OK, None, "x")
        self.assertIsNone(health.next_due())

    def test_broken_titles_are_rechecked_sooner_than_working_ones(self):
        health.statuses([{"title": "Ok"}, {"title": "Broken"}])
        health.record("ok", health.OK, None, "x")
        health.record("broken", health.BROKEN, "gone", None)
        self.backdate("Ok", health.BROKEN_RECHECK + 60)
        self.backdate("Broken", health.BROKEN_RECHECK + 60)
        self.assertEqual(health.next_due()["title"], "Broken")
        health.record("broken", health.BROKEN, "gone", None)
        self.assertIsNone(health.next_due())

    def test_a_report_does_not_mark_a_title_broken_by_itself(self):
        health.statuses([{"title": "A"}])
        health.record("a", health.OK, None, "x")
        health.report("A", [])
        self.assertEqual(self.entry("A")["status"], "ok")
        self.assertEqual(self.entry("A")["reports"], 1)

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
        self.image_requests = []
        self._pause = health.IMAGE_RETRY_PAUSE
        health.IMAGE_RETRY_PAUSE = 0

    def tearDown(self):
        health.IMAGE_RETRY_PAUSE = self._pause

    def fakes(self, found, pages, good_images, fails_first_time=()):
        async def find(title, alts):
            return found

        async def scrape(url):
            self.scraped.append(url)
            return {"images": pages.get(url, [])}

        async def image_ok(url):
            first_time = url not in self.image_requests
            self.image_requests.append(url)
            if url in fails_first_time and first_time:
                return False
            return url in good_images

        return find, scrape, image_ok

    async def test_a_page_that_fails_once_is_asked_for_again(self):
        # MangaDex's image servers 404 a page they haven't cached yet.
        found = {"domain": "mangadex", "chapters": chapters(1), "alternates": []}
        status, _, source = await health.check(
            "T", [], *self.fakes(found, {"u1": ["p1", "p2"]}, {"p1"}, fails_first_time={"p1"})
        )
        self.assertEqual((status, source), (health.OK, "mangadex"))
        self.assertEqual(self.image_requests, ["p1", "p1"])

    async def test_the_second_page_counts_if_the_first_never_loads(self):
        found = {"domain": "mangadex", "chapters": chapters(1), "alternates": []}
        status, _, _ = await health.check("T", [], *self.fakes(found, {"u1": ["p1", "p2"]}, {"p2"}))
        self.assertEqual(status, health.OK)
        self.assertEqual(self.image_requests, ["p1", "p1", "p2"])

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


class HealthApiTests(HealthTestCase):
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
        self.assertEqual(self.entry("Eleceed")["reports"], 1)

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
