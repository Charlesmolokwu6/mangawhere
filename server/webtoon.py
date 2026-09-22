import html
import re
from typing import Any, Dict, List, Optional
from urllib.parse import parse_qs, urlparse

import httpx

UA = "MangaWhere/1.0 (+https://mangawhere.example)"


def _rss_url(series_url: str) -> Optional[str]:
    """.../en/action/tower-of-god/list?title_no=95 -> .../rss?title_no=95"""
    try:
        u = urlparse(series_url)
    except ValueError:
        return None
    if "webtoons.com" not in (u.netloc or ""):
        return None

    title_no = parse_qs(u.query).get("title_no", [None])[0]
    if not title_no:
        return None

    parts = [p for p in u.path.split("/") if p]
    if len(parts) < 3:
        return None

    origin = f"{u.scheme}://{u.netloc}"
    return f"{origin}/{'/'.join(parts[:3])}/rss?title_no={title_no}"


async def latest_chapter(links: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """The publisher's own chapter count, via a title's Webtoons RSS feed
    — authoritative, unlike release trackers, so this is tried before
    falling back to MangaUpdates."""
    for link in links or []:
        feed = _rss_url(link.get("url") or "")
        if not feed:
            continue
        try:
            async with httpx.AsyncClient(headers={"User-Agent": UA}, timeout=15.0) as client:
                r = await client.get(feed)
                if r.status_code >= 400:
                    continue
                xml = r.text
        except Exception:
            continue

        # Episode numbers live in each item's link — a regex beats pulling
        # in an XML parser for one attribute.
        best = 0
        for m in re.finditer(r"episode_no=(\d+)", xml):
            n = int(m.group(1))
            if n > best:
                best = n
        if best <= 0:
            continue

        url_match = re.search(rf"<link>([^<]*episode_no={best}[^<]*)</link>", xml)
        return {
            "chapter": float(best),
            "source": "WEBTOON",
            "url": html.unescape(url_match.group(1)) if url_match else "",
        }
    return None
