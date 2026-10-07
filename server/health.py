"""Does a title actually open in the reader?

Everything the site shows (Trending, "You might also like", search
results) comes from AniList/MyAnimeList, which know nothing about whether
any reading source has it. A background worker checks each title the way a
reader would hit it: find it on a source, load the latest chapter's pages,
and fetch the first page image. Browsing hides titles that fail; search
and the title page label them instead.

One title is checked at a time with a pause between checks, so the free
server isn't swamped. Results are re-checked periodically, sooner when a
reader's browser reports that a title it was told is fine didn't load.
"""

import asyncio
import json
import time
from typing import Any, Awaitable, Callable, Dict, List, Optional, Sequence

from . import db

PENDING = "pending"
OK = "ok"
BROKEN = "broken"

OK_RECHECK = 12 * 3600        # a working title is re-verified twice a day
BROKEN_RECHECK = 3 * 3600     # a broken one may have been fixed upstream
REPORT_RECHECK = 10 * 60      # a reader hit a failure: look again soon
CHECK_PAUSE = 20              # seconds between checks
IDLE_PAUSE = 60               # nothing due: look again in a minute
CHECK_TIMEOUT = 180           # one title's whole check
MAX_TITLES_PER_REQUEST = 120
MAX_QUEUED = 600              # unchecked titles accepted before new ones wait
MAX_TITLE_LENGTH = 200
MAX_ALTS = 6
SOURCES_TO_TRY = 4            # best source plus up to 3 alternates

Find = Callable[[str, Sequence[str]], Awaitable[Optional[Dict[str, Any]]]]
Scrape = Callable[[str], Awaitable[Optional[Dict[str, Any]]]]
ImageOk = Callable[[str], Awaitable[bool]]


def key_for(title: str) -> str:
    return " ".join((title or "").lower().split())


def _clean(title: Any, alts: Any) -> Optional[tuple]:
    if not isinstance(title, str):
        return None
    title = title.strip()
    if not title or len(title) > MAX_TITLE_LENGTH:
        return None
    clean_alts = []
    for alt in alts if isinstance(alts, list) else []:
        if isinstance(alt, str) and alt.strip() and len(alt) <= MAX_TITLE_LENGTH:
            clean_alts.append(alt.strip())
        if len(clean_alts) >= MAX_ALTS:
            break
    return title, clean_alts


def statuses(items: List[Dict[str, Any]]) -> Dict[str, str]:
    """Known status for each title ({"title", "alts"} dicts), keyed by the
    title as sent. Titles never seen before are queued for a check and
    come back "pending" (shown as normal until checked)."""
    now = time.time()
    out: Dict[str, str] = {}
    conn = db.get_connection()
    try:
        queued = conn.execute(
            "SELECT COUNT(*) AS n FROM title_health WHERE status = ?", (PENDING,)
        ).fetchone()["n"]
        for item in items[:MAX_TITLES_PER_REQUEST]:
            cleaned = _clean(item.get("title") if isinstance(item, dict) else None,
                             item.get("alts") if isinstance(item, dict) else None)
            if not cleaned:
                continue
            title, alts = cleaned
            key = key_for(title)
            row = conn.execute("SELECT status FROM title_health WHERE key = ?", (key,)).fetchone()
            if row:
                conn.execute("UPDATE title_health SET seen_at = ? WHERE key = ?", (now, key))
                out[title] = row["status"]
            elif queued < MAX_QUEUED:
                conn.execute(
                    "INSERT INTO title_health (key, title, alts, status, requested_at, seen_at) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    (key, title, json.dumps(alts), PENDING, now, now),
                )
                queued += 1
                out[title] = PENDING
            else:
                out[title] = PENDING
        conn.commit()
    finally:
        conn.close()
    return out


def report(title: Any, alts: Any) -> bool:
    """A reader's browser couldn't open a title: check it again soon. One
    report doesn't mark it broken (the reader's own connection may be the
    problem); the re-check decides."""
    cleaned = _clean(title, alts)
    if not cleaned:
        return False
    title, alts = cleaned
    now = time.time()
    key = key_for(title)
    conn = db.get_connection()
    try:
        row = conn.execute("SELECT key FROM title_health WHERE key = ?", (key,)).fetchone()
        if row:
            conn.execute(
                "UPDATE title_health SET reports = reports + 1, requested_at = ?, seen_at = ? WHERE key = ?",
                (now, now, key),
            )
        else:
            conn.execute(
                "INSERT INTO title_health (key, title, alts, status, requested_at, seen_at, reports) "
                "VALUES (?, ?, ?, ?, ?, ?, 1)",
                (key, title, json.dumps(alts), PENDING, now, now),
            )
        conn.commit()
    finally:
        conn.close()
    return True


def next_due() -> Optional[Dict[str, Any]]:
    """The title most in need of a check: never checked first, then ones a
    reader reported, then working/broken titles whose re-check is due."""
    now = time.time()
    conn = db.get_connection()
    try:
        row = conn.execute(
            "SELECT * FROM title_health WHERE status = ? ORDER BY requested_at LIMIT 1",
            (PENDING,),
        ).fetchone()
        if not row:
            row = conn.execute(
                "SELECT * FROM title_health WHERE requested_at > checked_at AND checked_at < ? "
                "ORDER BY requested_at LIMIT 1",
                (now - REPORT_RECHECK,),
            ).fetchone()
        if not row:
            row = conn.execute(
                "SELECT * FROM title_health WHERE (status = ? AND checked_at < ?) "
                "OR (status = ? AND checked_at < ?) ORDER BY checked_at LIMIT 1",
                (OK, now - OK_RECHECK, BROKEN, now - BROKEN_RECHECK),
            ).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def record(key: str, status: str, reason: Optional[str], source: Optional[str]) -> None:
    conn = db.get_connection()
    try:
        conn.execute(
            "UPDATE title_health SET status = ?, reason = ?, source = ?, checked_at = ? WHERE key = ?",
            (status, reason, source, time.time(), key),
        )
        conn.commit()
    finally:
        conn.close()


def summary() -> Dict[str, Any]:
    conn = db.get_connection()
    try:
        counts = {
            r["status"]: r["n"]
            for r in conn.execute("SELECT status, COUNT(*) AS n FROM title_health GROUP BY status")
        }
        broken = [
            {
                "title": r["title"],
                "reason": r["reason"],
                "checked_at": r["checked_at"],
                "reader_reports": r["reports"],
            }
            for r in conn.execute(
                "SELECT title, reason, checked_at, reports FROM title_health "
                "WHERE status = ? ORDER BY reports DESC, seen_at DESC LIMIT 200",
                (BROKEN,),
            )
        ]
    finally:
        conn.close()
    return {
        "ok": counts.get(OK, 0),
        "broken": counts.get(BROKEN, 0),
        "pending": counts.get(PENDING, 0),
        "broken_titles": broken,
    }


async def check(title: str, alts: Sequence[str], find: Find, scrape: Scrape, image_ok: ImageOk):
    """(status, reason, source) for one title, following the reader's own
    path: the best source and, as the reader does, its alternates in turn.
    The newest chapter is tried first, then the one before it, since a site
    often lists a chapter before its pages are up."""
    found = await find(title, alts)
    if not found or not found.get("chapters"):
        return BROKEN, "Not found on any source", None

    candidates = [found] + list(found.get("alternates") or [])
    tried = []
    for source in candidates[:SOURCES_TO_TRY]:
        domain = source.get("domain") or "?"
        chapters = source.get("chapters") or []
        problem = "no chapters listed"
        for chapter in list(reversed(chapters))[:2]:
            payload = await scrape(chapter["url"])
            images = (payload or {}).get("images") or []
            if not images:
                problem = "chapter pages didn't load"
                continue
            if await image_ok(images[0]):
                return OK, None, domain
            problem = "page images didn't load"
        tried.append(f"{domain}: {problem}")
    return BROKEN, "; ".join(tried), None


_task: Optional[asyncio.Task] = None


async def _run_forever(find: Find, scrape: Scrape, image_ok: ImageOk) -> None:
    await asyncio.sleep(30)  # let the server finish starting
    while True:
        due = None
        try:
            due = await asyncio.to_thread(next_due)
        except Exception as e:
            print(f"[health] queue read failed: {e}")
        if not due:
            await asyncio.sleep(IDLE_PAUSE)
            continue
        try:
            alts = json.loads(due.get("alts") or "[]")
            status, reason, source = await asyncio.wait_for(
                check(due["title"], alts, find, scrape, image_ok), CHECK_TIMEOUT
            )
        except asyncio.TimeoutError:
            # Too slow to call either way; keep any earlier verdict and
            # move on. A never-checked title stays pending until the next try.
            status = due["status"] if due["status"] != PENDING else BROKEN
            reason, source = "Check timed out", due.get("source")
        except Exception as e:
            print(f"[health] check failed for {due['title']!r}: {e}")
            status = due["status"] if due["status"] != PENDING else BROKEN
            reason, source = "Check failed", due.get("source")
        try:
            await asyncio.to_thread(record, due["key"], status, reason, source)
        except Exception as e:
            print(f"[health] couldn't save result for {due['title']!r}: {e}")
        await asyncio.sleep(CHECK_PAUSE)


def start(find: Find, scrape: Scrape, image_ok: ImageOk) -> None:
    global _task
    if _task is None or _task.done():
        _task = asyncio.ensure_future(_run_forever(find, scrape, image_ok))


def stop() -> None:
    global _task
    if _task is not None:
        _task.cancel()
        _task = None
