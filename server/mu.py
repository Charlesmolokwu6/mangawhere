import re
from typing import Any, Dict, List, Optional

import httpx

MU = "https://api.mangaupdates.com/v1"
HEADERS = {"Content-Type": "application/json"}

COMIC_TYPES = ["manga", "manhwa", "manhua", "manhpa", "oel", "comic"]


def _bigrams(s: str) -> List[str]:
    s = re.sub(r"[^a-z0-9 ]", "", (s or "").lower()).strip()
    return [s[i : i + 2] for i in range(len(s) - 1)]


def similar(a: str, b: str) -> float:
    """Dice bigram coefficient, ported from index.html's similar()."""
    A, B = _bigrams(a), _bigrams(b)
    if not A or not B:
        return 1.0 if a == b else 0.0
    counts: Dict[str, int] = {}
    for g in A:
        counts[g] = counts.get(g, 0) + 1
    hits = 0
    for g in B:
        if counts.get(g, 0) > 0:
            counts[g] -= 1
            hits += 1
    return 2 * hits / (len(A) + len(B))


def is_comic(rec: Dict[str, Any]) -> bool:
    t = str(rec.get("type") or "").lower()
    if not t:
        return True
    if "novel" in t:
        return False
    if "artbook" in t or "doujin" in t:
        return False
    return True


async def _series(client: httpx.AsyncClient, series_id: Any) -> Optional[Dict[str, Any]]:
    try:
        r = await client.get(f"{MU}/series/{series_id}")
        r.raise_for_status()
        d = r.json()
    except Exception:
        return None

    if not is_comic(d):
        return None

    raw = d.get("latest_chapter")
    if raw is None and d.get("status"):
        m = re.search(r"(\d{1,4})\s*chapters?", str(d["status"]), re.I)
        if m:
            raw = m.group(1)
    try:
        n = float(raw)
    except (TypeError, ValueError):
        return None
    if not (n > 0):
        return None

    return {"chapter": n, "title": d.get("title") or ""}


async def latest_chapter(title: str) -> Optional[float]:
    """Best-effort lookup of a series' current chapter number on
    MangaUpdates, by title search. Mirrors the client-side muFetch()
    logic but without its season-merging refinement — good enough for
    "is there a new chapter" without re-implementing the full reader
    matching pipeline server-side."""
    async with httpx.AsyncClient(headers=HEADERS, timeout=15.0) as client:
        try:
            r = await client.post(f"{MU}/series/search", json={"search": title})
            r.raise_for_status()
            results = r.json().get("results") or []
        except Exception:
            return None

        scored = []
        for x in results:
            rec = x.get("record") or x
            series_id = rec.get("series_id") or rec.get("id")
            if not series_id or not is_comic(rec):
                continue
            names = [rec.get("title") or ""] + [
                a.get("title") or "" for a in (rec.get("associated") or [])
            ]
            best = max((similar(title, n) for n in names), default=0.0)
            if best >= 0.5:
                scored.append((best, series_id))

        if not scored:
            return None
        scored.sort(key=lambda x: x[0], reverse=True)

        for _, series_id in scored[:4]:
            result = await _series(client, series_id)
            if result:
                return result["chapter"]
        return None
