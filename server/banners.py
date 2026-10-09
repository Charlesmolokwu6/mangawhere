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
from collections import deque
from typing import Any, Awaitable, Callable, Deque, Dict, List, Optional, Tuple
from urllib.parse import urlparse

import httpx

from . import db, storyteller

GAP_ROWS = 40            # a blank gap at least this tall ends the block
UNIFORM = 12             # a row whose grey levels vary less than this is blank
MIN_BLOCK = 120          # shorter blocks are a line of text or a panel edge, not a banner
MAX_BLOCK_RATIO = 1.1    # a banner is wide: no taller than this times the width
MATCH_BITS = 14          # fingerprints this close (of 128 bits) are the same banner
NEIGHBOURS = 2           # chapters either side checked for a repeat
CACHE_SIZE = 3000
RECHECK_AFTER = 600      # seconds before a chapter with no banner found is looked at again
CANDIDATES = 5000        # blocks remembered from chapters read, to spot a repeat later
MAX_SHAPES = 200         # known banner shapes tried on an image with no gap after its banner

Scrape = Callable[[str], Awaitable[Optional[Dict[str, Any]]]]

_lock = threading.Lock()
_known: List[Tuple[int, Optional[float]]] = []   # (fingerprint, block height / width)
_candidates: Deque[Tuple[int, str]] = deque(maxlen=CANDIDATES)   # (fingerprint, chapter url)
_loaded = False
_results: Dict[str, Tuple[float, Dict[str, Any]]] = {}   # chapter url -> (when, result)
_busy = asyncio.Semaphore(2)   # decoding images is the heaviest thing the free server does
_added = 0


def _close(a: int, b: int) -> bool:
    return bin(a ^ b).count("1") <= MATCH_BITS


def _load() -> None:
    global _loaded
    if _loaded:
        return
    conn = db.get_connection()
    try:
        known = conn.execute("SELECT hash FROM promo_banners").fetchall()
        seen = conn.execute("SELECT hash, chapter_url FROM banner_candidates "
                            "ORDER BY created_at DESC LIMIT ?", (CANDIDATES,)).fetchall()
    finally:
        conn.close()
    with _lock:
        _known[:] = []
        for (text,) in ((r[0],) for r in known):
            mark, _, shape = text.partition(":")
            _known.append((int(mark, 16), float(shape) if shape else None))
        _candidates.clear()
        _candidates.extend((int(r[0], 16), r[1]) for r in reversed(seen))
        _loaded = True


def _remember(fingerprint: int, shape: float) -> None:
    """Keep a banner, stored as "<hash>:<height/width>"."""
    with _lock:
        if any(_close(fingerprint, k) and s is not None for k, s in _known):
            return
        _known.append((fingerprint, shape))
    conn = db.get_connection()
    try:
        conn.execute("INSERT OR IGNORE INTO promo_banners (hash, created_at) VALUES (?, ?)",
                     (f"{fingerprint:032x}:{shape:.4f}", time.time()))
        conn.commit()
    finally:
        conn.close()


def _note(marks: List[int], chapter_url: str) -> None:
    """Remember a chapter's edge blocks, to recognise them if they repeat."""
    global _added
    with _lock:
        _candidates.extend((m, chapter_url) for m in marks)
        _added += len(marks)
        prune = _added >= 200
        if prune:
            _added = 0
    conn = db.get_connection()
    try:
        for m in marks:
            conn.execute("INSERT OR REPLACE INTO banner_candidates (hash, chapter_url, created_at) "
                         "VALUES (?, ?, ?)", (f"{m:032x}", chapter_url, time.time()))
        if prune:
            conn.execute("DELETE FROM banner_candidates WHERE hash NOT IN (SELECT hash FROM "
                         "banner_candidates ORDER BY created_at DESC LIMIT ?)", (CANDIDATES,))
        conn.commit()
    finally:
        conn.close()


def is_known(fingerprint: int) -> bool:
    with _lock:
        return any(_close(fingerprint, k) for k, _ in _known)


def _host(url: str) -> str:
    return (urlparse(url).hostname or "").lower()


def seen_elsewhere(fingerprint: int, chapter_url: str) -> bool:
    """Whether another chapter on the same site, of any series, had this
    block. Only the same site: the same chapter from another site shares
    its story art, and must not count as a repeat."""
    host = _host(chapter_url)
    with _lock:
        return any(_close(fingerprint, m) and url != chapter_url and _host(url) == host
                   for m, url in _candidates)


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


def blocks(image, last: bool) -> List[Tuple[int, int]]:
    """(rows from the edge, fingerprint) of each block at the image's edge
    that might be a banner: the whole image when it's banner-shaped, and
    the artwork up to the first gap."""
    out = []
    if MIN_BLOCK <= image.height <= MAX_BLOCK_RATIO * image.width:
        out.append((image.height, fingerprint(image, image.height, from_bottom=last)))
    end = edge_block(image, from_bottom=last)
    if end is not None and end < image.height:
        out.append((end, fingerprint(image, end, from_bottom=last)))
    return out


def _known_shape(image, last: bool) -> Optional[int]:
    """Rows of a known banner at the edge, found by its shape: for a
    banner the art runs straight on from, with no gap to find."""
    with _lock:
        shapes = sorted({round(s, 3) for _, s in _known if s})[:MAX_SHAPES]
    for shape in shapes:
        end = round(shape * image.width)
        if MIN_BLOCK <= end <= image.height and is_known(fingerprint(image, end, from_bottom=last)):
            return end
    return None


async def _find(client, scrape: Scrape, chapter_url: str, last: bool) -> Optional[Dict[str, int]]:
    image = await _edge_image(client, scrape, chapter_url, last)
    if image is None:
        return None
    image.load()
    mine = blocks(image, last)

    def result(end):
        return {"px": end, "width": image.width, "height": image.height}

    for end, mark in mine:
        if is_known(mark):
            # Keeps its shape too, for banners kept before shapes were.
            await asyncio.to_thread(_remember, mark, end / image.width)
            return result(end)
    end = _known_shape(image, last)
    if end is not None:
        return result(end)
    if not mine:
        return None
    for end, mark in mine:
        if seen_elsewhere(mark, chapter_url):
            await asyncio.to_thread(_remember, mark, end / image.width)
            return result(end)
    await asyncio.to_thread(_note, [m for _, m in mine], chapter_url)
    for other in neighbour_urls(chapter_url):
        theirs = await _edge_image(client, scrape, other, last)
        if theirs is None:
            continue
        theirs.load()
        their_marks = [m for _, m in blocks(theirs, last)]
        for end, mark in mine:
            if any(_close(mark, m) for m in their_marks):
                await asyncio.to_thread(_remember, mark, end / image.width)
                return result(end)
    return None


def _cached(chapter_url: str) -> Optional[Dict[str, Any]]:
    """A recent answer. One with no banner is looked at again after a
    while: the banner may have been learned since."""
    hit = _results.get(chapter_url)
    if not hit:
        return None
    when, result = hit
    if not (result["top"] or result["bottom"]) and time.time() - when > RECHECK_AFTER:
        return None
    return result


async def trim_for(chapter_url: str, scrape: Scrape) -> Dict[str, Any]:
    """{"top": {"px", "width", "height"} or None, "bottom": ...}: rows of the
    first image's top and the last image's bottom that are a banner."""
    cached = _cached(chapter_url)
    if cached is not None:
        return cached
    if not _loaded:
        try:
            await asyncio.to_thread(_load)
        except Exception as e:
            print(f"[banners] couldn't load known banners: {e!r}")
    async with _busy:
        cached = _cached(chapter_url)
        if cached is not None:
            return cached
        async with httpx.AsyncClient(timeout=20.0, follow_redirects=True) as client:
            top = await _find(client, scrape, chapter_url, last=False)
            bottom = await _find(client, scrape, chapter_url, last=True)
    result = {"top": top, "bottom": bottom}
    _results.pop(chapter_url, None)
    if len(_results) >= CACHE_SIZE:
        _results.pop(next(iter(_results)))
    _results[chapter_url] = (time.time(), result)
    return result


def reset() -> None:
    """Forget everything in memory (tests)."""
    global _loaded
    with _lock:
        _known.clear()
        _candidates.clear()
        _loaded = False
    _results.clear()
