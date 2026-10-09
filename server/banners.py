"""Scanlation banners stitched into a chapter's first or last page.

Groups paste their banner (logo, Discord, "brought to you by", "read at")
onto the top of a chapter's first image or the bottom of its last. Where
the banner is a page of its own, scrapers/core.py drops it by shape; where
a site cuts every chapter into equal pieces (comizy), it shares an image
with the story and only the banner itself may go.

A banner is the block of artwork between the image's edge and the first
blank gap. What gives it away is that it repeats: a group puts the same
banner on every chapter, often across series, where story art never
repeats. So the block is fingerprinted (a difference hash), and it's a
banner once the same fingerprint turns up on another chapter, looked for
in the chapters next to this one (by the chapter number in the address).
Known banners are kept in the promo_banners table, so a group's banner is
recognised at once on every chapter after that.

The reader asks /api/trim after showing a chapter and hides the rows it
names.
"""
import asyncio
import io
import re
import threading
import time
from typing import Any, Awaitable, Callable, Dict, List, Optional

import httpx

from . import db, storyteller

GAP_ROWS = 40            # a blank gap at least this tall ends the block
UNIFORM = 12             # a row whose grey levels vary less than this is blank
MIN_BLOCK = 120          # shorter blocks are a line of text or a panel edge, not a banner
MAX_BLOCK_RATIO = 1.1    # a banner is wide: no taller than this times the width
MATCH_BITS = 14          # fingerprints this close (of 128 bits) are the same banner
NEIGHBOURS = 2           # chapters either side checked for a repeat
CACHE_SIZE = 3000

Scrape = Callable[[str], Awaitable[Optional[Dict[str, Any]]]]

_lock = threading.Lock()
_known: List[int] = []
_loaded = False
_results: Dict[str, Dict[str, Any]] = {}
_busy = asyncio.Semaphore(2)   # decoding images is the heaviest thing the free server does


def _load() -> None:
    global _loaded
    if _loaded:
        return
    conn = db.get_connection()
    try:
        rows = conn.execute("SELECT hash FROM promo_banners").fetchall()
    finally:
        conn.close()
    with _lock:
        _known[:] = [int(r[0], 16) for r in rows]
        _loaded = True


def _remember(fingerprint: int) -> None:
    with _lock:
        if any(bin(fingerprint ^ k).count("1") <= MATCH_BITS for k in _known):
            return
        _known.append(fingerprint)
    conn = db.get_connection()
    try:
        conn.execute("INSERT OR IGNORE INTO promo_banners (hash, created_at) VALUES (?, ?)",
                     (format(fingerprint, "032x"), time.time()))
        conn.commit()
    finally:
        conn.close()


def is_known(fingerprint: int) -> bool:
    with _lock:
        return any(bin(fingerprint ^ k).count("1") <= MATCH_BITS for k in _known)


def edge_block(image, from_bottom: bool = False) -> Optional[int]:
    """Height of the block of artwork at the image's top (or bottom), from
    the first non-blank row to the first blank gap, or None if there's no
    banner-shaped block there."""
    grey = image.convert("L")
    w, h = grey.size
    px = grey.load()
    step = max(1, w // 100)

    def blank(y):
        row = [px[x, y] for x in range(0, w, step)]
        return max(row) - min(row) < UNIFORM

    rows = range(h - 1, -1, -1) if from_bottom else range(h)
    start, run, seen = None, 0, 0
    for y in rows:
        seen += 1
        if start is None:
            if blank(y):
                if seen > h // 4:
                    return None   # a quarter of the page is blank: no banner at the edge
                continue
            start = seen - 1
            continue
        run = run + 1 if blank(y) else 0
        if run >= GAP_ROWS:
            end = seen - run
            height = end - start
            if MIN_BLOCK <= height <= MAX_BLOCK_RATIO * w:
                return end
            return None
    return None


def fingerprint(image, end: int, from_bottom: bool = False) -> int:
    """128-bit difference hash of the block: brightness steps across a
    17x8 thumbnail, so re-compressed copies of the same banner match."""
    w, h = image.size
    box = (0, h - end, w, h) if from_bottom else (0, 0, w, end)
    small = image.crop(box).convert("L").resize((17, 8))
    px = small.load()
    bits = 0
    for y in range(8):
        for x in range(16):
            bits = bits << 1 | (px[x, y] > px[x + 1, y])
    return bits


def neighbour_urls(chapter_url: str) -> List[str]:
    """The chapters either side of this one, by the last number in its
    address (comizy's /slug/chapter-49, mangaread's /chapter-49/, ...)."""
    found = list(re.finditer(r"(\d+)(?!.*\d)", chapter_url))
    if not found:
        return []
    m = found[-1]
    n = int(m.group(1))
    urls = []
    for d in range(1, NEIGHBOURS + 1):
        for k in (n - d, n + d):
            if k > 0:
                urls.append(chapter_url[:m.start()] + str(k) + chapter_url[m.end():])
    return urls


async def _edge_image(client, scrape: Scrape, chapter_url: str, last: bool):
    from PIL import Image

    try:
        chapter = await scrape(chapter_url)
    except Exception:
        return None
    images = (chapter or {}).get("images") or []
    if not images:
        return None
    data = await storyteller.fetch_image(client, images[-1 if last else 0], chapter_url)
    if not data:
        return None
    try:
        return Image.open(io.BytesIO(data))
    except Exception:
        return None


async def _find(client, scrape: Scrape, chapter_url: str, last: bool) -> Optional[Dict[str, int]]:
    image = await _edge_image(client, scrape, chapter_url, last)
    if image is None:
        return None
    end = edge_block(image, from_bottom=last)
    if end is None:
        return None
    mark = fingerprint(image, end, from_bottom=last)
    result = {"px": end, "width": image.width, "height": image.height}
    if is_known(mark):
        return result
    for other in neighbour_urls(chapter_url):
        theirs = await _edge_image(client, scrape, other, last)
        if theirs is None:
            continue
        their_end = edge_block(theirs, from_bottom=last)
        if their_end is not None and bin(fingerprint(theirs, their_end, from_bottom=last) ^ mark).count("1") <= MATCH_BITS:
            await asyncio.to_thread(_remember, mark)
            return result
    return None


async def trim_for(chapter_url: str, scrape: Scrape) -> Dict[str, Any]:
    """{"top": {"px", "width", "height"} or None, "bottom": ...}: rows of the
    first image's top and the last image's bottom that are a banner."""
    if chapter_url in _results:
        return _results[chapter_url]
    if not _loaded:
        try:
            await asyncio.to_thread(_load)
        except Exception as e:
            print(f"[banners] couldn't load known banners: {e!r}")
    async with _busy:
        if chapter_url in _results:
            return _results[chapter_url]
        async with httpx.AsyncClient(timeout=20.0, follow_redirects=True) as client:
            top = await _find(client, scrape, chapter_url, last=False)
            bottom = await _find(client, scrape, chapter_url, last=True)
    result = {"top": top, "bottom": bottom}
    if len(_results) >= CACHE_SIZE:
        _results.pop(next(iter(_results)))
    _results[chapter_url] = result
    return result


def reset() -> None:
    """Forget everything in memory (tests)."""
    global _loaded
    with _lock:
        _known.clear()
        _loaded = False
    _results.clear()
