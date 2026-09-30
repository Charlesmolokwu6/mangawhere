import asyncio
import json
import re
from typing import Any, Dict, Iterable, List, Optional
from urllib.parse import quote, urljoin

import httpx
from bs4 import BeautifulSoup
from playwright.async_api import async_playwright

from .matching import title_score

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


async def fetch_html_httpx(url: str, timeout: float = 15.0) -> str:
    """Fast standard fetch path for normal HTML pages."""
    async with httpx.AsyncClient(headers=HEADERS, follow_redirects=True, timeout=timeout) as client:
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


async def scrape_mangaread(url: str) -> List[str]:
    """Scrape mangaread.org chapter pages — a self-hosted Madara-theme
    WordPress site, fully server-rendered, no Cloudflare challenge."""
    try:
        html = await fetch_html_httpx(url)
    except Exception:
        try:
            html = await fetch_html_playwright(url)
        except Exception:
            html = ""
    return extract_image_urls_from_html(html or "", ["div.reading-content img.wp-manga-chapter-img"])


FLAMECOMICS_NEXT_DATA = re.compile(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', re.S)


def _flamecomics_page_props(html: str) -> Dict[str, Any]:
    """flamecomics.xyz is a Next.js app that ships its whole page's data as
    one JSON blob (the __NEXT_DATA__ SSR payload) — reading that directly
    is more stable than CSS selectors here, since Next.js's own CSS-module
    class names carry a build-specific hash that rotates across deploys."""
    match = FLAMECOMICS_NEXT_DATA.search(html or "")
    if not match:
        return {}
    try:
        data = json.loads(match.group(1))
    except Exception:
        return {}
    return data.get("props", {}).get("pageProps", {})


async def scrape_flamecomics(url: str) -> List[str]:
    """Scrape a flamecomics.xyz chapter's page images. The DOM also ships
    decoy "read on Flame" promo images with misleadingly page-like alt
    text; filtering to the real upload path (as opposed to the promo
    images' /assets/read/ path) excludes them cleanly."""
    try:
        html = await fetch_html_httpx(url)
    except Exception:
        return []
    return extract_image_urls_from_html(
        html, ['img[src*="cdn.flamecomics.xyz/uploads/images/series/"]']
    )


async def _fetch_html_playwright_scrolled(url: str) -> str:
    """Like fetch_html_playwright, but scrolls incrementally to the very
    bottom rather than jumping halfway once — needed for manhuaplus.org,
    whose page images are lazy-loaded and a long chapter can run well past
    the halfway point the shared helper stops at."""
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context(user_agent=HEADERS["User-Agent"])
        page = await context.new_page()
        await page.goto(url, wait_until="networkidle", timeout=30000)
        previous_height = 0
        for _ in range(20):
            height = await page.evaluate("document.body.scrollHeight")
            if height == previous_height:
                break
            previous_height = height
            await page.evaluate(f"window.scrollTo(0, {height})")
            await page.wait_for_timeout(400)
        await page.wait_for_timeout(3000)
        content = await page.content()
        await browser.close()
        return content


async def scrape_manhuaplus(url: str) -> List[str]:
    """Scrape a manhuaplus.org chapter's page images — via the page's own
    image-list endpoint first (_manhuaplus_images_via_ajax), and only if
    that fails, a real browser render. The static HTML only ships
    loading-spinner placeholders — real image URLs land in the <img>
    `src` attribute only after the page's own lazy-load JS runs. Read `src` directly
    rather than through extract_image_urls_from_html's usual data-src-first
    attribute order: this theme's lazy-load library leaves `data-src`
    permanently pointing at the loading-spinner placeholder even once
    `src` holds the real, fully-loaded URL — the reverse of the usual
    lazy-loading convention that helper is built around."""
    images = await _manhuaplus_images_via_ajax(url)
    if images:
        return images
    try:
        html = await _fetch_html_playwright_scrolled(url)
    except Exception:
        return []
    soup = BeautifulSoup(html, "html.parser")
    urls = [img.get("src") for img in soup.select("#chapterContent img.lazy") if img.get("src")]
    return clean_image_urls(urls)


MANHUAPLUS_CHAPTER_ID_IN_HTML = re.compile(r"CHAPTER_ID\s*=\s*(\d+)")


async def _manhuaplus_images_via_ajax(url: str) -> List[str]:
    """The page's own JS fills #chapterContent from a plain JSON endpoint
    keyed on the chapter's numeric id — calling that directly skips a whole
    browser launch. The pages in its HTML come back deliberately shuffled,
    each tagged with its real position as data-index, so they're sorted by
    that. Any failure returns [] so the caller can fall back to Playwright."""
    try:
        html = await fetch_html_httpx(url)
        match = MANHUAPLUS_CHAPTER_ID_IN_HTML.search(html)
        if not match:
            return []
        async with httpx.AsyncClient(headers=HEADERS, timeout=15.0) as client:
            response = await client.post(
                f"https://manhuaplus.org/ajax/image/list/chap/{match.group(1)}",
                headers={"X-Requested-With": "XMLHttpRequest", "Referer": url},
            )
            response.raise_for_status()
            data = response.json()
    except Exception as e:
        print(f"[manhuaplus] image list fetch failed ({e}), falling back to Playwright")
        return []
    if not data.get("status"):
        return []

    pages = []
    for block in BeautifulSoup(data.get("html") or "", "html.parser").select("[data-index]"):
        img = block.select_one("img")
        try:
            index = int(block.get("data-index"))
        except (TypeError, ValueError):
            continue
        if img and img.get("src"):
            pages.append((index, img.get("src")))
    return clean_image_urls(src for _, src in sorted(pages))


KALISCAN_IMAGES_IN_HTML = re.compile(r'var chapImages = "([^"]+)";')
# kaliscan.io routinely takes ~20s to answer (every page, not just some),
# so the default 15s timeout meant it failed every single time.
KALISCAN_TIMEOUT = 40.0


async def scrape_kaliscan(url: str) -> List[str]:
    """Scrape a kaliscan.io chapter's page images. The DOM's own <img> tags
    are populated by client-side JS, but the real URLs are already sitting
    in the server-rendered HTML as a JS string — no Playwright needed. Note:
    these URLs carry a short-lived signed token (~11h TTL via an `expires`
    query param), so they're only ever used fresh, right after this fetch —
    never cached or re-served later."""
    try:
        html = await fetch_html_httpx(url, timeout=KALISCAN_TIMEOUT)
    except Exception:
        try:
            html = await fetch_html_playwright(url)
        except Exception:
            html = ""
    match = KALISCAN_IMAGES_IN_HTML.search(html or "")
    if not match:
        return []
    return clean_image_urls(match.group(1).split(","))


MANGAKATANA_IMAGES_IN_HTML = re.compile(r"var thzq\s*=\s*\[(.*?)\];")


async def scrape_mangakatana(url: str) -> List[str]:
    """Scrape a mangakatana.com chapter's page images. Same deal as
    kaliscan.io — the DOM's <img src> is a "#" placeholder, but the real
    per-page URLs are already embedded server-side as a JS array."""
    try:
        html = await fetch_html_httpx(url)
    except Exception:
        try:
            html = await fetch_html_playwright(url)
        except Exception:
            html = ""
    match = MANGAKATANA_IMAGES_IN_HTML.search(html or "")
    if not match:
        return []
    urls = re.findall(r"'([^']+)'", match.group(1))
    return clean_image_urls(urls)


WEEBCENTRAL_API = "https://weebcentral.com"
WEEBCENTRAL_SERIES_ID = re.compile(r"/series/([A-Za-z0-9]+)")
WEEBCENTRAL_CHAPTER_ID = re.compile(r"/chapters/([A-Za-z0-9]+)")


async def scrape_weebcentral(url: str) -> List[str]:
    """Scrape a weebcentral.com chapter's page images. The reading page
    itself ships with zero <img> tags — the real page list is loaded by a
    second HTMX fragment call the browser fires after load. That fragment
    endpoint is plain HTTP, so it's called directly rather than driving a
    browser through the HTMX swap."""
    match = WEEBCENTRAL_CHAPTER_ID.search(url)
    if not match:
        return []
    chapter_id = match.group(1)
    try:
        async with httpx.AsyncClient(headers=HEADERS, timeout=15.0) as client:
            response = await client.get(
                f"{WEEBCENTRAL_API}/chapters/{chapter_id}/images", params={"is_prev": "False"}
            )
            response.raise_for_status()
            html = response.text
    except Exception as e:
        print(f"[weebcentral] image fetch failed ({e})")
        return []
    return extract_image_urls_from_html(html, ["section#chapter-images img"])


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

    # Only the first ~10 pages are server-rendered as <img> tags — the rest
    # are filled in client-side as the reader scrolls. The full, ordered
    # list is already in the page's __NEXT_DATA__ JSON, so read that first
    # and only fall back to the DOM if it's missing.
    images = _comizy_images_from_next_data(html)
    if images:
        return images
    return extract_image_urls_from_html(html, ["div[data-page-idx] img"])


def _comizy_images_from_next_data(html: str) -> List[str]:
    match = FLAMECOMICS_NEXT_DATA.search(html)  # the generic Next.js payload tag, not flame-specific
    if not match:
        return []
    try:
        chapter = json.loads(match.group(1))["props"]["pageProps"]["initialChapter"]
    except (ValueError, KeyError, TypeError):
        return []
    images = chapter.get("images") or [
        page.get("url") for page in chapter.get("pages") or [] if isinstance(page, dict)
    ]
    return clean_image_urls(images)


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


async def scrape_mangaread_chapter_list(url: str) -> List[Dict[str, Any]]:
    """List chapters from a mangaread.org series page (Madara theme)."""
    try:
        html = await fetch_html_httpx(url)
    except Exception:
        try:
            html = await fetch_html_playwright(url)
        except Exception:
            html = ""
    return extract_chapter_list(html or "", "li.wp-manga-chapter a", url)


FLAMECOMICS_SERIES_ID = re.compile(r"/series/(\d+)")


async def scrape_flamecomics_chapter_list(url: str) -> List[Dict[str, Any]]:
    """List chapters from a flamecomics.xyz series page, straight from its
    Next.js SSR JSON payload rather than parsing the rendered DOM."""
    match = FLAMECOMICS_SERIES_ID.search(url)
    if not match:
        return []
    series_id = match.group(1)
    try:
        html = await fetch_html_httpx(f"https://flamecomics.xyz/series/{series_id}")
    except Exception:
        return []
    props = _flamecomics_page_props(html)

    chapters: Dict[float, Dict[str, Any]] = {}
    for chapter in props.get("chapters", []):
        raw_number = chapter.get("chapter")
        if raw_number is None:
            continue
        try:
            number = float(raw_number)
        except (TypeError, ValueError):
            continue
        token = chapter.get("token")
        if not token or number in chapters:
            continue
        chapters[number] = {
            "number": number,
            "url": f"https://flamecomics.xyz/series/{series_id}/{token}",
            "title": chapter.get("title") or f"Chapter {number:g}",
        }
    return sorted(chapters.values(), key=lambda c: c["number"])


async def scrape_manhuaplus_chapter_list(url: str) -> List[Dict[str, Any]]:
    """List chapters from a manhuaplus.org series page."""
    try:
        html = await fetch_html_httpx(url)
    except Exception:
        try:
            html = await fetch_html_playwright(url)
        except Exception:
            html = ""
    return extract_chapter_list(html or "", "ul#myUL li.chapter a", url)


async def scrape_kaliscan_chapter_list(url: str) -> List[Dict[str, Any]]:
    """List chapters from a kaliscan.io series page. Each row's <a> wraps
    both the chapter name and a "3 years ago"-style timestamp in the same
    element, so this reads the name from its own inner element rather than
    the anchor's full text (which would drag the timestamp in too)."""
    try:
        html = await fetch_html_httpx(url, timeout=KALISCAN_TIMEOUT)
    except Exception:
        try:
            html = await fetch_html_playwright(url)
        except Exception:
            html = ""

    soup = BeautifulSoup(html or "", "html.parser")
    chapters: Dict[float, Dict[str, Any]] = {}
    for a in soup.select("#chapter-list > li > a"):
        href = a.get("href")
        if not href:
            continue
        match = CHAPTER_NUMBER_IN_HREF.search(href)
        if not match:
            continue
        number = float(match.group(1))
        if number in chapters:
            continue
        name_el = a.select_one("strong.chapter-title")
        title = (name_el.get_text(" ", strip=True) if name_el else "") or f"Chapter {number:g}"
        chapters[number] = {"number": number, "url": urljoin(url, href), "title": title}
    return sorted(chapters.values(), key=lambda c: c["number"])


# MangaKatana addresses chapters as a bare "/c<N>" URL suffix — no word
# "chapter" in the href at all, so the shared CHAPTER_NUMBER_IN_HREF regex
# (which requires that word) doesn't match. This is site-specific enough
# that it stays local rather than joining the shared regex list, where a
# bare "/c123" pattern would be too easy to false-positive on elsewhere.
MANGAKATANA_CHAPTER_NUMBER_IN_HREF = re.compile(r"/c(\d+(?:\.\d+)?)(?:[/?#]|$)")


async def scrape_mangakatana_chapter_list(url: str) -> List[Dict[str, Any]]:
    """List chapters from a mangakatana.com series page."""
    try:
        html = await fetch_html_httpx(url)
    except Exception:
        try:
            html = await fetch_html_playwright(url)
        except Exception:
            html = ""
    soup = BeautifulSoup(html or "", "html.parser")
    chapters: Dict[float, Dict[str, Any]] = {}
    for a in soup.select("div.chapters table.uk-table tr div.chapter a"):
        href = a.get("href")
        if not href:
            continue
        match = MANGAKATANA_CHAPTER_NUMBER_IN_HREF.search(href)
        if not match:
            continue
        number = float(match.group(1))
        if number in chapters:
            continue
        text = a.get_text(" ", strip=True)
        chapters[number] = {
            "number": number,
            "url": urljoin(url, href),
            "title": text or f"Chapter {number:g}",
        }
    return sorted(chapters.values(), key=lambda c: c["number"])


WEEBCENTRAL_CHAPTER_NUMBER_IN_TEXT = re.compile(r"(\d+(?:\.\d+)?)")
# Some series are split into seasons whose numbering restarts ("S1 -
# Chapter 86", "S2 - Chapter 1"). The first bare number there is the
# season, not the chapter, so these are matched explicitly.
WEEBCENTRAL_SEASON_CHAPTER_IN_TEXT = re.compile(
    r"\bS(\d+)\b.*?\b(?:chapter|ch|episode|ep)\.?\s*(\d+(?:\.\d+)?)", re.I
)


async def scrape_weebcentral_chapter_list(url: str) -> List[Dict[str, Any]]:
    """List chapters from a weebcentral.com series page. weebcentral
    addresses chapters by an opaque ID (/chapters/<ulid>), not a number, so
    the chapter number has to come from the link's own visible text rather
    than its URL. The series page itself only shows a partial/recent list;
    the full one is a separate HTMX fragment endpoint (plain HTTP)."""
    match = WEEBCENTRAL_SERIES_ID.search(url)
    if not match:
        return []
    series_id = match.group(1)
    try:
        async with httpx.AsyncClient(headers=HEADERS, timeout=15.0) as client:
            response = await client.get(f"{WEEBCENTRAL_API}/series/{series_id}/full-chapter-list")
            response.raise_for_status()
            html = response.text
    except Exception as e:
        print(f"[weebcentral] chapter list fetch failed ({e})")
        return []

    soup = BeautifulSoup(html, "html.parser")
    # (season, chapter-within-season) -> entry; season is 0 when unlabelled.
    parsed: Dict[tuple, Dict[str, Any]] = {}
    for a in soup.select('a[href^="/chapters/"]'):
        href = a.get("href")
        if not href:
            continue
        label_el = a.select_one("span.grow > span:first-child")
        label = (label_el.get_text(" ", strip=True) if label_el else "") or a.get_text(" ", strip=True)
        season_match = WEEBCENTRAL_SEASON_CHAPTER_IN_TEXT.search(label)
        if season_match:
            key = (int(season_match.group(1)), float(season_match.group(2)))
        else:
            number_match = WEEBCENTRAL_CHAPTER_NUMBER_IN_TEXT.search(label)
            if not number_match:
                continue
            key = (0, float(number_match.group(1)))
        if key in parsed:
            continue
        parsed[key] = {"url": urljoin(WEEBCENTRAL_API, href), "title": label}

    # Seasons restart their numbering, so each season's chapters are offset
    # by the last chapter of every season before it — S2 Ch 1 after an
    # 86-chapter S1 becomes 87 — which keeps numbers comparable with the
    # other sources' continuous numbering.
    chapters: List[Dict[str, Any]] = []
    offset, season, season_top = 0.0, None, 0.0
    for (this_season, within), entry in sorted(parsed.items()):
        if this_season != season:
            offset += season_top
            season, season_top = this_season, 0.0
        season_top = max(season_top, within)
        number = offset + within
        if chapters and number <= chapters[-1]["number"]:
            continue  # e.g. a season's "Chapter 0" landing on the last one's number
        chapters.append({
            "number": number,
            "url": entry["url"],
            "title": entry["title"] or f"Chapter {number:g}",
        })
    return chapters


# comizy's own "number" field is just the chapter's position in its list
# (Solo Leveling's "Chapter 202" is number 270), not the chapter number, so
# the real one is read from the chapter's name — or, failing that, its slug
# ("chapter-0-1" is 0.1, "chapter-12-the-return" is 12).
COMIZY_CHAPTER_NUMBER_IN_NAME = re.compile(r"chapter\s*(\d+(?:\.\d+)?)", re.I)
COMIZY_CHAPTER_NUMBER_IN_SLUG = re.compile(r"^chapter-(\d+)(?:-(\d+))?(?=-|$)", re.I)


def _comizy_chapter_number(chapter: Dict[str, Any]) -> Optional[float]:
    match = COMIZY_CHAPTER_NUMBER_IN_NAME.search(chapter.get("name") or "")
    if match:
        return float(match.group(1))
    slug = chapter.get("slug") or (chapter.get("url") or "").rstrip("/").rsplit("/", 1)[-1]
    match = COMIZY_CHAPTER_NUMBER_IN_SLUG.search(slug)
    if not match:
        return None  # a "Notice"/announcement post, not a chapter
    whole, fraction = match.groups()
    return float(f"{whole}.{fraction}") if fraction else float(whole)


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
        number = _comizy_chapter_number(chapter)
        if number is None:
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
    if "mangaread" in lowered:
        return "mangaread"
    if "flamecomics" in lowered:
        return "flamecomics"
    if "manhuaplus" in lowered:
        return "manhuaplus"
    if "kaliscan" in lowered:
        return "kaliscan"
    if "mangakatana" in lowered:
        return "mangakatana"
    if "weebcentral" in lowered:
        return "weebcentral"
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
    elif domain == "mangaread":
        chapters = await scrape_mangaread_chapter_list(url)
    elif domain == "flamecomics":
        chapters = await scrape_flamecomics_chapter_list(url)
    elif domain == "manhuaplus":
        chapters = await scrape_manhuaplus_chapter_list(url)
    elif domain == "kaliscan":
        chapters = await scrape_kaliscan_chapter_list(url)
    elif domain == "mangakatana":
        chapters = await scrape_mangakatana_chapter_list(url)
    elif domain == "weebcentral":
        chapters = await scrape_weebcentral_chapter_list(url)
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
        # manhuaplus.org's search cards are a bare linked cover image with
        # no visible text at all — the title only exists as the anchor's
        # own `title` attribute.
        name = name or (a.get("title") or "")
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

        score = title_score(title, name)
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


async def search_mangaread(title: str) -> Optional[str]:
    """Find the best-matching series URL on mangaread.org for a title. The
    theme's search form carries a hidden post_type field — a bare "?s="
    with no post_type silently returns a "no results" page even for
    titles that exist, so it has to be included explicitly."""
    search_url = f"https://www.mangaread.org/?s={quote(title)}&post_type=wp-manga"
    try:
        html = await fetch_html_httpx(search_url)
    except Exception as e:
        print(f"[mangaread] plain fetch failed ({e}), falling back to Playwright")
        try:
            html = await fetch_html_playwright(search_url)
        except Exception as e2:
            print(f"[mangaread] Playwright fallback also failed: {e2}")
            return None
    return _best_match(
        html, "div.c-tabs-item__content .post-title a", search_url, title, use_alt=False
    )


async def search_flamecomics(title: str) -> Optional[str]:
    """Find the best-matching series on flamecomics.xyz. There's no search
    endpoint at all — /browse ships its entire ~170-series catalog as one
    JSON payload, so "search" is just scoring every title in that list."""
    try:
        html = await fetch_html_httpx("https://flamecomics.xyz/browse")
    except Exception as e:
        print(f"[flamecomics] catalog fetch failed ({e})")
        return None
    props = _flamecomics_page_props(html)

    best_id, best_score = None, 0.0
    for series in props.get("series", []):
        score = title_score(title, series.get("title", ""))
        if score > best_score:
            best_score, best_id = score, series.get("series_id")

    if best_id is not None and best_score >= MATCH_THRESHOLD:
        return f"https://flamecomics.xyz/series/{best_id}"
    print(f"[flamecomics] no match for \"{title}\" cleared threshold (best score {best_score:.2f})")
    return None


async def search_manhuaplus(title: str) -> Optional[str]:
    """Find the best-matching series URL on manhuaplus.org for a title. The
    detail page's sidebar shows a fixed "Trending" list regardless of
    query, so the selector is scoped to the actual results section only —
    a looser selector would score that static sidebar every time."""
    search_url = f"https://manhuaplus.org/search?keyword={quote(title)}"
    try:
        html = await fetch_html_httpx(search_url)
    except Exception as e:
        print(f"[manhuaplus] plain fetch failed ({e}), falling back to Playwright")
        try:
            html = await fetch_html_playwright(search_url)
        except Exception as e2:
            print(f"[manhuaplus] Playwright fallback also failed: {e2}")
            return None
    return _best_match(
        html, "section.r2.s1 div.b-img a[title]", search_url, title, use_alt=False
    )


async def search_kaliscan(title: str) -> Optional[str]:
    """Find the best-matching series URL on kaliscan.io for a title."""
    search_url = f"https://kaliscan.io/search?q={quote(title)}"
    try:
        html = await fetch_html_httpx(search_url, timeout=KALISCAN_TIMEOUT)
    except Exception as e:
        print(f"[kaliscan] plain fetch failed ({e}), falling back to Playwright")
        try:
            html = await fetch_html_playwright(search_url)
        except Exception as e2:
            print(f"[kaliscan] Playwright fallback also failed: {e2}")
            return None
    return _best_match(
        html, "div.manga-list div.book-item div.title h3 a", search_url, title, use_alt=False
    )


async def search_mangakatana(title: str) -> Optional[str]:
    """Find the best-matching series URL on mangakatana.com for a title."""
    search_url = f"https://mangakatana.com/?search={quote(title)}&search_by=book_name"
    try:
        html = await fetch_html_httpx(search_url)
    except Exception as e:
        print(f"[mangakatana] plain fetch failed ({e}), falling back to Playwright")
        try:
            html = await fetch_html_playwright(search_url)
        except Exception as e2:
            print(f"[mangakatana] Playwright fallback also failed: {e2}")
            return None
    return _best_match(
        html, "#book_list > div.item h3.title a", search_url, title, use_alt=False
    )


async def search_weebcentral(title: str) -> Optional[str]:
    """Find the best-matching series URL on weebcentral.com for a title,
    via the same HTMX search endpoint the site's own search box posts to."""
    try:
        async with httpx.AsyncClient(headers=HEADERS, timeout=15.0) as client:
            response = await client.post(
                f"{WEEBCENTRAL_API}/search/simple",
                params={"location": "main"},
                data={"text": title},
            )
            response.raise_for_status()
            html = response.text
    except Exception as e:
        print(f"[weebcentral] search failed ({e})")
        return None
    return _best_match(
        html, "section#quick-search-result a", WEEBCENTRAL_API, title, use_alt=False
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
            score = title_score(title, name)
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
            score = title_score(title, name)
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
    "mangaread": (search_mangaread, scrape_mangaread_chapter_list),
    "flamecomics": (search_flamecomics, scrape_flamecomics_chapter_list),
    "manhuaplus": (search_manhuaplus, scrape_manhuaplus_chapter_list),
    "kaliscan": (search_kaliscan, scrape_kaliscan_chapter_list),
    "mangakatana": (search_mangakatana, scrape_mangakatana_chapter_list),
    "weebcentral": (search_weebcentral, scrape_weebcentral_chapter_list),
}


# /api/find waits on every source, so one slow site (kaliscan.io can take
# 20s+ per page, then a Playwright fallback on top) would hold up the whole
# answer. Past this budget a source is simply left out.
SOURCE_TIME_BUDGET = 30.0


async def _source_for(domain: str, title: str) -> Optional[Dict[str, Any]]:
    search, chapter_list = SOURCES[domain]

    async def lookup():
        series_url = await search(title)
        if not series_url:
            return None, []
        return series_url, await chapter_list(series_url)

    try:
        series_url, chapters = await asyncio.wait_for(lookup(), SOURCE_TIME_BUDGET)
    except asyncio.TimeoutError:
        print(f"[{domain}] skipped — took longer than {SOURCE_TIME_BUDGET:g}s")
        return None
    except Exception as e:
        print(f"[{domain}] lookup failed ({e})")
        return None
    if not series_url:
        return None
    if not chapters:
        return None
    return {"domain": domain, "series_url": series_url, "chapters": chapters}


async def find_best_source(title: str) -> Optional[Dict[str, Any]]:
    """Search every site for a title and read from whichever is more
    up to date — i.e. has the higher chapter number right now. Every other
    site that also has it comes back under "alternates" (best first), so
    the reader can fall back to one when a chapter won't load from the
    winner."""
    results = await asyncio.gather(*(_source_for(d, title) for d in SOURCES))
    candidates = [r for r in results if r]
    if not candidates:
        return None

    # Most chapters wins ties, since a longer list at the same latest
    # chapter means fewer gaps to hit while reading.
    candidates.sort(key=lambda c: (c["chapters"][-1]["number"], len(c["chapters"])), reverse=True)
    best = dict(candidates[0])
    best["alternates"] = candidates[1:]
    return best


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
    elif domain == "mangaread":
        images = await scrape_mangaread(url)
    elif domain == "flamecomics":
        images = await scrape_flamecomics(url)
    elif domain == "manhuaplus":
        images = await scrape_manhuaplus(url)
    elif domain == "kaliscan":
        images = await scrape_kaliscan(url)
    elif domain == "mangakatana":
        images = await scrape_mangakatana(url)
    elif domain == "weebcentral":
        images = await scrape_weebcentral(url)
    else:
        images = []

    return {
        "domain": domain,
        "chapter_url": url,
        "images": images,
    }
