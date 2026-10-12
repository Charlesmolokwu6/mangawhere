"""A real web page for every title, for Google and for link previews.

The app itself is one page (index.html) that draws everything with
JavaScript, so Google sees an empty page and a link shared on WhatsApp
shows no cover or name. These pages are plain HTML built on the server
from AniList: title, cover, synopsis, genres and a "Read on MangaWhere"
button into the app (/?m=al-ID), plus links to similar titles so a search
engine can walk from one to the next. /sitemap.xml lists the most popular
titles so Google finds them without waiting for links.

Switched off until launch: with PUBLIC_SITE_URL unset (it will be the
site's own domain, e.g. https://mangawhere.com) every page says "noindex",
robots.txt keeps crawlers out of /manga/, and the sitemap doesn't exist.
The pages still render, so they can be checked before launch.
"""
import html
import json
import os
import re
from typing import Any, Dict, List, Optional

import httpx

from server import cache

ANILIST = "https://graphql.anilist.co"
PAGE_TTL = 24 * 3600            # titles change slowly; a day is plenty fresh
MISS_TTL = 600                  # an AniList hiccup is retried soon
SITEMAP_TTL = 7 * 24 * 3600
SITEMAP_FILES = 20              # 20 files x 500 titles = the top 10,000
SITEMAP_ANILIST_PAGES = 10      # AniList pages (of 50) per sitemap file
SITEMAP_PER_PAGE = 50

KIND = {"JP": "Manga", "KR": "Manhwa", "CN": "Manhua", "TW": "Manhua", "HK": "Manhua"}
STATUS = {
    "RELEASING": "Ongoing",
    "FINISHED": "Completed",
    "HIATUS": "On hiatus",
    "CANCELLED": "Cancelled",
    "NOT_YET_RELEASED": "Coming soon",
}

_MEDIA_QUERY = """
query($id:Int){Media(id:$id,type:MANGA){
  id title{english romaji native} synonyms description(asHtml:false)
  coverImage{extraLarge large} genres status chapters countryOfOrigin isAdult
  startDate{year}
  recommendations(perPage:8,sort:RATING_DESC){nodes{mediaRecommendation{
    id title{english romaji native} coverImage{large} countryOfOrigin isAdult}}}
}}"""

_SITEMAP_QUERY = """
query($page:Int,$n:Int){Page(page:$page,perPage:$n){
  media(type:MANGA,sort:POPULARITY_DESC,isAdult:false){id title{english romaji native}}
}}"""


def site_url() -> str:
    """The public address once launched, or "" while the pages are off."""
    return os.environ.get("PUBLIC_SITE_URL", "").strip().rstrip("/")


def is_live() -> bool:
    return bool(site_url())


def title_of(media: Dict[str, Any]) -> str:
    t = media.get("title") or {}
    return t.get("english") or t.get("romaji") or t.get("native") or ""


def slugify(text: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", (text or "").lower()).strip("-")
    return slug[:80].rstrip("-") or "title"


def path_for(media: Dict[str, Any]) -> str:
    return f"/manga/{media['id']}/{slugify(title_of(media))}"


def clean_synopsis(raw: Optional[str]) -> str:
    """Same tidy-up as index.html's cleanSynopsis."""
    if not raw:
        return ""
    t = re.sub(r"<br\s*/?>", "\n", raw, flags=re.I)
    t = re.sub(r"</p>", "\n", t, flags=re.I)
    t = re.sub(r"<[^>]+>", "", t)
    t = html.unescape(t)
    t = re.sub(r"\[Written by MAL Rewrite\]", "", t, flags=re.I)
    t = re.sub(r"\(Source:[^)]*\)", "", t, flags=re.I)
    t = re.sub(r"\n{3,}", "\n\n", t)
    return t.strip()


def _short(text: str, limit: int = 155) -> str:
    text = " ".join(text.split())
    if len(text) <= limit:
        return text
    return text[: limit - 1].rsplit(" ", 1)[0].rstrip(",.;:") + "…"


async def _anilist(query: str, variables: Dict[str, Any]) -> Dict[str, Any]:
    async with httpx.AsyncClient(timeout=15) as client:
        r = await client.post(
            ANILIST,
            json={"query": query, "variables": variables},
            headers={"Accept": "application/json"},
        )
    if r.status_code == 404:
        return {}
    r.raise_for_status()
    body = r.json()
    if body.get("errors"):
        # AniList answers an unknown id with a 404 error, not empty data.
        if any(e.get("status") == 404 for e in body["errors"]):
            return {}
        raise RuntimeError(body["errors"][0].get("message", "AniList error"))
    return body.get("data") or {}


async def fetch_media(al_id: int) -> Optional[Dict[str, Any]]:
    async def produce():
        data = await _anilist(_MEDIA_QUERY, {"id": al_id})
        return data.get("Media") or {}

    media = await cache.cached("titlepage", str(al_id), PAGE_TTL, produce, miss_ttl=MISS_TTL)
    return media or None


async def sitemap_titles(part: int) -> List[Dict[str, Any]]:
    """Titles for sitemap file `part` (1-based): the next 500 by popularity."""
    async def produce():
        found: List[Dict[str, Any]] = []
        first = (part - 1) * SITEMAP_ANILIST_PAGES + 1
        for page in range(first, first + SITEMAP_ANILIST_PAGES):
            data = await _anilist(_SITEMAP_QUERY, {"page": page, "n": SITEMAP_PER_PAGE})
            media = ((data.get("Page") or {}).get("media")) or []
            found.extend({"id": m["id"], "title": m.get("title") or {}} for m in media)
            if len(media) < SITEMAP_PER_PAGE:
                break
        return found

    return await cache.cached("sitemap", str(part), SITEMAP_TTL, produce, miss_ttl=MISS_TTL)


def _e(value: Any) -> str:
    return html.escape(str(value or ""), quote=True)


_STYLE = """
:root{--bg:#14101a;--surf:#1e1826;--surf2:#271f31;--line:#372c44;--text:#f0ebf5;--dim:#a094ad;--mint:#3fe0c8}
*{box-sizing:border-box}html,body{margin:0;padding:0}
body{background:var(--bg);color:var(--text);line-height:1.55;
font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif}
a{color:var(--mint)}
header{display:flex;align-items:center;gap:8px;padding:13px 16px;border-bottom:1px solid var(--line)}
header a{color:var(--text);text-decoration:none;font-weight:700;font-size:17px;letter-spacing:-.02em;
display:flex;align-items:center;gap:8px}
header b{color:var(--mint)}
main{max-width:820px;margin:0 auto;padding:20px 16px 60px}
.hero{display:flex;gap:16px;align-items:flex-start}
.hero img{width:130px;border-radius:10px;flex:none;background:var(--surf)}
h1{font-size:22px;line-height:1.25;margin:0 0 6px}
.alt{color:var(--dim);font-size:13px;margin:0 0 10px}
.facts{display:flex;flex-wrap:wrap;gap:6px;margin:0;padding:0;list-style:none}
.facts li{background:var(--surf2);border:1px solid var(--line);border-radius:20px;padding:2px 10px;font-size:12px}
.read{display:block;text-align:center;margin:20px 0;background:var(--mint);color:#0d2b26;font-weight:700;
font-size:16px;padding:14px;border-radius:12px;text-decoration:none}
.syn{white-space:pre-line;color:var(--text)}
h2{font-size:15px;margin:28px 0 10px;color:var(--dim);text-transform:uppercase;letter-spacing:.06em}
.recs{display:grid;grid-template-columns:repeat(auto-fill,minmax(100px,1fr));gap:12px}
.recs a{text-decoration:none;color:var(--text);font-size:13px;line-height:1.3}
.recs img{width:100%;aspect-ratio:2/3;object-fit:cover;border-radius:8px;background:var(--surf);margin-bottom:5px}
footer{color:var(--dim);font-size:12px;text-align:center;padding:20px 16px 40px}
@media (max-width:420px){.hero img{width:110px}h1{font-size:19px}}
"""

_LOGO = (
    '<svg width="24" height="24" viewBox="0 0 100 100" aria-hidden="true">'
    '<rect x="6" y="10" width="88" height="66" rx="24" fill="#3fe0c8"/>'
    '<path d="M22 68 L12 92 L46 74 Z" fill="#3fe0c8"/>'
    '<path d="M30 59 V28 L50 48 L70 28 V59" fill="none" stroke="#14101a" stroke-width="10" '
    'stroke-linecap="round" stroke-linejoin="round"/></svg>'
)


def render(media: Dict[str, Any]) -> str:
    name = title_of(media)
    t = media.get("title") or {}
    alts = [a for a in [t.get("romaji"), t.get("native")] + list(media.get("synonyms") or [])
            if a and a != name]
    alts = list(dict.fromkeys(alts))[:4]
    kind = KIND.get((media.get("countryOfOrigin") or "").upper(), "Manga")
    synopsis = clean_synopsis(media.get("description"))
    cover = (media.get("coverImage") or {}).get("extraLarge") or (media.get("coverImage") or {}).get("large") or ""
    genres = list(media.get("genres") or [])
    status = STATUS.get(media.get("status") or "", "")
    chapters = media.get("chapters")
    year = (media.get("startDate") or {}).get("year")

    base = site_url()
    path = path_for(media)
    canonical = (base or "") + path
    app_link = f"/?m=al-{media['id']}"
    noindex = not is_live() or bool(media.get("isAdult"))

    page_title = f"{name} – Read {kind} Online | MangaWhere"
    description = _short(synopsis) if synopsis else f"Find where to read {name} ({kind}) online."

    facts = [kind] + ([status] if status else []) + \
        ([f"{chapters} chapters"] if chapters else []) + ([str(year)] if year else []) + genres[:6]

    recs = []
    for node in ((media.get("recommendations") or {}).get("nodes") or []):
        r = node.get("mediaRecommendation")
        if not r or r.get("isAdult"):
            continue
        r_cover = (r.get("coverImage") or {}).get("large") or ""
        recs.append(
            f'<a href="{_e(path_for(r))}"><img src="{_e(r_cover)}" alt="" loading="lazy">'
            f"{_e(title_of(r))}</a>"
        )

    ld = {
        "@context": "https://schema.org",
        "@type": "Book",
        "name": name,
        "url": canonical,
        "genre": genres,
        "bookFormat": "GraphicNovel",
    }
    if alts:
        ld["alternateName"] = alts
    if cover:
        ld["image"] = cover
    if synopsis:
        ld["description"] = _short(synopsis, 300)
    # Inside <script>: no "<", ">" or "&" from AniList may show through.
    ld_json = (json.dumps(ld, ensure_ascii=False)
               .replace("<", "\\u003c").replace(">", "\\u003e").replace("&", "\\u0026"))

    head = [
        '<meta charset="utf-8">',
        '<meta name="viewport" content="width=device-width,initial-scale=1">',
        f"<title>{_e(page_title)}</title>",
        f'<meta name="description" content="{_e(description)}">',
        '<meta name="robots" content="noindex">' if noindex else "",
        f'<link rel="canonical" href="{_e(canonical)}">' if base else "",
        '<meta property="og:type" content="book">',
        '<meta property="og:site_name" content="MangaWhere">',
        f'<meta property="og:title" content="{_e(name)} – read it on MangaWhere">',
        f'<meta property="og:description" content="{_e(description)}">',
        f'<meta property="og:url" content="{_e(canonical)}">' if base else "",
        f'<meta property="og:image" content="{_e(cover)}">' if cover else "",
        '<meta name="twitter:card" content="summary">',
        '<meta name="theme-color" content="#12101c">',
        '<link rel="icon" href="/icons/favicon.svg" type="image/svg+xml">',
        '<link rel="icon" href="/icons/favicon-48.png" sizes="48x48" type="image/png">',
        f"<style>{_STYLE}</style>",
        f'<script type="application/ld+json">{ld_json}</script>',
    ]
    body = [
        f'<header><a href="/">{_LOGO}<span>manga<b>where</b></span></a></header>',
        "<main>",
        '<div class="hero">',
        f'<img src="{_e(cover)}" alt="{_e(name)} cover">' if cover else "",
        "<div>",
        f"<h1>{_e(name)}</h1>",
        f'<p class="alt">Also known as {_e(" · ".join(alts))}</p>' if alts else "",
        '<ul class="facts">' + "".join(f"<li>{_e(f)}</li>" for f in facts) + "</ul>",
        "</div></div>",
        f'<a class="read" href="{_e(app_link)}">Read {_e(name)} on MangaWhere</a>',
        f'<h2>About {_e(name)}</h2><p class="syn">{_e(synopsis)}</p>' if synopsis else "",
        ("<h2>If you like this</h2><div class=\"recs\">" + "".join(recs) + "</div>") if recs else "",
        "</main>",
        "<footer>MangaWhere finds where to read any manga, manhwa or manhua. "
        "Title details from AniList.</footer>",
    ]
    return ("<!doctype html>\n<html lang=\"en\"><head>" + "".join(h for h in head if h) +
            "</head><body>" + "".join(b for b in body if b) + "</body></html>")


def robots_txt() -> str:
    if not is_live():
        return "User-agent: *\nDisallow: /manga/\n"
    return f"User-agent: *\nAllow: /\n\nSitemap: {site_url()}/sitemap.xml\n"


def sitemap_index() -> str:
    base = site_url()
    parts = "".join(
        f"<sitemap><loc>{_e(base)}/sitemaps/titles-{n}.xml</loc></sitemap>"
        for n in range(1, SITEMAP_FILES + 1)
    )
    return ('<?xml version="1.0" encoding="UTF-8"?>\n'
            '<sitemapindex xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
            f"{parts}</sitemapindex>")


def sitemap_file(titles: List[Dict[str, Any]]) -> str:
    base = site_url()
    urls = "".join(f"<url><loc>{_e(base + path_for(m))}</loc></url>" for m in titles)
    return ('<?xml version="1.0" encoding="UTF-8"?>\n'
            '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
            f"{urls}</urlset>")
