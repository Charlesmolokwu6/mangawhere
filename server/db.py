import sqlite3
import time
from pathlib import Path

DATA_DIR = Path(__file__).resolve().parent.parent / "data"
DATA_DIR.mkdir(exist_ok=True)
DB_PATH = DATA_DIR / "mangawhere.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    email TEXT UNIQUE NOT NULL,
    name TEXT NOT NULL,
    password_hash TEXT NOT NULL,
    salt TEXT NOT NULL,
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
    title_key TEXT NOT NULL,
    chapter_key TEXT NOT NULL,
    body TEXT NOT NULL,
    created_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_comments_lookup
    ON comments (title_key, chapter_key, created_at);
"""


def get_connection() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db() -> None:
    conn = get_connection()
    try:
        conn.executescript(SCHEMA)
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
