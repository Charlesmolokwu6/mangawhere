import os
import sqlite3
import time
from pathlib import Path

DATA_DIR = Path(__file__).resolve().parent.parent / "data"
DATA_DIR.mkdir(exist_ok=True)
DB_PATH = DATA_DIR / "mangawhere.db"

# Render's free plan wipes DATA_DIR on every redeploy — fine for trying
# things out, but it means every account, watch list, and comment is lost
# each time the app restarts. Setting these two switches every read/write in
# this file over to a Turso (libSQL) database instead, via an "embedded
# replica": a local file that mirrors a remote primary, so reads stay as
# fast as plain SQLite while writes are transparently forwarded to, and
# durable on, that remote database regardless of what happens to this
# container. See README.md for how to provision one. Left unset, everything
# below behaves exactly as it always has — a local SQLite file, wiped on
# redeploy same as before this existed.
TURSO_DATABASE_URL = os.environ.get("TURSO_DATABASE_URL", "").strip()
TURSO_AUTH_TOKEN = os.environ.get("TURSO_AUTH_TOKEN", "").strip()
TURSO_REPLICA_PATH = DATA_DIR / "mangawhere-replica.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    email TEXT UNIQUE NOT NULL,
    name TEXT NOT NULL,
    password_hash TEXT NOT NULL,
    salt TEXT NOT NULL,
    avatar_url TEXT,
    created_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS sessions (
    token TEXT PRIMARY KEY,
    user_id INTEGER NOT NULL,
    created_at REAL NOT NULL,
    expires_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS push_subscriptions (
    endpoint TEXT PRIMARY KEY,
    p256dh TEXT NOT NULL,
    auth TEXT NOT NULL,
    user_id INTEGER,
    created_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS watches (
    user_id INTEGER NOT NULL,
    key TEXT NOT NULL,
    name TEXT NOT NULL,
    cover TEXT,
    kind TEXT,
    country TEXT,
    links TEXT,
    seen_chapter REAL,
    latest_chapter REAL,
    updated_at REAL NOT NULL,
    PRIMARY KEY (user_id, key)
);

CREATE TABLE IF NOT EXISTS captchas (
    id TEXT PRIMARY KEY,
    answer TEXT NOT NULL,
    expires_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS comments (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    name TEXT NOT NULL,
    avatar_url TEXT,
    title_key TEXT NOT NULL,
    chapter_key TEXT NOT NULL,
    body TEXT NOT NULL,
    created_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_comments_lookup
    ON comments (title_key, chapter_key, created_at);
"""


class _Row:
    """sqlite3.Row-alike for libsql's plain-tuple cursor results, built from
    the cursor's own column names — every existing `row["column"]` access
    across auth.py/watch.py/comments.py/push.py/poller.py works unchanged
    on either backend without those callers needing to know which is live."""

    __slots__ = ("_data",)

    def __init__(self, columns, values):
        self._data = dict(zip(columns, values))

    def __getitem__(self, key):
        return self._data[key]

    def keys(self):
        return self._data.keys()


class _CursorAdapter:
    def __init__(self, cursor):
        self._cursor = cursor

    @property
    def lastrowid(self):
        return self._cursor.lastrowid

    def _columns(self):
        return [d[0] for d in (self._cursor.description or [])]

    def fetchone(self):
        row = self._cursor.fetchone()
        return None if row is None else _Row(self._columns(), row)

    def fetchall(self):
        columns = self._columns()
        return [_Row(columns, row) for row in self._cursor.fetchall()]

    def __iter__(self):
        return iter(self.fetchall())


class _LibsqlConnectionAdapter:
    """Wraps a libsql embedded-replica connection so it matches the small
    slice of sqlite3.Connection's API this codebase actually uses — nothing
    outside this file needs to know which backend it's talking to."""

    def __init__(self, conn):
        self._conn = conn

    def execute(self, sql, params=()):
        return _CursorAdapter(self._conn.execute(sql, params))

    def executescript(self, sql):
        self._conn.executescript(sql)

    def commit(self):
        self._conn.commit()

    def close(self):
        self._conn.close()

    def sync(self):
        self._conn.sync()


def get_connection():
    if TURSO_DATABASE_URL:
        import libsql

        conn = libsql.connect(
            str(TURSO_REPLICA_PATH),
            sync_url=TURSO_DATABASE_URL,
            auth_token=TURSO_AUTH_TOKEN,
        )
        return _LibsqlConnectionAdapter(conn)

    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def _migrate(conn) -> None:
    """Add columns introduced after a table already existed. CREATE TABLE
    IF NOT EXISTS only helps on a fresh database — an upgrade needs its
    own ALTER TABLE, guarded so re-running it is a no-op."""
    cols = {row["name"] for row in conn.execute("PRAGMA table_info(users)")}
    if "avatar_url" not in cols:
        conn.execute("ALTER TABLE users ADD COLUMN avatar_url TEXT")

    comment_cols = {row["name"] for row in conn.execute("PRAGMA table_info(comments)")}
    if "avatar_url" not in comment_cols:
        conn.execute("ALTER TABLE comments ADD COLUMN avatar_url TEXT")


def init_db() -> None:
    conn = get_connection()
    try:
        if TURSO_DATABASE_URL:
            # A fresh container has no local replica file yet (or an out of
            # date one) — pull the real remote state down before touching
            # schema, so CREATE TABLE IF NOT EXISTS sees what's actually
            # there instead of an empty local file.
            conn.sync()
        conn.executescript(SCHEMA)
        _migrate(conn)
        conn.commit()
    finally:
        conn.close()


def purge_expired() -> None:
    """Drop expired sessions and captchas so the tables don't grow forever."""
    now = time.time()
    conn = get_connection()
    try:
        conn.execute("DELETE FROM sessions WHERE expires_at < ?", (now,))
        conn.execute("DELETE FROM captchas WHERE expires_at < ?", (now,))
        conn.commit()
    finally:
        conn.close()
