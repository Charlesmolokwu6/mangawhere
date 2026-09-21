import asyncio
import json
import os
import time
from typing import Dict, List

from . import db, mu, push

POLL_INTERVAL_SECONDS = int(os.environ.get("POLL_INTERVAL_SECONDS", 30 * 60))

_task: "asyncio.Task | None" = None


def _distinct_titles() -> List[Dict]:
    conn = db.get_connection()
    try:
        rows = conn.execute(
            "SELECT DISTINCT key, name FROM watches WHERE name != ''"
        ).fetchall()
    finally:
        conn.close()
    return [{"key": r["key"], "name": r["name"]} for r in rows]


def _watchers_for_key(key: str) -> List[Dict]:
    conn = db.get_connection()
    try:
        rows = conn.execute(
            "SELECT user_id, seen_chapter, latest_chapter, endpoint, name "
            "FROM watches WHERE key = ?",
            (key,),
        ).fetchall()
    finally:
        conn.close()
    return [dict(r) for r in rows]


def _update_latest(user_id: int, key: str, latest: float) -> None:
    conn = db.get_connection()
    try:
        conn.execute(
            "UPDATE watches SET latest_chapter = ?, updated_at = ? "
            "WHERE user_id = ? AND key = ?",
            (latest, time.time(), user_id, key),
        )
        conn.commit()
    finally:
        conn.close()


async def _check_title(title: Dict) -> None:
    latest = await mu.latest_chapter(title["name"])
    if latest is None:
        return

    for watcher in _watchers_for_key(title["key"]):
        previous = watcher["latest_chapter"]
        _update_latest(watcher["user_id"], title["key"], latest)

        already_notified = previous is not None and latest <= previous
        seen = watcher["seen_chapter"]
        is_new_to_reader = seen is None or latest > seen
        if already_notified or not is_new_to_reader or not watcher["endpoint"]:
            continue

        payload = json.dumps(
            {
                "title": "New chapter!",
                "body": f"{title['name']} — chapter {latest:g} is out.",
                "tag": f"mangawhere-{title['key']}",
            }
        )
        push.send(watcher["endpoint"], payload)


async def _run_forever() -> None:
    while True:
        try:
            db.purge_expired()
            for title in _distinct_titles():
                await _check_title(title)
        except Exception:
            # One bad cycle (a network blip, a malformed MU response)
            # shouldn't take the poller down for good.
            pass
        await asyncio.sleep(POLL_INTERVAL_SECONDS)


def start() -> None:
    global _task
    if _task is None or _task.done():
        _task = asyncio.ensure_future(_run_forever())


def stop() -> None:
    global _task
    if _task is not None:
        _task.cancel()
        _task = None
