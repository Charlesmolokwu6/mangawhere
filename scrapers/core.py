import re
from typing import Any, Dict, Iterable, List

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
        if any(keyword in lower for keyword in IGNORE_KEYWORDS):
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
        if response.status_code in (403, 429) or "just a moment" in lowered or "cloudflare" in lowered:
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
    """Scrape Asura Scans chapter pages, using browser fallback when Cloudflare blocks access."""
    try:
        html = await fetch_html_playwright(url)
    except Exception:
        try:
            html = await fetch_html_httpx(url)
        except Exception:
            html = ""

    selectors = [
        "#readerarea img",
        "#chapter-images img",
    ]
    return extract_image_urls_from_html(html or "", selectors)


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
