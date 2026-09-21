import json
import time
from typing import Any, Dict, List

from . import db


def upsert(user_id: int, payload: Dict[str, Any]) -> None:
    title = payload.get("title") or {}
    key = str(title.get("key") or "")
    if not key:
        return

    conn = db.get_connection()
    try:
        conn.execute(
            """
            INSERT INTO watches
                (user_id, key, name, cover, kind, country, links,
                 seen_chapter, latest_chapter, endpoint, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(user_id, key) DO UPDATE SET
                name = excluded.name,
                cover = excluded.cover,
                kind = excluded.kind,
                country = excluded.country,
                links = excluded.links,
                seen_chapter = excluded.seen_chapter,
                endpoint = excluded.endpoint,
                updated_at = excluded.updated_at
            """,
            (
                user_id,
                key,
                title.get("name") or "",
                title.get("cover") or "",
                title.get("kind") or "",
                title.get("country") or "",
                json.dumps(title.get("links") or []),
                payload.get("seen_chapter"),
                payload.get("seen_chapter"),
                payload.get("endpoint"),
                time.time(),
            ),
        )
        conn.commit()
    finally:
        conn.close()


def remove(user_id: int, key: str) -> None:
    conn = db.get_connection()
    try:
        conn.execute(
            "DELETE FROM watches WHERE user_id = ? AND key = ?", (user_id, str(key))
        )
        conn.commit()
    finally:
        conn.close()


def mark_read(user_id: int, key: str) -> None:
    conn = db.get_connection()
    try:
        conn.execute(
            "UPDATE watches SET seen_chapter = latest_chapter, updated_at = ? "
            "WHERE user_id = ? AND key = ?",
            (time.time(), user_id, str(key)),
        )
        conn.commit()
    finally:
        conn.close()


def list_for_user(user_id: int) -> List[Dict[str, Any]]:
    conn = db.get_connection()
    try:
        rows = conn.execute(
            "SELECT * FROM watches WHERE user_id = ? ORDER BY updated_at DESC",
            (user_id,),
        ).fetchall()
    finally:
        conn.close()

    return [
        {
            "key": row["key"],
            "name": row["name"],
            "cover": row["cover"],
            "kind": row["kind"],
            "seen": row["seen_chapter"],
            "latest": row["latest_chapter"],
        }
        for row in rows
    ]


def count_for_user(user_id: int) -> int:
    conn = db.get_connection()
    try:
        row = conn.execute(
            "SELECT COUNT(*) AS n FROM watches WHERE user_id = ?", (user_id,)
        ).fetchone()
    finally:
        conn.close()
    return row["n"] if row else 0
