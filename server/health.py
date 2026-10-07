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

The live list is kept in this process's memory: every page that shows
titles asks about them, and the database (Turso, a network round trip per
statement) is far too slow for that. Changes are written to the
title_health table in the background and read back at startup, so a
restart doesn't forget what's been checked.
"""

import asyncio
import json
import threading
import time
from typing import Any, Awaitable, Callable, Dict, List, Optional, Sequence

from . import db

PENDING = "pending"
OK = "ok"
BROKEN = "broken"

OK_RECHECK = 12 * 3600        # a working title is re-verified twice a day
BROKEN_RECHECK = 3 * 3600     # a broken one may have been fixed upstream
REPORT_RECHECK = 10 * 60      # a reader hit a failure: look again soon
CHECK_PAUSE = 30              # seconds between checks
IDLE_PAUSE = 60               # nothing due: look again in a minute
CHECK_TIMEOUT = 180           # one title's whole check
SAVE_EVERY = 30               # seconds between writes to the database
MAX_TITLES_PER_REQUEST = 120
MAX_QUEUED = 600              # unchecked titles accepted before new ones wait
MAX_TITLE_LENGTH = 200
MAX_ALTS = 6
SOURCES_TO_TRY = 4            # best source plus up to 3 alternates
FORGET_AFTER = 30 * 86400     # not shown for a month: stop re-checking

COLUMNS = ("key", "title", "alts", "status", "reason", "source",
           "checked_at", "requested_at", "seen_at", "reports")

Find = Callable[[str, Sequence[str]], Awaitable[Optional[Dict[str, Any]]]]
Scrape = Callable[[str], Awaitable[Optional[Dict[str, Any]]]]
ImageOk = Callable[[str], Awaitable[bool]]

_lock = threading.Lock()
_titles: Dict[str, Dict[str, Any]] = {}
_dirty: set = set()
_loaded = False


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


def _new_entry(key: str, title: str, alts: List[str], now: float) -> Dict[str, Any]:
    return {"key": key, "title": title, "alts": alts, "status": PENDING, "reason": None,
            "source": None, "checked_at": None, "requested_at": now, "seen_at": now, "reports": 0}


def load() -> None:
    """Read saved results back from the database (once, at startup)."""
    global _loaded
    conn = db.get_connection()
    try:
        rows = conn.execute(f"SELECT {', '.join(COLUMNS)} FROM title_health").fetchall()
    finally:
        conn.close()
    cutoff = time.time() - FORGET_AFTER
    with _lock:
        for r in rows:
            entry = {c: r[c] for c in COLUMNS}
            if (entry["seen_at"] or 0) < cutoff:
                continue
            try:
                entry["alts"] = json.loads(entry["alts"] or "[]")
            except ValueError:
                entry["alts"] = []
            entry["reports"] = entry["reports"] or 0
            _titles.setdefault(entry["key"], entry)
        _loaded = True


def save() -> int:
    """Write every changed title to the database: one upsert per batch."""
    with _lock:
        keys = list(_dirty)
        _dirty.clear()
        rows = [dict(_titles[k]) for k in keys if k in _titles]
    if not rows:
        return 0
    try:
        conn = db.get_connection()
        try:
            for start in range(0, len(rows), 50):
                batch = rows[start:start + 50]
                params: List[Any] = []
                for e in batch:
                    params += [e["key"], e["title"], json.dumps(e["alts"]), e["status"], e["reason"],
                               e["source"], e["checked_at"], e["requested_at"], e["seen_at"], e["reports"]]
                conn.execute(
                    f"INSERT INTO title_health ({', '.join(COLUMNS)}) VALUES "
                    + ",".join(["(?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"] * len(batch))
                    + " ON CONFLICT(key) DO UPDATE SET title = excluded.title, alts = excluded.alts, "
                    "status = excluded.status, reason = excluded.reason, source = excluded.source, "
                    "checked_at = excluded.checked_at, requested_at = excluded.requested_at, "
                    "seen_at = excluded.seen_at, reports = excluded.reports",
                    params,
                )
            conn.commit()
        finally:
            conn.close()
    except Exception:
        with _lock:
            _dirty.update(e["key"] for e in rows)  # try again next time
        raise
    return len(rows)


def statuses(items: List[Dict[str, Any]]) -> Dict[str, str]:
    """Known status for each title ({"title", "alts"} dicts), keyed by the
    title as sent. Titles never seen before are queued for a check and
    come back "pending" (shown as normal until checked). Memory only."""
    now = time.time()
    out: Dict[str, str] = {}
    with _lock:
        queued = sum(1 for e in _titles.values() if e["status"] == PENDING)
        for item in items[:MAX_TITLES_PER_REQUEST]:
            if not isinstance(item, dict):
                continue
            cleaned = _clean(item.get("title"), item.get("alts"))
            if not cleaned:
                continue
            title, alts = cleaned
            key = key_for(title)
            entry = _titles.get(key)
            if entry:
                if entry["seen_at"] < now - 86400:  # "still shown", saved at most daily
                    entry["seen_at"] = now
                    _dirty.add(key)
                out[title] = entry["status"]
                continue
            if queued < MAX_QUEUED:
                _titles[key] = _new_entry(key, title, alts, now)
                _dirty.add(key)
                queued += 1
            out[title] = PENDING
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
    with _lock:
        entry = _titles.get(key)
        if entry is None:
            entry = _titles[key] = _new_entry(key, title, alts, now)
        entry["reports"] += 1
        entry["requested_at"] = now
        entry["seen_at"] = now
        _dirty.add(key)
    return True


def next_due() -> Optional[Dict[str, Any]]:
    """The title most in need of a check: never checked first, then ones a
    reader reported, then working/broken titles whose re-check is due."""
    now = time.time()
    with _lock:
        entries = list(_titles.values())

    def pick(candidates, by):
        return dict(min(candidates, key=by)) if candidates else None

    return (
        pick([e for e in entries if e["status"] == PENDING], lambda e: e["requested_at"])
        or pick([e for e in entries if e["checked_at"] is not None
                 and e["requested_at"] > e["checked_at"]
                 and e["checked_at"] < now - REPORT_RECHECK], lambda e: e["requested_at"])
        or pick([e for e in entries if e["checked_at"] is not None and (
                    (e["status"] == OK and e["checked_at"] < now - OK_RECHECK)
                    or (e["status"] == BROKEN and e["checked_at"] < now - BROKEN_RECHECK))],
                lambda e: e["checked_at"])
    )


def record(key: str, status: str, reason: Optional[str], source: Optional[str]) -> None:
    with _lock:
        entry = _titles.get(key)
        if entry is None:
            return
        entry.update(status=status, reason=reason, source=source, checked_at=time.time())
        _dirty.add(key)


def summary() -> Dict[str, Any]:
    with _lock:
        entries = [dict(e) for e in _titles.values()]
    counts = {s: sum(1 for e in entries if e["status"] == s) for s in (OK, BROKEN, PENDING)}
    broken = sorted((e for e in entries if e["status"] == BROKEN),
                    key=lambda e: (-e["reports"], -(e["seen_at"] or 0)))[:200]
    return {
        "ok": counts[OK],
        "broken": counts[BROKEN],
        "pending": counts[PENDING],
        "broken_titles": [
            {"title": e["title"], "reason": e["reason"], "checked_at": e["checked_at"],
             "reader_reports": e["reports"]}
            for e in broken
        ],
    }


def forget_old() -> None:
    cutoff = time.time() - FORGET_AFTER
    with _lock:
        for key in [k for k, e in _titles.items() if (e["seen_at"] or 0) < cutoff]:
            del _titles[key]
            _dirty.discard(key)


def reset() -> None:
    """Forget everything in memory (tests)."""
    global _loaded
    with _lock:
        _titles.clear()
        _dirty.clear()
        _loaded = False


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


async def _save_quietly() -> None:
    try:
        await asyncio.to_thread(save)
    except Exception as e:
        print(f"[health] save failed (will retry): {e}")


async def _run_forever(find: Find, scrape: Scrape, image_ok: ImageOk) -> None:
    if not _loaded:
        try:
            await asyncio.to_thread(load)
        except Exception as e:
            print(f"[health] couldn't load saved results: {e}")
    await asyncio.sleep(30)  # let the server finish starting
    last_save = time.time()
    while True:
        if time.time() - last_save >= SAVE_EVERY:
            await _save_quietly()
            forget_old()
            last_save = time.time()
        due = next_due()
        if not due:
            await asyncio.sleep(IDLE_PAUSE)
            continue
        try:
            status, reason, source = await asyncio.wait_for(
                check(due["title"], due["alts"], find, scrape, image_ok), CHECK_TIMEOUT
            )
        except asyncio.TimeoutError:
            # Too slow to call either way; keep any earlier verdict. A title
            # never checked before counts as broken for now (a reader would
            # have given up too) and is looked at again in a few hours.
            status = due["status"] if due["status"] != PENDING else BROKEN
            reason, source = "Check timed out", due.get("source")
        except Exception as e:
            print(f"[health] check failed for {due['title']!r}: {e}")
            status = due["status"] if due["status"] != PENDING else BROKEN
            reason, source = "Check failed", due.get("source")
        record(due["key"], status, reason, source)
        await asyncio.sleep(CHECK_PAUSE)


def start(find: Find, scrape: Scrape, image_ok: ImageOk) -> None:
    global _task
    if _task is None or _task.done():
        _task = asyncio.ensure_future(_run_forever(find, scrape, image_ok))


async def stop() -> None:
    global _task
    if _task is not None:
        _task.cancel()
        _task = None
    await _save_quietly()
