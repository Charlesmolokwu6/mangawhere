"""YouTube community posts ("youtube.com/post/<id>"), for the "paste a link"
search flow.

A post isn't a video: oEmbed and yt-dlp know nothing about it, so the
video path can only fail on one. Recommendation channels post lists
there ("1. Chronicles of Runes 2. L.A.G 3. Hand Jumper ..."), so this
reads the post's text from its page and pulls out the titles it lists.
"""
import asyncio
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
            if text or post.get("backstageAttachment"):
                return {"text": text, "author": author, "images": post_images(post)}
    meta = _OG_DESCRIPTION.search(html)
    if meta:
        import html as html_lib

        return {"text": html_lib.unescape(meta.group(1)), "author": "", "images": []}
    return {"text": "", "author": "", "images": []}


# ---- pictures ----
#
# Recommendation posts often say only "Manhwa recommendation - Apocalyptic"
# and write each title on its picture. The app reads those pictures one at
# a time (/api/post-image-title) so names show up as they're found.

IMAGE_HOSTS = {"yt3.ggpht.com", "yt3.googleusercontent.com"}
_IMAGE_PATH = re.compile(r"^/[A-Za-z0-9_-]{20,}$")
MAX_IMAGES = 20
IMAGE_SIZE = "=s480"   # the titles are big lettering; small pictures read faster


def image_base(url: str) -> Optional[str]:
    """A YouTube post picture's address without its size suffix, or None
    for anything that isn't one."""
    parsed = urlparse(url or "")
    host = (parsed.hostname or "").lower()
    path = parsed.path.split("=", 1)[0]
    if parsed.scheme != "https" or host not in IMAGE_HOSTS or not _IMAGE_PATH.match(path):
        return None
    return f"https://{host}{path}"


def post_images(post: dict) -> List[str]:
    """The post's pictures, in order, one address each."""
    found: List[str] = []
    for node in _walk(post.get("backstageAttachment") or {}):
        image = node.get("backstageImageRenderer")
        if not isinstance(image, dict):
            continue
        thumbs = ((image.get("image") or {}).get("thumbnails")) or []
        base = image_base(thumbs[-1].get("url", "")) if thumbs else None
        if base and base not in found:
            found.append(base)
    return found[:MAX_IMAGES]


_NOT_A_TITLE = re.compile(r"\d+\s*chap|\bchapters?\b|ongoing|complet|hiatus|season|episode|@|www|\.com|subscribe", re.I)
_OCR_LOCK = asyncio.Semaphore(1)   # one picture at a time: the server's CPU is small


def title_lines(results) -> List[str]:
    """The likeliest title lines from OCR results ([box, text, score]):
    the biggest lettering first, without "95 Chapters | Ongoing" and the
    like, with the spaces OCR drops put back ("SURVIVINGTHEAPOCALYPSE")."""
    from server.storyteller import repair_word

    lines = []
    for box, text, score in results or []:
        letters = sum(c.isalpha() for c in text or "")
        if score < 0.75 or not letters or sum(c.isalnum() for c in text) < 3 or _NOT_A_TITLE.search(text):
            continue
        height = abs(box[3][1] - box[0][1])
        words = [repair_word(t.lower()) for t in re.split(r"[^A-Za-z0-9']+", text) if t]
        name = " ".join(w for w in words if w)
        if name:
            lines.append((height, name))
    lines.sort(key=lambda l: -l[0])
    return [name for _, name in lines[:3]]


def _ocr(image: bytes):
    from server.storyteller import _get_ocr

    engine = _get_ocr()
    if engine is None:
        raise RuntimeError("OCR unavailable")
    results, _ = engine(image)
    return results


async def title_from_image(url: str) -> Dict[str, object]:
    """{"title", "id"} when a line on the picture is a title AniList knows,
    {"guess": text} for the biggest lettering when it isn't, {} for a
    picture with no lettering."""
    from scrapers.titlecheck import display_title, matches, search_many
    from server import cache

    base = image_base(url)
    if not base:
        return {}

    async def produce():
        async with httpx.AsyncClient(timeout=20.0) as client:
            r = await client.get(base + IMAGE_SIZE)
            r.raise_for_status()
            async with _OCR_LOCK:
                results = await asyncio.to_thread(_ocr, r.content)
            names = title_lines(results)
            for name, options in zip(names, await search_many(client, names)):
                hit = next((m for m in options if matches(name, m)), None)
                if hit:
                    return {"title": display_title(hit), "id": hit["id"]}
        return {"guess": names[0].title()} if names else {"none": True}

    return await cache.cached("postimg", base, 30 * 24 * 3600, produce, miss_ttl=600)


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
    return {"text": post["text"], "author": post["author"], "titles": list_titles(post["text"]),
            "images": post.get("images") or []}
