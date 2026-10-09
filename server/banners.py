"""Scanlation banners and notices at either end of a chapter.

Groups paste their banner (logo, Discord, "brought to you by", "read at")
onto the top of a chapter's first image or the bottom of its last, and
some copies open and close with a notice of their own ("WARNING!! Read
only at ..."). Where one is a page of its own, scrapers/core.py drops it
by shape; where a site cuts every chapter into equal pieces (comizy), it
shares images with the story, can run across two of them, and only the
banner itself may go.

What gives a banner away is that it repeats, where story art never does.
Each end of a chapter is read as a strip: its first (or last) few images,
shrunk to PROFILE_W columns and stacked. The banner is the stretch of the
strip, from the edge, that is the same as:
  - the other end of the same chapter (a notice put at both ends),
  - a banner already known (promo_notices),
  - the same end of a chapter next to this one (by the chapter number in
    the address), or
  - the same end of any chapter read before from the same site
    (notice_candidates).
What's found is kept in promo_notices, so a group's banner is recognised
at once on every chapter after that, of any series.

The reader asks /api/trim after showing a chapter and hides what it names.
"""
import asyncio
import base64
import io
import re
import threading
import time
from collections import OrderedDict
from typing import Any, Awaitable, Callable, Dict, List, Optional, Tuple
from urllib.parse import urlparse

import httpx

from . import db, storyteller

PROFILE_W = 32           # a strip row is a band of an image, PROFILE_W values across
ROWS_PER_WIDTH = 64      # bands an image-width tall (a band is 12px of an 800px-wide image)
STRIP_ROWS = 192         # how much of each end is read: three image-widths
STRIP_IMAGES = 4         # and from no more images than this
ROW_DIFF = 16            # bands whose grey levels differ less than this on average are the same
SPREAD_SHARE = 0.5       # and less than this share of how much the bands vary across
UNIFORM = 12             # a band varying less than this across is blank (or a flat colour)
MIN_ROWS = 10            # a shorter match is a panel edge, not a banner
MIN_TEXTURED = 6         # and a banner has this many bands with something in them
GAP_ROWS = 4             # this many blank bands is the gap after a banner...
GO_ON_ROWS = 6           # ...and the match only goes past it if this many bands with something in them match after
BOTH_ENDS_SHARE = 0.8    # a notice at both ends: this share of its bands the same (the copies are resized)
NEIGHBOURS = 2           # chapters either side checked for a repeat
CANDIDATES = 2000        # chapter ends remembered, to spot a repeat later
CACHE_SIZE = 3000
STRIP_CACHE = 200
RECHECK_AFTER = 600      # seconds before a chapter with no banner found is looked at again

Scrape = Callable[[str], Awaitable[Optional[Dict[str, Any]]]]
Rows = List[bytes]
# (image index, first strip row, rows, width, height) for each image in a strip
Segments = List[Tuple[int, int, int, int, int]]

_lock = threading.Lock()
_known: List[Rows] = []
_candidates: "OrderedDict[str, Tuple[Rows, Rows]]" = OrderedDict()   # chapter url -> (head, tail)
_loaded = False
_results: Dict[str, Tuple[float, Dict[str, Any]]] = {}   # chapter url -> (when, result)
_strips: "OrderedDict[Tuple[str, bool], Optional[Tuple[Rows, Segments]]]" = OrderedDict()
_busy = asyncio.Semaphore(2)   # decoding images is the heaviest thing the free server does
_added = 0


# ---- strip rows ---------------------------------------------------------

def _pack(rows: Rows) -> str:
    return base64.b64encode(b"".join(rows)).decode()


def _unpack(text: str) -> Rows:
    data = base64.b64decode(text)
    return [data[i:i + PROFILE_W] for i in range(0, len(data), PROFILE_W)]


def _diff(a: bytes, b: bytes) -> float:
    return sum(abs(x - y) for x, y in zip(a, b)) / PROFILE_W


def _spread(row: bytes) -> float:
    mean = sum(row) / PROFILE_W
    return sum(abs(v - mean) for v in row) / PROFILE_W


def _same(a: bytes, b: bytes) -> bool:
    """Two bands are the same picture: close on average, and closer still
    for bands with little in them (a few marks on white look like any
    other few marks on white)."""
    d = _diff(a, b)
    return d < ROW_DIFF and d < SPREAD_SHARE * (_spread(a) + _spread(b)) / 2 + 3


def _textured(row: bytes) -> bool:
    return max(row) - min(row) >= UNIFORM


def image_rows(image) -> Rows:
    """The image shrunk to PROFILE_W columns (keeping its shape), row by row."""
    w, h = image.size
    rows = max(1, round(h * ROWS_PER_WIDTH / w))
    small = image.convert("L").resize((PROFILE_W, rows), resample=2)   # bilinear: averages bands
    data = small.tobytes()
    return [data[i * PROFILE_W:(i + 1) * PROFILE_W] for i in range(rows)]


def _run(a: Rows, b: Rows) -> int:
    """How many rows from the start a and b are the same for. A row may
    line up one off (each image is shrunk on its own), and one odd row is
    let through: two in a row end it."""
    i, misses, length = 0, 0, 0
    n = min(len(a), len(b))
    while i < n:
        same = any(0 <= j < len(b) and _same(a[i], b[j]) for j in (i, i - 1, i + 1))
        if same:
            misses = 0
            length = i + 1
        else:
            misses += 1
            if misses == 2:
                break
        i += 1
    return length


def _stop_at_gap(a: Rows, length: int) -> int:
    """A banner ends at a blank gap: past one, the match only counts if
    enough of what follows matches too (not just the first line of the
    story looking like the other's)."""
    i = 0
    while i < length:
        if _textured(a[i]):
            i += 1
            continue
        start = i
        while i < length and not _textured(a[i]):
            i += 1
        if i - start >= GAP_ROWS and sum(_textured(r) for r in a[i:length]) < GO_ON_ROWS:
            return start
    return length


def match_length(a: Rows, b: Rows) -> int:
    """_run up to any gap it shouldn't cross, less the blank rows it ends on."""
    length = _stop_at_gap(a, _run(a, b))
    while length and not (_textured(a[length - 1]) and _textured(b[min(length - 1, len(b) - 1)])):
        length -= 1
    # The banner's last band or two, half banner and half gap, rarely
    # match: take them too when a gap follows.
    for extra in (1, 2):
        edge = a[length:length + extra]
        gap = a[length + extra:length + extra + GAP_ROWS]
        if (length and all(_textured(r) for r in edge)
                and len(gap) == GAP_ROWS and not any(_textured(r) for r in gap)):
            return length + extra
    return length


def _banner_like(rows: Rows, length: int) -> bool:
    return length >= MIN_ROWS and sum(_textured(r) for r in rows[:length]) >= MIN_TEXTURED


def shared_start(a: Rows, b: Rows) -> int:
    """Rows at the start of a that b starts with too: a banner, if it's
    banner-like and doesn't simply run on to where either strip ends (two
    copies of one chapter)."""
    length = match_length(a, b)
    if not _banner_like(a, length) or length >= min(len(a), len(b)) - 1:
        return 0
    return length


def known_start(rows: Rows, notice: Rows) -> int:
    """Rows at the start of `rows` that are the whole of a known notice."""
    length = match_length(rows, notice)
    return length if length >= len(notice) - 2 and _banner_like(rows, length) else 0


def both_ends(head: Rows, tail: Rows) -> int:
    """Rows at the start of head that are also the very end of tail (in
    the same order): a notice the chapter opens and closes with. The two
    copies are often resized differently, so most rows matching is
    enough. 0 if none."""
    best, best_share = 0, 0.0
    for length in range(MIN_ROWS, min(len(head) - 1, len(tail)) + 1):
        end = tail[len(tail) - length:]

        def same(i):
            return any(0 <= j < length and _same(head[i], end[j]) for j in (i, i - 1, i + 1))

        if not all(same(i) for i in range(4)):
            continue   # the start doesn't line up: not this length
        # Only bands with something in them count: blank ones match anything blank.
        filled = [i for i in range(length) if _textured(head[i])]
        if len(filled) < 2 * MIN_TEXTURED or not all(same(i) for i in range(length) if not _textured(head[i])):
            continue
        share = sum(same(i) for i in filled) / len(filled)
        if share >= BOTH_ENDS_SHARE and share >= best_share:
            best, best_share = length, share
    return best


def _cut(segments: Segments, length: int) -> Optional[Dict[str, int]]:
    """Strip rows -> {"skip": whole images to hide, "px": rows to clip off
    the next one, "width", "height"} (images counted from the edge)."""
    if not length:
        return None
    for k, (_, first, rows, w, h) in enumerate(segments):
        if length < first + rows:
            if length > first and k + 1 < len(segments) and abs(segments[k + 1][3] - w) > 0.05 * w:
                # Not the width of what follows: a page of its own (a credits
                # page), not a piece of the strip. It goes whole.
                return {"skip": k + 1, "px": 0, "width": segments[k + 1][3], "height": segments[k + 1][4]}
            return {"skip": k, "px": round((length - first) * h / rows), "width": w, "height": h}
    _, _, _, w, h = segments[-1]
    return {"skip": len(segments) - 1, "px": h, "width": w, "height": h}


# ---- memory -------------------------------------------------------------

def _load() -> None:
    global _loaded
    if _loaded:
        return
    conn = db.get_connection()
    try:
        known = conn.execute("SELECT profile FROM promo_notices").fetchall()
        seen = conn.execute("SELECT chapter_url, head, tail FROM notice_candidates "
                            "ORDER BY created_at DESC LIMIT ?", (CANDIDATES,)).fetchall()
    finally:
        conn.close()
    with _lock:
        _known[:] = [_unpack(r[0]) for r in known]
        _candidates.clear()
        for url, head, tail in reversed(seen):
            _candidates[url] = (_unpack(head), _unpack(tail))
        _loaded = True


def _remember(notice: Rows) -> None:
    with _lock:
        if any(known_start(notice, k) for k in _known):
            return
        _known.append(notice)
    conn = db.get_connection()
    try:
        conn.execute("INSERT OR IGNORE INTO promo_notices (profile, created_at) VALUES (?, ?)",
                     (_pack(notice), time.time()))
        conn.commit()
    finally:
        conn.close()


def _note(chapter_url: str, head: Rows, tail: Rows) -> None:
    """Remember a chapter's ends, to recognise a banner on them later."""
    global _added
    with _lock:
        _candidates.pop(chapter_url, None)
        _candidates[chapter_url] = (head, tail)
        while len(_candidates) > CANDIDATES:
            _candidates.popitem(last=False)
        _added += 1
        prune = _added >= 100
        if prune:
            _added = 0
    conn = db.get_connection()
    try:
        conn.execute("INSERT OR REPLACE INTO notice_candidates (chapter_url, head, tail, created_at) "
                     "VALUES (?, ?, ?, ?)", (chapter_url, _pack(head), _pack(tail), time.time()))
        if prune:
            conn.execute("DELETE FROM notice_candidates WHERE chapter_url NOT IN (SELECT chapter_url "
                         "FROM notice_candidates ORDER BY created_at DESC LIMIT ?)", (CANDIDATES,))
        conn.commit()
    finally:
        conn.close()


def _host(url: str) -> str:
    return (urlparse(url).hostname or "").lower()


def _same_site_ends(chapter_url: str) -> List[Tuple[Rows, Rows]]:
    """Ends of chapters read before on the same site. Only the same site:
    the same chapter from another site shares its story art."""
    host = _host(chapter_url)
    with _lock:
        return [ends for url, ends in _candidates.items() if url != chapter_url and _host(url) == host]


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


# ---- reading chapters ---------------------------------------------------

async def _strip(client, scrape: Scrape, chapter_url: str, last: bool) -> Optional[Tuple[Rows, Segments]]:
    """One end of a chapter as strip rows, in order from that edge (the
    last end reversed, so both are matched from the edge in)."""
    key = (chapter_url, last)
    if key in _strips:
        _strips.move_to_end(key)
        return _strips[key]
    from PIL import Image

    try:
        chapter = await scrape(chapter_url)
    except Exception:
        chapter = None
    images = (chapter or {}).get("images") or []
    result = None
    if len(images) >= 2:
        order = list(range(len(images) - 1, -1, -1)) if last else list(range(len(images)))
        rows: Rows = []
        segments: Segments = []
        for index in order[:STRIP_IMAGES]:
            data = await storyteller.fetch_image(client, images[index], chapter_url)
            if not data:
                break
            try:
                image = Image.open(io.BytesIO(data))
                image.load()
            except Exception:
                break
            part = image_rows(image)
            segments.append((index, len(rows), len(part), image.width, image.height))
            rows.extend(reversed(part) if last else part)
            if len(rows) >= STRIP_ROWS:
                break
        if segments:
            result = (rows[:STRIP_ROWS], segments)
    _strips[key] = result
    while len(_strips) > STRIP_CACHE:
        _strips.popitem(last=False)
    return result


def _from_known(rows: Rows, last: bool) -> int:
    with _lock:
        notices = list(_known)
    best = 0
    for notice in notices:
        best = max(best, known_start(rows, list(reversed(notice)) if last else notice))
    return best


def _as_notice(rows: Rows, length: int, last: bool) -> Rows:
    """Matched strip rows as a notice, top to bottom."""
    part = rows[:length]
    return list(reversed(part)) if last else part


async def _find_end(client, scrape: Scrape, chapter_url: str, rows: Rows, last: bool) -> int:
    length = _from_known(rows, last)
    if length:
        return length
    for head, tail in _same_site_ends(chapter_url):
        length = shared_start(rows, tail if last else head)
        if length:
            await asyncio.to_thread(_remember, _as_notice(rows, length, last))
            return length
    for other in neighbour_urls(chapter_url):
        theirs = await _strip(client, scrape, other, last)
        if not theirs:
            continue
        length = shared_start(rows, theirs[0])
        if length:
            await asyncio.to_thread(_remember, _as_notice(rows, length, last))
            return length
    return 0


async def _find(client, scrape: Scrape, chapter_url: str) -> Dict[str, Any]:
    head = await _strip(client, scrape, chapter_url, last=False)
    tail = await _strip(client, scrape, chapter_url, last=True)
    if not head or not tail:
        return {"top": None, "bottom": None}
    head_rows, head_segments = head
    tail_rows, tail_segments = tail
    top = bottom = 0
    # The same notice at both ends: no need to look anywhere else.
    both = both_ends(head_rows, list(reversed(tail_rows)))
    if both:
        top = bottom = both
        await asyncio.to_thread(_remember, head_rows[:both])
    else:
        top = await _find_end(client, scrape, chapter_url, head_rows, last=False)
        bottom = await _find_end(client, scrape, chapter_url, tail_rows, last=True)
    await asyncio.to_thread(_note, chapter_url, head_rows, tail_rows)
    result = {"top": _cut(head_segments, top), "bottom": _cut(tail_segments, bottom)}
    # A short chapter: never let the two cuts meet.
    count = len((await scrape(chapter_url) or {}).get("images") or [])
    if result["top"] and result["bottom"] and result["top"]["skip"] + result["bottom"]["skip"] + 2 > count:
        result["bottom"] = None
    return result


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
    """{"top": {"skip", "px", "width", "height"} or None, "bottom": ...}:
    how many whole images at that end are banner, and how many rows of
    the next one."""
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
            result = await _find(client, scrape, chapter_url)
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
    _strips.clear()
