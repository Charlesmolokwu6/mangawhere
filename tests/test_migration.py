import sqlite3
import tempfile
import time
import unittest
from pathlib import Path

from server import db


class PasswordColumnMigrationTests(unittest.TestCase):
    """OAuth-only accounts need password_hash/salt to be nullable, but the
    original schema had them NOT NULL. SQLite can't ALTER COLUMN to drop
    that constraint, so _migrate() rebuilds the table instead -- this
    exercises that rebuild starting from a real old-style database, not
    just a fresh one (which would already have the new schema and never
    touch this code path)."""

    def setUp(self):
        self._tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self._tmp.close()
        db.DB_PATH = Path(self._tmp.name)

        # Build the OLD schema by hand -- password_hash/salt NOT NULL, no
        # oauth_accounts table -- then seed a real user row.
        conn = sqlite3.connect(db.DB_PATH)
        conn.execute(
            """
            CREATE TABLE users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                email TEXT UNIQUE NOT NULL,
                name TEXT NOT NULL,
                password_hash TEXT NOT NULL,
                salt TEXT NOT NULL,
                avatar_url TEXT,
                created_at REAL NOT NULL
            )
            """
        )
        conn.execute(
            "INSERT INTO users (email, name, password_hash, salt, created_at) "
            "VALUES (?, ?, ?, ?, ?)",
            ("old@example.com", "old", "somehash", "somesalt", time.time()),
        )
        conn.commit()
        conn.close()

    def tearDown(self):
        Path(self._tmp.name).unlink(missing_ok=True)

    def test_password_columns_become_nullable_without_losing_data(self):
        db.init_db()

        conn = db.get_connection()
        try:
            cols = {row["name"]: row for row in conn.execute("PRAGMA table_info(users)")}
            self.assertEqual(cols["password_hash"]["notnull"], 0)
            self.assertEqual(cols["salt"]["notnull"], 0)

            row = conn.execute(
                "SELECT * FROM users WHERE email = ?", ("old@example.com",)
            ).fetchone()
            self.assertIsNotNone(row)
            self.assertEqual(row["password_hash"], "somehash")
            self.assertEqual(row["name"], "old")
        finally:
            conn.close()

    def test_oauth_accounts_table_exists_after_migration(self):
        db.init_db()
        conn = db.get_connection()
        try:
            # Doesn't raise -- the table exists and is queryable.
            rows = conn.execute("SELECT * FROM oauth_accounts").fetchall()
            self.assertEqual(rows, [])
        finally:
            conn.close()

    def test_migration_is_a_no_op_the_second_time(self):
        db.init_db()
        db.init_db()  # would error on a second table rebuild if not guarded
        conn = db.get_connection()
        try:
            row = conn.execute(
                "SELECT * FROM users WHERE email = ?", ("old@example.com",)
            ).fetchone()
            self.assertIsNotNone(row)
        finally:
            conn.close()

    def test_can_insert_a_passwordless_user_after_migration(self):
        db.init_db()
        conn = db.get_connection()
        try:
            conn.execute(
                "INSERT INTO users (email, name, password_hash, salt, avatar_url, created_at) "
                "VALUES (?, ?, NULL, NULL, ?, ?)",
                ("oauth@example.com", "oauth", None, time.time()),
            )
            conn.commit()
            row = conn.execute(
                "SELECT * FROM users WHERE email = ?", ("oauth@example.com",)
            ).fetchone()
            self.assertIsNone(row["password_hash"])
        finally:
            conn.close()


if __name__ == "__main__":
    unittest.main()
