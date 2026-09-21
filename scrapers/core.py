import re
from typing import Any, Dict, Iterable, List
from urllib.parse import urljoin

import httpx
from bs4 import BeautifulSoup
from playwright.async_api import async_playwright

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


async def fetch_html_httpx(url: str) -> str:
    """Fast standard fetch path for normal HTML pages."""
    async with httpx.AsyncClient(headers=HEADERS, follow_redirects=True, timeout=15.0) as client:
        response = await client.get(url)
        lowered = response.text.lower()
        # Cloudflare's actual interstitial challenge page — not just any
        # page that happens to load Cloudflare's (very common) analytics
        # beacon script, which the bare substring "cloudflare" also matches.
        is_challenge_page = (
            "just a moment" in lowered
            or "cf-browser-verification" in lowered
            or "cf_chl_opt" in lowered
        )
        if response.status_code in (403, 429) or is_challenge_page:
            raise PermissionError("Cloudflare protection active")
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


CHAPTER_NUMBER_IN_HREF = re.compile(r"chapter[-/](\d+(?:\.\d+)?)", re.I)
CHAPTER_NUMBER_IN_TEXT = re.compile(r"(\d+(?:\.\d+)?)\s*$")


def extract_chapter_list(html: str, selector: str, base_url: str) -> List[Dict[str, Any]]:
    """Collect a series page's chapter links, deduplicated and sorted by
    chapter number. Numbers are read from the URL first (reliable on both
    sites' predictable /chapter-N/ and /chapter/N paths) and only fall
    back to the link text for markup that doesn't follow that pattern."""
    soup = BeautifulSoup(html, "html.parser")
    chapters: Dict[float, Dict[str, Any]] = {}

    for a in soup.select(selector):
        href = a.get("href")
        if not href:
            continue
        text = a.get_text(" ", strip=True)
        match = CHAPTER_NUMBER_IN_HREF.search(href) or CHAPTER_NUMBER_IN_TEXT.search(text)
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


async def scrape_series(url: str) -> Dict[str, Any]:
    """Auto-detect the site and return the series' chapter list."""
    lowered = (url or "").lower()

    if "toongod" in lowered:
        domain = "toongod"
        chapters = await scrape_toongod_chapter_list(url)
    elif "asurascans" in lowered or "asura" in lowered:
        domain = "asurascans"
        chapters = await scrape_asurascans_chapter_list(url)
    else:
        domain = "unknown"
        chapters = []

    return {"domain": domain, "series_url": url, "chapters": chapters}


async def scrape_chapter(url: str) -> Dict[str, Any]:
    """Auto-detect the site and return the normalized chapter payload."""
    lowered = (url or "").lower()

    if "toongod" in lowered:
        domain = "toongod"
        images = await scrape_toongod(url)
    elif "asurascans" in lowered or "asura" in lowered:
        domain = "asurascans"
        images = await scrape_asurascans(url)
    else:
        domain = "unknown"
        images = []

    return {
        "domain": domain,
        "chapter_url": url,
        "images": images,
    }
