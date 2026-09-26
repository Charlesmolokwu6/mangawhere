import unittest
from unittest.mock import patch

import libsql

from server import db


class LibsqlAdapterTests(unittest.TestCase):
    """The Turso path wraps libsql's plain-tuple cursor results so they
    support the same row["column"] access every consumer (auth.py,
    watch.py, comments.py, push.py, poller.py) already relies on. Exercised
    against a plain local libsql connection (no sync_url) — real Turso
    network behavior is out of scope for a unit test, only the adapter's
    own translation logic is."""

    def _connection(self):
        return db._LibsqlConnectionAdapter(libsql.connect(":memory:"))

    def test_bracket_access_on_fetchone(self):
        conn = self._connection()
        conn.executescript("CREATE TABLE t (id INTEGER PRIMARY KEY, name TEXT)")
        conn.execute("INSERT INTO t (name) VALUES (?)", ("hello",))
        conn.commit()

        row = conn.execute("SELECT * FROM t WHERE id = ?", (1,)).fetchone()
        self.assertEqual(row["id"], 1)
        self.assertEqual(row["name"], "hello")

    def test_bracket_access_on_fetchall_and_iteration(self):
        conn = self._connection()
        conn.executescript("CREATE TABLE t (id INTEGER PRIMARY KEY, name TEXT)")
        conn.execute("INSERT INTO t (name) VALUES (?)", ("a",))
        conn.execute("INSERT INTO t (name) VALUES (?)", ("b",))
        conn.commit()

        rows = conn.execute("SELECT * FROM t ORDER BY id").fetchall()
        self.assertEqual([r["name"] for r in rows], ["a", "b"])

        iterated = list(conn.execute("SELECT * FROM t ORDER BY id"))
        self.assertEqual([r["name"] for r in iterated], ["a", "b"])

    def test_fetchone_returns_none_when_no_row(self):
        conn = self._connection()
        conn.executescript("CREATE TABLE t (id INTEGER PRIMARY KEY, name TEXT)")
        conn.commit()
        self.assertIsNone(conn.execute("SELECT * FROM t WHERE id = 1").fetchone())

    def test_lastrowid_is_passed_through(self):
        conn = self._connection()
        conn.executescript("CREATE TABLE t (id INTEGER PRIMARY KEY, name TEXT)")
        cur = conn.execute("INSERT INTO t (name) VALUES (?)", ("x",))
        conn.commit()
        self.assertEqual(cur.lastrowid, 1)

    def test_pragma_table_info_style_query_works(self):
        # _migrate() in db.py relies on exactly this shape of query.
        conn = self._connection()
        conn.executescript("CREATE TABLE t (id INTEGER PRIMARY KEY, name TEXT)")
        conn.commit()
        cols = {row["name"] for row in conn.execute("PRAGMA table_info(t)")}
        self.assertEqual(cols, {"id", "name"})


class GetConnectionBackendSelectionTests(unittest.TestCase):
    def test_uses_plain_sqlite_when_turso_not_configured(self):
        with patch.object(db, "TURSO_DATABASE_URL", ""):
            conn = db.get_connection()
        try:
            self.assertNotIsInstance(conn, db._LibsqlConnectionAdapter)
        finally:
            conn.close()

    def test_uses_libsql_adapter_when_turso_configured(self):
        fake_conn = libsql.connect(":memory:")
        with patch.object(db, "TURSO_DATABASE_URL", "libsql://example.turso.io"), \
             patch.object(db, "TURSO_AUTH_TOKEN", "fake-token"), \
             patch("libsql.connect", return_value=fake_conn) as mock_connect:
            conn = db.get_connection()
        try:
            self.assertIsInstance(conn, db._LibsqlConnectionAdapter)
            mock_connect.assert_called_once_with(
                str(db.TURSO_REPLICA_PATH),
                sync_url="libsql://example.turso.io",
                auth_token="fake-token",
            )
        finally:
            conn.close()


if __name__ == "__main__":
    unittest.main()
