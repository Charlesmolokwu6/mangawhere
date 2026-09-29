import asyncio
import re
from typing import Any, Dict, Iterable, List, Optional
from urllib.parse import quote, urljoin

import httpx
from bs4 import BeautifulSoup
from playwright.async_api import async_playwright

from .matching import similar

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.5",
    "Upgrade-Insecure-Requests": "1",
}

IGNORE_KEYWORDS = [
    "logo",
    "banner",
    "discord",
    "avatar",
    "credit",
    "patreon",
    "promo",
    "ad",
    "ads",
]

# Keywords need non-alphanumeric boundaries — otherwise short/generic ones
# like "ad" match as a substring of completely unrelated words. A chapter
# page for "Shadow Slave" has "ad" sitting right inside "shadow", and a
# bare `"ad" in url` check was silently dropping every one of its images.
_IGNORE_PATTERN = re.compile(
    r"(?:^|[^a-z0-9])(?:" + "|".join(re.escape(k) for k in IGNORE_KEYWORDS) + r")(?:[^a-z0-9]|$)"
)


def clean_image_urls(raw_urls: Iterable[str]) -> List[str]:
    """Filter image URLs to keep only chapter pages and remove common ad/banner noise."""
    cleaned: List[str] = []
    seen = set()

    for raw in raw_urls:
        if not raw or not isinstance(raw, str):
            continue
        url = raw.strip()
        if not url:
            continue
        if url.startswith("//"):
            url = "https:" + url

        lower = url.lower()
        if not re.search(r"\.(?:jpe?g|png|webp|gif|avif)(?:[?#]|$)", lower):
            continue
        if _IGNORE_PATTERN.search(lower):
            continue
        if url in seen:
            continue
        seen.add(url)
        cleaned.append(url)

    return cleaned


def extract_image_urls_from_html(html: str, selectors: Iterable[str]) -> List[str]:
    """Collect image URLs from the given selectors using the most useful attribute available."""
    soup = BeautifulSoup(html, "html.parser")
    raw_sources: List[str] = []

    for selector in selectors:
        for element in soup.select(selector):
            for attr in ("data-src", "data-lazy-src", "src"):
                value = element.get(attr)
                if value:
                    raw_sources.append(value)
                    break

    return clean_image_urls(raw_sources)


class CloudflareChallenge(PermissionError):
    """Cloudflare's JS-computation interstitial ("Just a moment...") — a
    real browser can run the JS and get past this."""


class CloudflareBlocked(PermissionError):
    """A hard, network-level Cloudflare deny rule (its "error 1005"-style
    page, typically aimed at whole datacenter/hosting IP ranges). No
    browser, however real, gets past this — Cloudflare rejects the
    connection before serving anything a JS challenge would apply to, so
    a real one just gets the identical deny page a plain fetch did."""


async def fetch_html_httpx(url: str) -> str:
    """Fast standard fetch path for normal HTML pages."""
    async with httpx.AsyncClient(headers=HEADERS, follow_redirects=True, timeout=15.0) as client:
        response = await client.get(url)
        lowered = response.text.lower()
        if "used cloudflare to restrict access" in lowered:
            raise CloudflareBlocked("Cloudflare denied this request outright")
        # The actual interstitial challenge page — not just any page that
        # happens to load Cloudflare's (very common) analytics beacon
        # script, which the bare substring "cloudflare" also matches.
        is_challenge_page = (
            "just a moment" in lowered
            or "cf-browser-verification" in lowered
            or "cf_chl_opt" in lowered
        )
        if response.status_code in (403, 429) or is_challenge_page:
            raise CloudflareChallenge("Cloudflare protection active")
        response.raise_for_status()
        return response.text


async def fetch_html_playwright(url: str) -> str:
    """Browser-based fallback used when anti-bot protection blocks standard HTTP requests."""
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context(user_agent=HEADERS["User-Agent"])
        page = await context.new_page()
        await page.goto(url, wait_until="domcontentloaded", timeout=30000)
        await page.evaluate("window.scrollTo(0, document.body.scrollHeight / 2)")
        await page.wait_for_timeout(2000)
        content = await page.content()
        await browser.close()
        return content


async def scrape_toongod(url: str) -> List[str]:
    """Scrape ToonGod chapter pages using the Madara widget structure."""
    try:
        html = await fetch_html_httpx(url)
    except CloudflareBlocked:
        html = ""
    except PermissionError:
        html = await fetch_html_playwright(url)

    selectors = [
        ".reading-content img",
        ".page-break img",
        "img.wp-manga-chapter-img",
    ]
    return extract_image_urls_from_html(html, selectors)


async def scrape_asurascans(url: str) -> List[str]:
    """Scrape Asura Scans chapter pages. The plain HTTP fetch works in
    practice (chapter pages are server-rendered, not behind Cloudflare's
    JS challenge), so it's tried first; a real browser is the fallback."""
    try:
        html = await fetch_html_httpx(url)
    except Exception:
        try:
            html = await fetch_html_playwright(url)
        except Exception:
            html = ""

    selectors = [
        "img[data-page-index]",  # current site markup, verified directly
        "#readerarea img",  # older/alternate theme, kept as a fallback
        "#chapter-images img",
    ]
    return extract_image_urls_from_html(html or "", selectors)


async def scrape_mangafreak(url: str) -> List[str]:
    """Scrape MangaFreak chapter pages. Server-rendered, no Cloudflare
    challenge in practice, so the plain fetch is tried first."""
    try:
        html = await fetch_html_httpx(url)
    except Exception:
        try:
            html = await fetch_html_playwright(url)
        except Exception:
            html = ""

    return extract_image_urls_from_html(html or "", ['img[id="gohere"]'])


COMIZY_API = "https://api.comizy.io"
COMIZY_ID_IN_HTML = re.compile(r'"id":"([A-Za-z0-9]+)"')
COMIZY_IS_ADULT_IN_HTML = re.compile(r'"is_adult":(true|false)')


async def scrape_comizy(url: str) -> List[str]:
    """Scrape a comizy.io chapter's page images. Server-rendered, no
    Cloudflare challenge in practice, so a plain fetch is enough — no
    Playwright needed. Checked for is_adult here too (not just in search)
    since a chapter URL can be reached directly, bypassing search."""
    try:
        html = await fetch_html_httpx(url)
    except Exception:
        return []

    is_adult_match = COMIZY_IS_ADULT_IN_HTML.search(html)
    if is_adult_match and is_adult_match.group(1) == "true":
        return []

    return extract_image_urls_from_html(html, ["div[data-page-idx] img"])


MANGADEX_API = "https://api.mangadex.org"
MANGADEX_CONTENT_RATING = ["safe", "suggestive"]
MANGADEX_ID_IN_URL = re.compile(r"/(?:title|chapter)/([0-9a-f-]{36})", re.I)


async def scrape_mangadex(url: str) -> List[str]:
    """List a MangaDex chapter's page images via the official public API —
    no HTML scraping or anti-bot handling needed, this site has a real API."""
    match = MANGADEX_ID_IN_URL.search(url)
    if not match:
        return []
    chapter_id = match.group(1)
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            response = await client.get(f"{MANGADEX_API}/at-home/server/{chapter_id}")
            response.raise_for_status()
            data = response.json()
    except Exception as e:
        print(f"[mangadex] image list fetch failed ({e})")
        return []

    chapter = data.get("chapter") or {}
    base_url = data.get("baseUrl")
    file_hash = chapter.get("hash")
    filenames = chapter.get("data") or []
    if not base_url or not file_hash:
        return []
    return [f"{base_url}/data/{file_hash}/{name}" for name in filenames]


CHAPTER_NUMBER_IN_HREF = re.compile(r"chapter[-/](\d+(?:\.\d+)?)", re.I)
# MangaFreak's chapter URLs don't contain the word "chapter" at all —
# /Read1_One_Piece_1 — just a number at the very end of the path.
CHAPTER_NUMBER_TRAILING_IN_HREF = re.compile(r"_(\d+(?:\.\d+)?)/?(?:[?#]|$)")
CHAPTER_NUMBER_IN_TEXT = re.compile(r"(\d+(?:\.\d+)?)\s*$")


def extract_chapter_list(html: str, selector: str, base_url: str) -> List[Dict[str, Any]]:
    """Collect a series page's chapter links, deduplicated and sorted by
    chapter number. Numbers are read from the URL first (reliable on every
    site's own predictable pattern) and only fall back to the link text
    for markup that doesn't follow one."""
    soup = BeautifulSoup(html, "html.parser")
    chapters: Dict[float, Dict[str, Any]] = {}

    for a in soup.select(selector):
        href = a.get("href")
        if not href:
            continue
        text = a.get_text(" ", strip=True)
        match = (
            CHAPTER_NUMBER_IN_HREF.search(href)
            or CHAPTER_NUMBER_TRAILING_IN_HREF.search(href)
            or CHAPTER_NUMBER_IN_TEXT.search(text)
        )
        if not match:
            continue
        number = float(match.group(1))
        if number in chapters:
            continue
        chapters[number] = {
            "number": number,
            "url": urljoin(base_url, href),
            "title": text or f"Chapter {number:g}",
        }

    return sorted(chapters.values(), key=lambda c: c["number"])


async def scrape_toongod_chapter_list(url: str) -> List[Dict[str, Any]]:
    """List chapters from a ToonGod series page (Madara theme chapter list)."""
    try:
        html = await fetch_html_httpx(url)
    except CloudflareBlocked:
        html = ""
    except PermissionError:
        html = await fetch_html_playwright(url)
    return extract_chapter_list(html, ".wp-manga-chapter a", url)


async def scrape_asurascans_chapter_list(url: str) -> List[Dict[str, Any]]:
    """List chapters from an Asura Scans series page."""
    try:
        html = await fetch_html_httpx(url)
    except Exception:
        try:
            html = await fetch_html_playwright(url)
        except Exception:
            html = ""
    return extract_chapter_list(html or "", 'a[href*="/chapter/"]', url)


async def scrape_mangafreak_chapter_list(url: str) -> List[Dict[str, Any]]:
    """List chapters from a MangaFreak series page."""
    try:
        html = await fetch_html_httpx(url)
    except Exception:
        try:
            html = await fetch_html_playwright(url)
        except Exception:
            html = ""
    return extract_chapter_list(html or "", "a.chapter-link", url)


async def scrape_comizy_chapter_list(url: str) -> List[Dict[str, Any]]:
    """List a comizy.io series' chapters. The title id isn't in the URL
    (comizy.io addresses series by slug, e.g. /solo-leveling), so this
    reads it out of the series page's own server-rendered JSON — which
    also carries is_adult, checked here for the same direct-URL reason
    as scrape_comizy above."""
    try:
        html = await fetch_html_httpx(url)
    except Exception:
        return []

    id_match = COMIZY_ID_IN_HTML.search(html)
    is_adult_match = COMIZY_IS_ADULT_IN_HTML.search(html)
    if not id_match or (is_adult_match and is_adult_match.group(1) == "true"):
        return []
    title_id = id_match.group(1)

    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            response = await client.get(
                f"{COMIZY_API}/titles/{title_id}/chapters", params={"page": 1, "limit": 500}
            )
            response.raise_for_status()
            data = response.json()
    except Exception as e:
        print(f"[comizy] chapter list fetch failed ({e})")
        return []

    chapters: Dict[float, Dict[str, Any]] = {}
    for chapter in data.get("data", {}).get("chapters", []):
        raw_number = chapter.get("number")
        if raw_number is None:
            continue
        try:
            number = float(raw_number)
        except (TypeError, ValueError):
            continue
        if number in chapters:
            continue
        chapters[number] = {
            "number": number,
            "url": f"https://comizy.io{chapter['url']}",
            "title": chapter.get("name") or f"Chapter {number:g}",
        }
    return sorted(chapters.values(), key=lambda c: c["number"])


async def scrape_mangadex_chapter_list(url: str) -> List[Dict[str, Any]]:
    """List a MangaDex series' chapters via its feed API. Chapters whose
    `externalUrl` is set are licensed out to another platform — MangaDex
    itself hosts no pages for those, so they're skipped rather than
    included as dead links."""
    match = MANGADEX_ID_IN_URL.search(url)
    if not match:
        return []
    manga_id = match.group(1)
    params = {
        "translatedLanguage[]": "en",
        "order[chapter]": "desc",
        "limit": 100,
        "contentRating[]": MANGADEX_CONTENT_RATING,
    }
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            response = await client.get(f"{MANGADEX_API}/manga/{manga_id}/feed", params=params)
            response.raise_for_status()
            data = response.json()
    except Exception as e:
        print(f"[mangadex] chapter list fetch failed ({e})")
        return []

    chapters: Dict[float, Dict[str, Any]] = {}
    for chapter in data.get("data", []):
        attrs = chapter.get("attributes", {})
        if attrs.get("externalUrl") or not attrs.get("pages"):
            continue
        raw_number = attrs.get("chapter")
        if raw_number is None:
            continue
        try:
            number = float(raw_number)
        except ValueError:
            continue
        if number in chapters:
            continue
        chapters[number] = {
            "number": number,
            "url": f"https://mangadex.org/chapter/{chapter['id']}",
            "title": attrs.get("title") or f"Chapter {number:g}",
        }
    return sorted(chapters.values(), key=lambda c: c["number"])


def _detect_domain(url: str) -> str:
    lowered = (url or "").lower()
    if "toongod" in lowered:
        return "toongod"
    if "asurascans" in lowered or "asura" in lowered:
        return "asurascans"
    if "mangafreak" in lowered:
        return "mangafreak"
    if "mangadex" in lowered:
        return "mangadex"
    if "comizy" in lowered or "mangabuddy" in lowered:
        return "comizy"
    return "unknown"


async def scrape_series(url: str) -> Dict[str, Any]:
    """Auto-detect the site and return the series' chapter list."""
    domain = _detect_domain(url)

    if domain == "toongod":
        chapters = await scrape_toongod_chapter_list(url)
    elif domain == "asurascans":
        chapters = await scrape_asurascans_chapter_list(url)
    elif domain == "mangafreak":
        chapters = await scrape_mangafreak_chapter_list(url)
    elif domain == "mangadex":
        chapters = await scrape_mangadex_chapter_list(url)
    elif domain == "comizy":
        chapters = await scrape_comizy_chapter_list(url)
    else:
        chapters = []

    return {"domain": domain, "series_url": url, "chapters": chapters}


MATCH_THRESHOLD = 0.5  # same bar the client's own MangaUpdates matching uses


def _best_match(html: str, selector: str, base_url: str, title: str, *, use_alt: bool) -> Optional[str]:
    """Score every candidate link's visible name against `title` and
    return the best match's absolute URL, if it clears MATCH_THRESHOLD."""
    soup = BeautifulSoup(html, "html.parser")
    best_url, best_name, best_score = None, None, 0.0
    seen = set()
    candidate_count = 0

    for a in soup.select(selector):
        href = a.get("href")
        if not href:
            continue

        name = ""
        if use_alt:
            img = a.select_one("img[alt]")
            name = (img.get("alt") if img else "") or ""
        name = name or a.get_text(" ", strip=True)
        if not name:
            # Some cards wrap two anchors around one href — a bare cover
            # image first, the titled link second. Skip a nameless one
            # without marking its href "seen", or the titled anchor right
            # after it never gets a chance to be scored.
            continue
        if href in seen:
            continue
        seen.add(href)
        candidate_count += 1

        score = similar(title, name)
        if score > best_score:
            best_score, best_url, best_name = score, href, name

    if best_url and best_score >= MATCH_THRESHOLD:
        return urljoin(base_url, best_url)

    # Distinguishes "the page had nothing to match" (selector drift, or the
    # site's own search genuinely has nothing) from "something was there
    # but scored too low to trust" — otherwise a rejected match and a
    # broken selector look identical from the outside.
    if candidate_count:
        print(
            f"[match] {candidate_count} candidate(s) for \"{title}\" via {selector!r}; "
            f"best was \"{best_name}\" (score {best_score:.2f}, threshold {MATCH_THRESHOLD})"
        )
    else:
        print(f"[match] no candidates found for \"{title}\" via {selector!r} — selector may not match the page")
    return None


async def search_toongod(title: str) -> Optional[str]:
    """Find the best-matching series URL on ToonGod for a title, via the
    Madara theme's standard search results page."""
    search_url = f"https://toongod.org/?s={quote(title)}&post_type=wp-manga"
    try:
        html = await fetch_html_httpx(search_url)
    except CloudflareBlocked:
        # A real browser would hit the identical deny page — not worth a
        # multi-second Playwright launch to find that out again. Confirmed
        # directly: ToonGod's Cloudflare rule denies this host outright,
        # every time, regardless of how the request is made.
        print("[toongod] hard-blocked by Cloudflare — not retrying via Playwright")
        return None
    except Exception as e:
        print(f"[toongod] plain fetch failed ({e}), falling back to Playwright")
        try:
            html = await fetch_html_playwright(search_url)
        except Exception as e2:
            print(f"[toongod] Playwright fallback also failed: {e2}")
            return None
    return _best_match(html, ".post-title a", search_url, title, use_alt=False)


async def search_asurascans(title: str) -> Optional[str]:
    """Find the best-matching series URL on Asura Scans for a title. The
    site's search box filters client-side (a plain fetch of a "?search="
    URL returns the same unfiltered page), so this matches against the
    full comics listing instead — each card's cover <img alt> carries the
    clean title even where the link's own text has extra rating/badge text
    mixed in."""
    listing_url = "https://asurascans.com/comics"
    try:
        html = await fetch_html_httpx(listing_url)
    except Exception as e:
        print(f"[asurascans] plain fetch failed ({e}), falling back to Playwright")
        try:
            html = await fetch_html_playwright(listing_url)
        except Exception as e2:
            print(f"[asurascans] Playwright fallback also failed: {e2}")
            return None
    return _best_match(html, 'a[href^="/comics/"]', listing_url, title, use_alt=True)


async def search_mangafreak(title: str) -> Optional[str]:
    """Find the best-matching series URL on MangaFreak for a title, via
    its own search results page (/Find/<query>)."""
    search_url = f"https://ww3.mangafreak.me/Find/{quote(title)}"
    try:
        html = await fetch_html_httpx(search_url)
    except Exception as e:
        print(f"[mangafreak] plain fetch failed ({e}), falling back to Playwright")
        try:
            html = await fetch_html_playwright(search_url)
        except Exception as e2:
            print(f"[mangafreak] Playwright fallback also failed: {e2}")
            return None
    return _best_match(
        html, '.manga_search_item a[href^="/Manga/"]', search_url, title, use_alt=False
    )


async def search_mangadex(title: str) -> Optional[str]:
    """Find the best-matching series on MangaDex via its official public
    API. Titles are stored per-language plus a list of alt titles, so every
    name a manga is known by is scored and the best across all of them wins."""
    params = {"title": title, "limit": 10, "contentRating[]": MANGADEX_CONTENT_RATING}
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            response = await client.get(f"{MANGADEX_API}/manga", params=params)
            response.raise_for_status()
            data = response.json()
    except Exception as e:
        print(f"[mangadex] search failed ({e})")
        return None

    best_id, best_score = None, 0.0
    for manga in data.get("data", []):
        attrs = manga.get("attributes", {})
        names = list(attrs.get("title", {}).values())
        for alt in attrs.get("altTitles", []):
            names.extend(alt.values())
        for name in names:
            score = similar(title, name)
            if score > best_score:
                best_score, best_id = score, manga.get("id")

    if best_id and best_score >= MATCH_THRESHOLD:
        return f"https://mangadex.org/title/{best_id}"
    print(f"[mangadex] no match for \"{title}\" cleared threshold (best score {best_score:.2f})")
    return None


async def search_comizy(title: str) -> Optional[str]:
    """Find the best-matching series on comizy.io via its internal search
    API. Titles flagged is_adult or has_dmca are skipped — mangawhere has
    no age-gating, and DMCA'd titles aren't comizy's to serve either."""
    params = {"page": 1, "limit": 10, "q": title}
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            response = await client.get(f"{COMIZY_API}/titles/search", params=params)
            response.raise_for_status()
            data = response.json()
    except Exception as e:
        print(f"[comizy] search failed ({e})")
        return None

    best_url, best_score = None, 0.0
    for item in data.get("data", {}).get("items", []):
        if item.get("is_adult") or item.get("has_dmca"):
            continue
        names = [item.get("name", "")] + [alt.get("name", "") for alt in item.get("alt_names", [])]
        for name in names:
            score = similar(title, name)
            if score > best_score:
                best_score, best_url = score, item.get("url")

    if best_url and best_score >= MATCH_THRESHOLD:
        return f"https://comizy.io{best_url}"
    print(f"[comizy] no match for \"{title}\" cleared threshold (best score {best_score:.2f})")
    return None


SOURCES = {
    "toongod": (search_toongod, scrape_toongod_chapter_list),
    "asurascans": (search_asurascans, scrape_asurascans_chapter_list),
    "mangafreak": (search_mangafreak, scrape_mangafreak_chapter_list),
    "mangadex": (search_mangadex, scrape_mangadex_chapter_list),
    "comizy": (search_comizy, scrape_comizy_chapter_list),
}


async def find_best_source(title: str) -> Optional[Dict[str, Any]]:
    """Search every site for a title and read from whichever is more
    up to date — i.e. has the higher chapter number right now."""
    domains = list(SOURCES.keys())
    urls = await asyncio.gather(*(SOURCES[d][0](title) for d in domains))

    candidates: List[Dict[str, Any]] = []
    for domain, series_url in zip(domains, urls):
        if not series_url:
            continue
        chapters = await SOURCES[domain][1](series_url)
        if chapters:
            candidates.append({"domain": domain, "series_url": series_url, "chapters": chapters})

    if not candidates:
        return None

    candidates.sort(key=lambda c: c["chapters"][-1]["number"], reverse=True)
    return candidates[0]


async def scrape_chapter(url: str) -> Dict[str, Any]:
    """Auto-detect the site and return the normalized chapter payload."""
    domain = _detect_domain(url)

    if domain == "toongod":
        images = await scrape_toongod(url)
    elif domain == "asurascans":
        images = await scrape_asurascans(url)
    elif domain == "mangafreak":
        images = await scrape_mangafreak(url)
    elif domain == "mangadex":
        images = await scrape_mangadex(url)
    elif domain == "comizy":
        images = await scrape_comizy(url)
    else:
        images = []

    return {
        "domain": domain,
        "chapter_url": url,
        "images": images,
    }
