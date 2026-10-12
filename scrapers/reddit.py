"""Reddit posts, for the "paste a link" search flow.

Reddit is where people ask "what's the name of this manhwa?" and the answer
sits in the comments ("Primal hunter?"). This reads the post and its top
comments and pulls out the names people suggest, so the reader can tap one
instead of scrolling the thread.

Reddit's JSON is refused from server addresses (403), but each post's RSS
feed still answers, so that's what's read. It's rate-limited hard, so every
post is cached and a 429 is retried once after the wait Reddit asks for.
"""
import asyncio
import html as html_lib
import json
import re
import xml.etree.ElementTree as ET
from difflib import SequenceMatcher
from typing import Dict, List, Optional
from urllib.parse import unquote, urlparse

import httpx
from bs4 import BeautifulSoup

from server import cache

REDDIT_HOSTS = {
    "reddit.com", "www.reddit.com", "old.reddit.com", "new.reddit.com",
    "m.reddit.com", "np.reddit.com", "sh.reddit.com", "redd.it",
}
POST_ID = re.compile(r"^[a-z0-9]{4,12}$")
ANILIST = "https://graphql.anilist.co"
UA = "Mozilla/5.0 (Linux; Android 14) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0 Mobile Safari/537.36"
ATOM = "{http://www.w3.org/2005/Atom}"
POST_TTL = 30 * 60
COMMENTS = 25
MAX_SUGGESTIONS = 8
MAX_NAME = 80


class RedditBusy(Exception):
    """Reddit refused (rate limit or block); worth trying again shortly."""


def is_reddit(url: str) -> bool:
    return (urlparse(url).hostname or "").lower() in REDDIT_HOSTS


def post_id_from_url(url: str) -> Optional[str]:
    """The post id from /r/<sub>/comments/<id>/..., /comments/<id>,
    /gallery/<id> or redd.it/<id>. None for share links (/r/<sub>/s/<code>,
    see resolve_share_link) and anything else."""
    parsed = urlparse(url)
    host = (parsed.hostname or "").lower()
    if host not in REDDIT_HOSTS:
        return None
    parts = [p for p in parsed.path.split("/") if p]
    candidate = None
    if host == "redd.it" and parts:
        candidate = parts[0]
    elif "comments" in parts and parts.index("comments") + 1 < len(parts):
        candidate = parts[parts.index("comments") + 1]
    elif len(parts) >= 2 and parts[0] == "gallery":
        candidate = parts[1]
    candidate = (candidate or "").lower()
    return candidate if POST_ID.match(candidate) else None


def is_share_link(url: str) -> bool:
    parts = [p for p in urlparse(url).path.split("/") if p]
    return is_reddit(url) and len(parts) == 4 and parts[0] == "r" and parts[2] == "s"


async def resolve_share_link(client: httpx.AsyncClient, url: str) -> Optional[str]:
    """The app's Share button gives /r/<sub>/s/<code>, which redirects to
    the real post address. Follow the redirects without reading pages."""
    for _ in range(4):
        r = await client.get(url, follow_redirects=False)
        location = r.headers.get("location")
        if r.status_code not in (301, 302, 303, 307, 308) or not location:
            return None
        url = str(httpx.URL(url).join(location))
        if post_id_from_url(url):
            return url
    return None


def _text(fragment: str) -> str:
    return " ".join(BeautifulSoup(fragment or "", "html.parser").get_text(" ").split())


def _plain(node) -> str:
    """Text of a post body, one line per paragraph, inline tags kept inline."""
    if node is None:
        return ""
    for tag in node.find_all(["br"]):
        tag.replace_with("\n")
    for tag in node.find_all(["p", "li", "h1", "h2", "h3", "blockquote", "pre"]):
        tag.append("\n")
    return "\n".join(" ".join(l.split()) for l in node.get_text().splitlines() if l.strip())


def parse_feed(xml_text: str) -> Dict[str, object]:
    """The post (first entry) and its comments (the rest) from the RSS feed."""
    root = ET.fromstring(xml_text)
    entries = root.findall(f"{ATOM}entry")
    if not entries:
        return {}

    def author(entry) -> str:
        name = entry.findtext(f"{ATOM}author/{ATOM}name") or ""
        return name.replace("/u/", "").strip()

    post = entries[0]
    post_html = post.findtext(f"{ATOM}content") or ""
    soup = BeautifulSoup(post_html, "html.parser")
    body = soup.find("div", class_="md")
    category = post.find(f"{ATOM}category")
    if category is None:
        category = root.find(f"{ATOM}category")
    image = soup.find("img")
    comments = []
    for entry in entries[1:]:
        comments.append({"author": author(entry), "html": entry.findtext(f"{ATOM}content") or ""})
    return {
        "title": (post.findtext(f"{ATOM}title") or "").strip(),
        "text": _plain(body),
        "author": author(post),
        "subreddit": (category.get("label") or category.get("term") or "") if category is not None else "",
        "image": image.get("src", "") if image else "",
        "comments": comments,
    }


# ---- picking names out of comments ----
#
# Comments are mostly chat ("it's getting an anime", "Thanks so much!"), so
# the text only proposes candidates — short answers, bold or quoted names,
# linked title pages, "it's called X" — and AniList decides which are real
# titles (verify()). Only names AniList knows are shown.

_BOTS = re.compile(r"(automoderator|bot)$", re.I)
_TITLE_LINK = re.compile(
    r"(?:anilist\.co/manga/\d+/|mangadex\.org/title/[0-9a-f-]+/|myanimelist\.net/manga/\d+/|"
    r"mangaupdates\.com/series/[0-9a-z]+/|webtoons\.com/[a-z-]+/[a-z-]+/)([^/?#\s]+)", re.I)
_LEAD = re.compile(
    r"^(?:(?:yes|yeah|yep|ya|oh|ah|hmm|lol|i\s+think|i\s+believe|i'?m\s+pretty\s+sure|pretty\s+sure|"
    r"probably|maybe|could\s+be|might\s+be|looks\s+like|sounds\s+like|seems\s+like|it'?s\s+called|"
    r"it'?s\s+from|it'?s|it\s+is|its|this\s+is|that'?s|thats|that\s+is|is\s+it|is\s+this|called|"
    r"the\s+name\s+is|name\s+is|title\s+is|sauce\s+is|sauce|source|try|answer|the\s+manga\s+is|"
    r"the\s+manhwa\s+is|the\s+top\s+one\s+is|the\s+first\s+one\s+is)[\s,:\-–]+)+", re.I)
_SAID = re.compile(
    r"(?:it'?s|its|it\s+is|called|named|title\s+is|name\s+is|sauce\s+is|this\s+is|one\s+is|try|"
    r"from|manga\s+is|manhwa\s+is)\s*:?\s+[\"“'«<]*([A-Z0-9][^.!?\n\"”»>(]{1,%d})" % MAX_NAME)
_QUOTED = re.compile(r"(?:<<|«|\"|“)\s*([^\"”»<>]{2,%d}?)\s*(?:>>|»|\"|”)" % MAX_NAME)
ANSWER_WORDS = 10
MAX_CANDIDATES = 24


def _clean_name(raw: str) -> str:
    name = " ".join((raw or "").split())
    name = _LEAD.sub("", name)
    name = re.sub(r"\s*[\(\[].*$", "", name)                # "(on Webtoon)" / "[link]"
    name = re.sub(r"\s+(?:on|from)\s+(?:webtoons?|tapas|tappytoon|mangadex|asura\w*)\b.*$", "", name, flags=re.I)
    name = re.sub(r"\s+(?:ch(?:apter)?|vol(?:ume)?|ep(?:isode)?)\.?\s*\d+.*$", "", name, flags=re.I)
    return name.strip(" \"'“”‘’*_~`.,!?:;-–<>«»")


# Bold labels in info boxes bots post ("**Status:** Ongoing").
_LABELS = {"status", "chapters", "chapter", "genres", "genre", "stats", "volumes", "score", "type",
           "author", "artist", "synopsis", "summary", "links", "spoiler", "spoilers", "edit", "update",
           "note", "tldr", "sauce", "source", "name", "title"}


def _good(name: str) -> bool:
    return (2 <= len(name) <= MAX_NAME and "http" not in name.lower() and len(name.split()) <= 12
            and name.lower() not in _LABELS)


_ASKS = re.compile(r"^(is|are|was|what'?s|what|which|who|why|how|did|does|do|didn'?t|anyone|anybody|"
                   r"can|could|you|where|when)\b", re.I)
_CHAT = re.compile(r"\b(i|i'?m|i'?ve|you|you'?re|he|he'?s|she|she'?s|they|we|we'?re|is|are|was|were|did|"
                   r"do|does|don'?t|didn'?t|not|be|been|have|has|had|seems?|looks?|got|get|getting|just|"
                   r"so|lol|lmao|bro|me|my|an|anime|about|please|thanks?|thank|manga|manhwa|manhua|"
                   r"chapters?|good|same|yes|no|idk)\b", re.I)


def _is_answer(text: str) -> bool:
    """A reply that is only a name, not a question or a remark."""
    return bool(text) and len(text.split()) <= ANSWER_WORDS and not _ASKS.match(text)


def names_in_comment(html_fragment: str) -> List[Dict[str, object]]:
    """Possible titles in one comment, as {name, strong, answer}: a linked
    title page, bold text, quoted or introduced names ("it's called X"),
    and the reply itself when it's only a name ("Primal hunter?"). Names
    of one word count only as the whole reply, bold or a link (strong);
    "called MAGA" or a quoted "talent" is just a remark."""
    found: List[tuple] = []
    soup = BeautifulSoup(html_fragment or "", "html.parser")
    for a in soup.find_all("a", href=True):
        m = _TITLE_LINK.search(a["href"])
        if m:
            found.append((unquote(m.group(1)).replace("-", " ").replace("_", " "), True))
    for tag in soup.find_all(["strong", "b"]):
        found.append((tag.get_text(" "), True))
    text = " ".join(soup.get_text(" ").split())
    answer = _clean_name(text) if _is_answer(text) else ""
    if answer:
        found.append((answer, True))
    for m in list(_QUOTED.finditer(text)) + list(_SAID.finditer(text)):
        found.append((m.group(1), False))
    names: Dict[str, Dict[str, object]] = {}
    for raw, strong in found:
        name = _clean_name(raw)
        if not _good(name):
            continue
        if not strong and len(name.split()) == 1:
            continue
        entry = names.setdefault(_key(name), {"name": name, "strong": False, "answer": False})
        entry["strong"] = entry["strong"] or strong
        entry["answer"] = entry["answer"] or (bool(answer) and _key(name) == _key(answer))
    return list(names.values())


def _key(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", (name or "").lower())


def candidates(post: Dict[str, object]) -> List[Dict[str, object]]:
    """Possible names from the comments, most backed first: a name several
    people gave beats one only one person gave, then Reddit's top order."""
    op = (post.get("author") or "").lower()
    found: Dict[str, Dict[str, object]] = {}
    for i, comment in enumerate(post.get("comments") or []):
        who = (comment.get("author") or "").lower()
        if _BOTS.search(who) and who != "roboragi":
            continue
        if who and who == op:
            continue  # the asker's replies are thanks, not answers
        for c in names_in_comment(comment.get("html") or ""):
            key = _key(c["name"])
            if not key:
                continue
            entry = found.setdefault(key, dict(c, count=0, first=i))
            entry["count"] += 1
            entry["strong"] = entry["strong"] or c["strong"]
            entry["answer"] = entry["answer"] or c["answer"]
    order = sorted(found.values(), key=lambda e: (-e["count"], not e["answer"], not e["strong"], e["first"]))
    return order[:MAX_CANDIDATES]


def _names_of(media: Dict[str, object]) -> List[str]:
    t = media.get("title") or {}
    return [n for n in [t.get("english"), t.get("romaji"), t.get("native")] + list(media.get("synonyms") or []) if n]


def matches(candidate: str, media: Dict[str, object], strong: bool = True) -> bool:
    """Whether AniList's title really is what the comment said — not just
    the nearest thing its search could find for "give it time". A name
    picked out of a sentence, or a single word, has to match exactly, and
    a single word only counts when the comment stressed it (strong)."""
    c = _key(candidate)
    one_word = len(candidate.split()) == 1
    if len(c) < 4 or (one_word and not strong):
        return False
    exact_only = one_word or not strong
    t = media.get("title") or {}
    names = [t.get("english"), t.get("romaji")] if one_word else _names_of(media)
    for name in filter(None, names):
        n = _key(name)
        if not n:
            continue
        if c == n:
            return True
        if exact_only:
            continue
        # "Primal hunter" for "The Primal Hunter"; "Atsumare fushigi
        # kenkyubu" for "Atsumare! Fushigi Kenkyuubu".
        if len(c) >= 6 and c in n and len(c) >= 0.75 * len(n):
            return True
        if len(c) >= 8 and SequenceMatcher(None, c, n).ratio() >= 0.9:
            return True
    return False


async def verify(client: httpx.AsyncClient, found: List[Dict[str, object]]) -> Dict[str, list]:
    """{"titles": [{title, id}], "maybe": [name]}: the candidates AniList
    confirms as real titles, in the same order (one request for all of
    them), and short direct answers it doesn't know (the site's own search
    looks wider than AniList) to offer only when nothing was confirmed."""
    if not found:
        return {"titles": [], "maybe": []}
    parts = []
    for i, c in enumerate(found):
        parts.append(f'c{i}:Page(perPage:3){{media(search:{json.dumps(c["name"])},type:MANGA)'
                     f'{{id title{{english romaji native}} synonyms}}}}')
    r = await client.post(ANILIST, json={"query": "query{" + " ".join(parts) + "}"},
                          headers={"Accept": "application/json"})
    r.raise_for_status()
    data = r.json().get("data") or {}
    titles: List[Dict[str, object]] = []
    maybe: List[str] = []
    seen = set()
    for i, c in enumerate(found):
        hit = None
        for media in ((data.get(f"c{i}") or {}).get("media") or []):
            if matches(c["name"], media, c["strong"]):
                hit = media
                break
        if hit is None:
            if c["answer"] and c["first"] < 5 and len(_key(c["name"])) >= 4 and not _CHAT.search(c["name"]):
                maybe.append(c["name"])
            continue
        if hit["id"] in seen:
            continue
        seen.add(hit["id"])
        t = hit.get("title") or {}
        titles.append({"title": t.get("english") or t.get("romaji") or c["name"], "id": hit["id"]})
    return {"titles": titles[:MAX_SUGGESTIONS], "maybe": maybe[:3]}


_QUESTION = re.compile(
    r"\b(sauce|source|name|names|what'?s this|what is this|(what|which) (manga|manhwa|manhua|webtoon)|"
    r"identify|help (me )?find|looking for|wcif|anyone know|anybody know|tell me|forgot|title\?)\b|\?", re.I)


def looks_like_question(post: Dict[str, object]) -> bool:
    return bool(_QUESTION.search(str(post.get("title", ""))))


async def _fetch_feed(client: httpx.AsyncClient, post_id: str) -> str:
    url = f"https://www.reddit.com/comments/{post_id}/.rss?sort=top&limit={COMMENTS}"
    for attempt in range(2):
        r = await client.get(url)
        if r.status_code == 429 and attempt == 0:
            wait = float(r.headers.get("x-ratelimit-reset") or r.headers.get("retry-after") or 5)
            await asyncio.sleep(min(max(wait, 1.0), 8.0))
            continue
        if r.status_code == 404:
            return ""
        if r.status_code in (403, 429):
            raise RedditBusy(f"reddit {r.status_code}")
        r.raise_for_status()
        return r.text
    raise RedditBusy("reddit 429")


async def analyse(client: httpx.AsyncClient, post_id: str, post: Dict[str, object]) -> Dict[str, object]:
    """What the app needs from a parsed post (parse_feed or parse_listing)."""
    return {
        "id": post_id,
        "title": post["title"],
        "text": post["text"],
        "author": post["author"],
        "subreddit": post["subreddit"],
        "image": post.get("image", ""),
        **(await verify(client, candidates(post))),
        "question": looks_like_question(post),
    }


async def lookup(url: str) -> Dict[str, object]:
    """{title, text, author, subreddit, titles, maybe, question} for a
    post, or {} when the post doesn't exist."""
    async with httpx.AsyncClient(timeout=15, headers={"User-Agent": UA}) as client:
        if not post_id_from_url(url) and is_share_link(url):
            url = await resolve_share_link(client, url) or url
        post_id = post_id_from_url(url)
        if not post_id:
            return {}

        async def produce():
            feed = await _fetch_feed(client, post_id)
            post = parse_feed(feed) if feed else {}
            return await analyse(client, post_id, post) if post else {}

        return await cache.cached("reddit", post_id, POST_TTL, produce, miss_ttl=120)


# ---- the post as the reader's phone fetched it ----
#
# Reddit refuses server addresses but answers phones, so the app first
# fetches the post's JSON itself and sends it here (/api/reddit-read) to be
# read the same way. Only text is taken from it; nothing is fetched.

def _html_of(data: Dict[str, object], html_key: str, text_key: str) -> str:
    fragment = str(data.get(html_key) or "")
    if fragment.startswith("&lt;"):
        fragment = html_lib.unescape(fragment)
    if fragment:
        return fragment
    # Markdown only: keep **bold** as bold, the rest as text.
    plain = html_lib.escape(str(data.get(text_key) or ""))
    return re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", plain)


def parse_listing(listing: object) -> Dict[str, object]:
    """The same shape as parse_feed, from /comments/<id>.json."""
    try:
        post = listing[0]["data"]["children"][0]["data"]
        children = listing[1]["data"]["children"] if len(listing) > 1 else []
    except (KeyError, IndexError, TypeError):
        return {}
    body = BeautifulSoup(_html_of(post, "selftext_html", "selftext"), "html.parser")
    comments = []
    for child in children[:COMMENTS]:
        if not isinstance(child, dict) or child.get("kind") != "t1":
            continue
        data = child.get("data") or {}
        comments.append({"author": str(data.get("author") or ""), "html": _html_of(data, "body_html", "body")})
    return {
        "id": str(post.get("id") or "").lower(),
        "title": str(post.get("title") or "").strip(),
        "text": _plain(body),
        "author": str(post.get("author") or ""),
        "subreddit": str(post.get("subreddit_name_prefixed") or ""),
        "image": "",
        "comments": comments,
    }


async def read_listing(listing: object) -> Dict[str, object]:
    post = parse_listing(listing)
    if not post or not POST_ID.match(post["id"]):
        return {}
    async with httpx.AsyncClient(timeout=15) as client:
        return await analyse(client, post["id"], post)
