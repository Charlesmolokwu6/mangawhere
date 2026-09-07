"""Crawl a series landing page for its chapter list, oldest chapter first.

Many manga/series landing pages render their chapter list client-side (a
JS fetch populates the list after load, sometimes behind an infinite-
scroll "load more"), so a plain HTML fetch can see an empty or partial
list. This module drives a real headless Chromium page with Playwright to
get the fully rendered DOM, then parses it with BeautifulSoup to pull out
chapter links, and orders them chronologically (oldest to newest) — most
landing pages list newest-first, so this is a real re-ordering step, not
just a pass-through.

Because chapter markup and numbering conventions vary per site, ordering
here is a best-effort heuristic:

1. Try to parse a chapter number out of each link's text or URL (e.g.
   "Chapter 12", ".../chapter-12.5/") and sort by that, ascending.
2. If too few links yield a parseable number to trust that ordering,
   fall back to reversing DOM order — the common "newest chapter listed
   first" layout — and log a warning that this is a heuristic.

Pass `chapter_pattern` to match a site's own numbering convention, or
`order="dom"` / `order="dom-reversed"` to skip the heuristic entirely once
you know a given site's listing direction.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import re
from dataclasses import dataclass
from typing import Literal, Sequence
from urllib.parse import urljoin

from bs4 import BeautifulSoup
from playwright.async_api import Browser, async_playwright

logger = logging.getLogger(__name__)

DEFAULT_NAV_TIMEOUT_MS = 30_000
DEFAULT_WAIT_UNTIL = "networkidle"

# Matches "Chapter 12", "Ch. 12.5", "Episode 3", "ep-3", etc. in link text
# or an href path segment. Case-insensitive (applied with re.IGNORECASE).
DEFAULT_CHAPTER_PATTERN = r"(?:chapter|chap|ch|episode|ep)[\s._-]*?(\d+(?:\.\d+)?)"

# Fallback when DEFAULT_CHAPTER_PATTERN finds nothing: a bare number
# occupying its own URL path segment, e.g. ".../one-piece/12" or
# ".../one-piece/12/".
_TRAILING_NUMBER_PATTERN = r"/(\d+(?:\.\d+)?)/?(?:\?.*)?$"

# Minimum fraction of links that must yield a parsed chapter number before
# we trust numeric sort over the "newest first" DOM-order heuristic.
DEFAULT_MIN_NUMBERED_RATIO = 0.6

OrderStrategy = Literal["auto", "numeric", "dom", "dom-reversed"]


@dataclass
class Chapter:
    """A single chapter link found on the series landing page."""

    url: str
    number: float | None
    label: str


def _extract_chapters(
    html: str,
    base_url: str,
    *,
    container_selector: str | None,
    link_selector: str,
    chapter_pattern: str,
) -> list[Chapter]:
    """Parse `html` and return chapter links in DOM (document) order."""
    soup = BeautifulSoup(html, "html.parser")

    scope = soup.select_one(container_selector) if container_selector else soup
    if scope is None:
        logger.warning("container_selector %r matched nothing", container_selector)
        return []

    pattern = re.compile(chapter_pattern, re.IGNORECASE)
    fallback_pattern = re.compile(_TRAILING_NUMBER_PATTERN)

    chapters: list[Chapter] = []
    seen_urls: set[str] = set()

    for link in scope.select(link_selector):
        href = link.get("href")
        if not href or href.startswith("#") or href.lower().startswith("javascript:"):
            continue

        url = urljoin(base_url, href.strip())
        if url in seen_urls:
            continue
        seen_urls.add(url)

        label = link.get_text(strip=True) or link.get("title", "") or link.get("aria-label", "")

        number = _parse_chapter_number(label, url, pattern, fallback_pattern)
        chapters.append(Chapter(url=url, number=number, label=label))

    return chapters


def _parse_chapter_number(
    label: str, url: str, pattern: re.Pattern[str], fallback_pattern: re.Pattern[str]
) -> float | None:
    for text in (label, url):
        match = pattern.search(text)
        if match:
            return float(match.group(1))

    match = fallback_pattern.search(url)
    if match:
        return float(match.group(1))

    return None


def _order_chapters(
    chapters: list[Chapter],
    *,
    order: OrderStrategy,
    min_numbered_ratio: float,
) -> list[Chapter]:
    """Return `chapters` ordered oldest-first per `order`."""
    if order == "dom":
        return chapters
    if order == "dom-reversed":
        return list(reversed(chapters))

    numbered = [c for c in chapters if c.number is not None]
    has_enough_numbers = bool(chapters) and len(numbered) / len(chapters) >= min_numbered_ratio

    if order == "numeric" or (order == "auto" and has_enough_numbers):
        if not has_enough_numbers:
            logger.warning(
                "order='numeric' requested but only %d/%d links had a parseable "
                "chapter number; sorting is unreliable — consider a custom chapter_pattern",
                len(numbered),
                len(chapters),
            )
        # Stable sort: links without a parsed number keep their relative
        # DOM position, sorted after the highest known chapter number.
        return sorted(chapters, key=lambda c: (c.number is None, c.number))

    logger.info(
        "only %d/%d links had a parseable chapter number; falling back to "
        "reversed DOM order (assumes a 'newest chapter listed first' layout)",
        len(numbered),
        len(chapters),
    )
    return list(reversed(chapters))


async def crawl_series_index(
    landing_page_url: str,
    *,
    container_selector: str | None = None,
    link_selector: str = "a[href]",
    chapter_pattern: str = DEFAULT_CHAPTER_PATTERN,
    order: OrderStrategy = "auto",
    min_numbered_ratio: float = DEFAULT_MIN_NUMBERED_RATIO,
    headless: bool = True,
    wait_until: str = DEFAULT_WAIT_UNTIL,
    nav_timeout_ms: float = DEFAULT_NAV_TIMEOUT_MS,
    user_agent: str | None = None,
    executable_path: str | None = None,
) -> list[str]:
    """Load a series landing page and return its chapter URLs, oldest first.

    Args:
        landing_page_url: URL of the series' main/landing page.
        container_selector: CSS selector scoping the search to the element
            that holds the chapter list (e.g. `".chapter-list"`). Searches
            the whole page if omitted.
        link_selector: CSS selector for chapter links within the scope.
        chapter_pattern: Regex (with one capture group) used to pull a
            chapter number out of a link's text or URL. Override this to
            match a specific site's numbering convention.
        order: `"auto"` (numeric sort if enough links yield a parseable
            number, else reversed DOM order), `"numeric"` (force numeric
            sort), `"dom"` (keep listed order as-is), or `"dom-reversed"`
            (reverse listed order — for known newest-first listings).
        min_numbered_ratio: For `order="auto"`, the minimum fraction of
            links that must have a parseable number to trust numeric sort.
        headless: Run Chromium headless.
        wait_until: Playwright navigation wait condition — `"networkidle"`
            gives client-side-rendered chapter lists time to finish loading.
        nav_timeout_ms: Timeout for the initial navigation.
        user_agent: Optional custom User-Agent for the browser context.
        executable_path: Optional path to a specific Chromium/Chrome binary.

    Returns:
        Chapter URLs ordered oldest to newest (best-effort — see module
        docstring for the ordering heuristic and its limits).
    """
    async with async_playwright() as playwright:
        browser: Browser = await playwright.chromium.launch(
            headless=headless, executable_path=executable_path
        )
        try:
            context = await browser.new_context(user_agent=user_agent)
            page = await context.new_page()
            try:
                await page.goto(
                    landing_page_url, timeout=nav_timeout_ms, wait_until=wait_until
                )
                html = await page.content()
                base_url = page.url  # final URL, after any redirects
            finally:
                await context.close()
        finally:
            await browser.close()

    chapters = _extract_chapters(
        html,
        base_url,
        container_selector=container_selector,
        link_selector=link_selector,
        chapter_pattern=chapter_pattern,
    )
    ordered = _order_chapters(chapters, order=order, min_numbered_ratio=min_numbered_ratio)

    logger.info("found %d chapter(s) for %s", len(ordered), landing_page_url)
    return [chapter.url for chapter in ordered]


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Crawl a series landing page and list its chapter URLs, oldest first."
    )
    parser.add_argument("url", help="Series landing page URL")
    parser.add_argument(
        "--container", default=None, help="CSS selector scoping the search to the chapter list"
    )
    parser.add_argument("--link-selector", default="a[href]", help="CSS selector for chapter links")
    parser.add_argument("--chapter-pattern", default=DEFAULT_CHAPTER_PATTERN)
    parser.add_argument(
        "--order",
        choices=["auto", "numeric", "dom", "dom-reversed"],
        default="auto",
    )
    parser.add_argument("--headed", action="store_true", help="Run with a visible browser window")
    parser.add_argument("--user-agent", default=None)
    parser.add_argument("--executable-path", default=None)
    parser.add_argument("-v", "--verbose", action="store_true")
    return parser.parse_args()


async def _main_async() -> None:
    args = _parse_args()
    logging.basicConfig(
        level=logging.INFO if args.verbose else logging.WARNING,
        format="%(asctime)s %(levelname)s %(message)s",
    )

    urls: Sequence[str] = await crawl_series_index(
        args.url,
        container_selector=args.container,
        link_selector=args.link_selector,
        chapter_pattern=args.chapter_pattern,
        order=args.order,
        headless=not args.headed,
        user_agent=args.user_agent,
        executable_path=args.executable_path,
    )

    for url in urls:
        print(url)


def main() -> None:
    asyncio.run(_main_async())


if __name__ == "__main__":
    main()
