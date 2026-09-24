import time
from typing import Any, Dict, List

from . import db

MAX_BODY_LENGTH = 2000
LIST_LIMIT = 100


def add(user_id: int, name: str, title_key: str, chapter_key: str, body: str) -> Dict[str, Any]:
    title_key = (title_key or "").strip()
    chapter_key = (chapter_key or "").strip()
    body = (body or "").strip()[:MAX_BODY_LENGTH]
    if not title_key or not chapter_key or not body:
        raise ValueError("A comment needs a title, chapter, and body")

    created_at = time.time()
    conn = db.get_connection()
    try:
        cur = conn.execute(
            """
            INSERT INTO comments (user_id, name, title_key, chapter_key, body, created_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (user_id, name, title_key, chapter_key, body, created_at),
        )
        conn.commit()
        comment_id = cur.lastrowid
    finally:
        conn.close()

    return {
        "id": comment_id,
        "name": name,
        "body": body,
        "created_at": created_at,
    }


def list_for(title_key: str, chapter_key: str) -> List[Dict[str, Any]]:
    title_key = (title_key or "").strip()
    chapter_key = (chapter_key or "").strip()
    if not title_key or not chapter_key:
        return []

    conn = db.get_connection()
    try:
        rows = conn.execute(
            "SELECT id, name, body, created_at FROM comments "
            "WHERE title_key = ? AND chapter_key = ? "
            "ORDER BY created_at DESC LIMIT ?",
            (title_key, chapter_key, LIST_LIMIT),
        ).fetchall()
    finally:
        conn.close()

    return [
        {"id": row["id"], "name": row["name"], "body": row["body"], "created_at": row["created_at"]}
        for row in rows
    ]
