import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from server import adcontrol, auth, db


class GrowthStatsTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self._tmp.close()
        db.DB_PATH = Path(self._tmp.name)
        db.init_db()

    def tearDown(self):
        Path(self._tmp.name).unlink(missing_ok=True)

    def _add_user(self, email, created_at):
        ph, salt = auth.hash_password("password123")
        conn = db.get_connection()
        try:
            conn.execute(
                "INSERT INTO users (email, name, password_hash, salt, created_at) VALUES (?, ?, ?, ?, ?)",
                (email, email.split("@")[0], ph, salt, created_at),
            )
            conn.commit()
        finally:
            conn.close()

    def test_counts_split_new_signups_by_age(self):
        now = time.time()
        self._add_user("old@example.com", now - 10 * 86400)   # outside 7d
        self._add_user("week@example.com", now - 3 * 86400)   # within 7d, not today
        self._add_user("today@example.com", now - 100)        # within 24h
        s = adcontrol.growth_stats()
        self.assertEqual(s["users"], 3)
        self.assertEqual(s["new_1d"], 1)
        self.assertEqual(s["new_7d"], 2)

    def test_empty_database_reports_zeros_not_an_error(self):
        s = adcontrol.growth_stats()
        self.assertEqual(s, {"users": 0, "new_1d": 0, "new_7d": 0,
                             "tracked": 0, "reachable": 0, "comments": 0})
        # The rendered view still builds (and shows the goal).
        self.assertIn("0 / 100", adcontrol._growth_text(s))

    def test_stats_command_replies_with_growth_to_the_admin(self):
        self._add_user("reader@example.com", time.time())
        with patch.dict(os.environ, {"TELEGRAM_BOT_TOKEN": "t", "TELEGRAM_ADMIN_ID": "42"}), \
                patch.object(adcontrol, "_tg", AsyncMock()) as tg:
            import asyncio
            asyncio.run(adcontrol.handle_update(
                {"message": {"from": {"id": 42}, "text": "/stats"}}
            ))
        sent = tg.await_args_list[0].args[1]
        self.assertIn("users & growth", sent["text"])
        self.assertIn("Accounts: 1 / 100", sent["text"])

    def test_a_non_admin_gets_no_stats(self):
        with patch.dict(os.environ, {"TELEGRAM_BOT_TOKEN": "t", "TELEGRAM_ADMIN_ID": "42"}), \
                patch.object(adcontrol, "_tg", AsyncMock()) as tg:
            import asyncio
            asyncio.run(adcontrol.handle_update(
                {"message": {"from": {"id": 9999}, "text": "/stats"}}
            ))
        # Nothing with growth data was sent to a stranger.
        for call in tg.await_args_list:
            self.assertNotIn("users & growth", call.args[1].get("text", ""))


if __name__ == "__main__":
    unittest.main()
