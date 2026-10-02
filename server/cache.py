"""Shared cache for scraper results, so one fetch serves every reader.

Without this, every reader who opens a title makes this server search all
the source sites again (20-40s each time) — at scale that's slow for
readers, expensive, and exactly the traffic pattern that gets this
server's IP blocked by those sites. With it, a title is looked up once per
TTL no matter how many people open it.

Two layers:
- an in-process dict, so a hot title costs nothing at all;
- the `scrape_cache` table in the app's database, so the result survives
  restarts and — when Turso is configured — is shared by every server
  instance.

Concurrent requests for the same key while it's being fetched all wait on
that one fetch ("single flight") instead of each starting their own.
"""
import asyncio
import hashlib
import json
import time
from collections import OrderedDict
from typing import Any, Awaitable, Callable, Dict, Optional, Tuple

from server import db

MEMORY_ENTRIES = 2000

_memory: "OrderedDict[str, Tuple[float, Any]]" = OrderedDict()
_in_flight: Dict[str, "asyncio.Future"] = {}


def _key(namespace: str, raw: str) -> str:
    digest = hashlib.sha1(raw.strip().lower().encode()).hexdigest()
    return f"{namespace}:{digest}"


def _memory_get(key: str) -> Tuple[bool, Any]:
    entry = _memory.get(key)
    if not entry:
        return False, None
    expires_at, value = entry
    if expires_at < time.time():
        _memory.pop(key, None)
        return False, None
    _memory.move_to_end(key)
    return True, value


def _memory_set(key: str, value: Any, expires_at: float) -> None:
    _memory[key] = (expires_at, value)
    _memory.move_to_end(key)
    while len(_memory) > MEMORY_ENTRIES:
        _memory.popitem(last=False)


def _db_get(key: str) -> Tuple[bool, Any]:
    conn = db.get_connection()
    try:
        row = conn.execute(
            "SELECT value, expires_at FROM scrape_cache WHERE key = ?", (key,)
        ).fetchone()
    finally:
        conn.close()
    if not row or row["expires_at"] < time.time():
        return False, None
    return True, (row["expires_at"], json.loads(row["value"]))


def _db_set(key: str, value: Any, expires_at: float) -> None:
    conn = db.get_connection()
    try:
        conn.execute(
            "INSERT OR REPLACE INTO scrape_cache (key, value, expires_at) VALUES (?, ?, ?)",
            (key, json.dumps(value), expires_at),
        )
        conn.commit()
    finally:
        conn.close()


async def cached(
    namespace: str,
    raw_key: str,
    ttl: float,
    produce: Callable[[], Awaitable[Any]],
    *,
    keep: Callable[[Any], bool] = bool,
    miss_ttl: Optional[float] = None,
) -> Any:
    """Return the cached value for (namespace, raw_key), or run `produce()`
    once to fill it. A result `keep` rejects (empty by default — a failed
    or blocked scrape) is cached only for `miss_ttl` seconds, if given, so
    a site that's down is retried soon rather than remembered as empty.
    Exceptions from `produce` are never cached."""
    key = _key(namespace, raw_key)

    hit, value = _memory_get(key)
    if hit:
        return value
    try:
        hit, stored = await asyncio.to_thread(_db_get, key)
    except Exception as e:  # cache trouble must never break a lookup
        print(f"[cache] read failed for {namespace}: {e}")
        hit = False
    if hit:
        expires_at, value = stored
        _memory_set(key, value, expires_at)
        return value

    if key in _in_flight:
        return await asyncio.shield(_in_flight[key])

    future = asyncio.get_running_loop().create_future()
    _in_flight[key] = future
    try:
        value = await produce()
    except BaseException as e:
        future.set_exception(e)
        future.exception()  # mark retrieved, so a waiter-less failure isn't logged as unhandled
        raise
    finally:
        _in_flight.pop(key, None)

    lifetime = ttl if keep(value) else miss_ttl
    if lifetime:
        expires_at = time.time() + lifetime
        _memory_set(key, value, expires_at)
        try:
            await asyncio.to_thread(_db_set, key, value, expires_at)
        except Exception as e:
            print(f"[cache] write failed for {namespace}: {e}")
    future.set_result(value)
    return value


def clear_memory() -> None:
    _memory.clear()
