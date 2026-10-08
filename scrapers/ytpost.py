"""YouTube community posts ("youtube.com/post/<id>"), for the "paste a link"
search flow.

A post isn't a video: oEmbed and yt-dlp know nothing about it, so the
video path can only fail on one. Recommendation channels post lists
there ("1. Chronicles of Runes 2. L.A.G 3. Hand Jumper ..."), so this
reads the post's text from its page and pulls out the titles it lists.
"""
import json
import re
from typing import Dict, List, Optional
from urllib.parse import parse_qs, urlparse

import httpx

YOUTUBE_HOSTS = {"www.youtube.com", "youtube.com", "m.youtube.com"}
POST_ID = re.compile(r"^[A-Za-z0-9_-]{10,64}$")
MAX_TITLES = 50
MAX_TITLE_LENGTH = 120

# "1. Title", "2) Title", "3 - Title", "- Title", "• Title"
_LIST_LINE = re.compile(r"^\s*(?:\d{1,3}\s*[.):\-–]|[-•*▪►➤✅])\s*(.+?)\s*$")
_INITIAL_DATA = re.compile(r"var ytInitialData = (\{.*?\});</script>", re.S)
_OG_DESCRIPTION = re.compile(r'<meta property="og:description" content="([^"]*)"')


def post_id_from_url(url: str) -> Optional[str]:
    """The post id from a youtube.com/post/<id> link, or from the older
    .../community?lb=<id> form. None for anything else."""
    parsed = urlparse(url)
    if (parsed.hostname or "").lower() not in YOUTUBE_HOSTS:
        return None
    parts = [p for p in parsed.path.split("/") if p]
    candidate = None
    if len(parts) >= 2 and parts[0] == "post":
        candidate = parts[1]
    elif parts and parts[-1] == "community":
        candidate = (parse_qs(parsed.query).get("lb") or [None])[0]
    return candidate if candidate and POST_ID.match(candidate) else None


def _walk(node):
    if isinstance(node, dict):
        yield node
        for value in node.values():
            yield from _walk(value)
    elif isinstance(node, list):
        for value in node:
            yield from _walk(value)


def parse_post_html(html: str) -> Dict[str, str]:
    """The post's full text and author from its page. The page's own
    description meta is cut off after ~160 characters, so the text comes
    from the embedded ytInitialData, with the meta as a fallback."""
    match = _INITIAL_DATA.search(html)
    if match:
        try:
            data = json.loads(match.group(1))
        except ValueError:
            data = None
        for node in _walk(data):
            post = node.get("backstagePostRenderer")
            if not isinstance(post, dict):
                continue
            runs = (post.get("contentText") or {}).get("runs") or []
            text = "".join(r.get("text", "") for r in runs if isinstance(r, dict))
            author_runs = (post.get("authorText") or {}).get("runs") or []
            author = "".join(r.get("text", "") for r in author_runs if isinstance(r, dict))
            if text:
                return {"text": text, "author": author}
    meta = _OG_DESCRIPTION.search(html)
    if meta:
        import html as html_lib

        return {"text": html_lib.unescape(meta.group(1)), "author": ""}
    return {"text": "", "author": ""}


def list_titles(text: str) -> List[str]:
    """The titles a post lists one per line ("1. Title", "- Title").
    Empty unless the post really is a list (two or more such lines)."""
    titles: List[str] = []
    seen = set()
    for line in (text or "").splitlines():
        match = _LIST_LINE.match(line)
        if not match:
            continue
        title = match.group(1).strip(" .,:;!-–—|")
        key = title.lower()
        if not title or len(title) > MAX_TITLE_LENGTH or key in seen:
            continue
        seen.add(key)
        titles.append(title)
        if len(titles) >= MAX_TITLES:
            break
    return titles if len(titles) >= 2 else []


async def lookup_post(post_id: str) -> Dict[str, object]:
    """Fetch a post by id (never a caller-supplied URL) and return its
    text, author and any titles it lists."""
    url = f"https://www.youtube.com/post/{post_id}"
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                      "(KHTML, like Gecko) Chrome/130.0 Safari/537.36",
        "Accept-Language": "en-US,en;q=0.9",
    }
    # SOCS skips the EU cookie-consent interstitial, which has no post in it.
    async with httpx.AsyncClient(timeout=20.0, follow_redirects=True,
                                 cookies={"SOCS": "CAI"}) as client:
        response = await client.get(url, headers=headers)
        response.raise_for_status()
    post = parse_post_html(response.text)
    return {"text": post["text"], "author": post["author"], "titles": list_titles(post["text"])}
