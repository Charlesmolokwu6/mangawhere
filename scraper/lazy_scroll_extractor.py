"""Extract lazy-loaded image URLs from a page by scrolling it with Playwright.

Many pages only populate `<img src>` (or swap in the real URL from
`data-src`/`srcset`) once the element scrolls into the viewport, via an
`IntersectionObserver` or a scroll listener. A plain HTTP fetch of the
page's HTML sees only the placeholders. This script drives a real headless
Chromium page instead: it navigates to the URL, repeatedly scrolls to the
bottom (waiting between scrolls for the resulting network activity and DOM
updates to settle), then reads each `<img>` element's resolved `src` /
`currentSrc` DOM property — which the browser has already turned into a
fully-qualified absolute URL, unlike the raw `src` attribute which may
still be relative or a lazy-load placeholder at parse time.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys

from playwright.async_api import Browser, Page, async_playwright

logger = logging.getLogger(__name__)

DEFAULT_SCROLL_PAUSE = 0.5  # seconds to let lazy-load handlers fire after each scroll
DEFAULT_MAX_SCROLLS = 50  # hard cap so an infinite-scroll page can't loop forever
DEFAULT_STABLE_ROUNDS = 2  # consecutive no-growth scrolls before we call it "done"
DEFAULT_NAV_TIMEOUT_MS = 30_000

# JS that returns each <img>'s fully resolved source. `currentSrc` reflects
# whichever `srcset` candidate the browser actually picked (or the resolved
# `src` for a plain <img>); it is empty for an image that hasn't started
# loading, so we fall back to the `.src` DOM property (also browser-resolved
# to an absolute URL, unlike the raw attribute).
_EXTRACT_IMAGE_URLS_JS = """
() => Array.from(document.images, (img) => img.currentSrc || img.src)
"""


async def _scroll_to_bottom(
    page: Page,
    *,
    scroll_pause: float,
    max_scrolls: int,
    stable_rounds: int,
) -> None:
    """Scroll `page` down one viewport height at a time until it stops growing.

    Stepping by `window.innerHeight` (rather than jumping straight to
    `document.body.scrollHeight`) matters: a single jump to the bottom
    never puts elements in the middle of the page through the viewport, so
    an `IntersectionObserver`-based lazy loader never fires for them.
    Scrolling incrementally, like a real user, visits every position.

    Stops once we're at the bottom and the page height hasn't grown for
    `stable_rounds` consecutive steps, or `max_scrolls` is reached —
    whichever comes first — so a genuinely infinite-scroll page can't hang
    the script.
    """
    previous_height = await page.evaluate("document.body.scrollHeight")
    stable_count = 0

    for scroll_index in range(1, max_scrolls + 1):
        await page.evaluate("window.scrollBy(0, window.innerHeight)")
        # Let lazy-load callbacks (IntersectionObserver, scroll listeners)
        # fire and any resulting image requests start.
        await page.wait_for_timeout(scroll_pause * 1000)
        try:
            await page.wait_for_load_state("networkidle", timeout=5_000)
        except Exception:
            # Some pages never go fully idle (polling, websockets); that's
            # fine, the fixed pause above already gave handlers a chance.
            pass

        current_height = await page.evaluate("document.body.scrollHeight")
        reached_bottom = await page.evaluate(
            "window.scrollY + window.innerHeight >= document.body.scrollHeight"
        )

        if current_height <= previous_height and reached_bottom:
            stable_count += 1
            if stable_count >= stable_rounds:
                logger.info(
                    "page height stable at %dpx after %d scrolls, stopping",
                    current_height,
                    scroll_index,
                )
                return
        else:
            stable_count = 0

        previous_height = current_height

    logger.warning("reached max_scrolls=%d without the page settling", max_scrolls)


def _clean_urls(raw_urls: list[str]) -> list[str]:
    """De-duplicate and drop empty/placeholder entries, preserving order."""
    cleaned: list[str] = []
    seen: set[str] = set()

    for url in raw_urls:
        if not url or url.startswith("data:"):
            continue
        if url not in seen:
            seen.add(url)
            cleaned.append(url)

    return cleaned


async def extract_lazy_loaded_images(
    url: str,
    *,
    headless: bool = True,
    scroll_pause: float = DEFAULT_SCROLL_PAUSE,
    max_scrolls: int = DEFAULT_MAX_SCROLLS,
    stable_rounds: int = DEFAULT_STABLE_ROUNDS,
    nav_timeout_ms: float = DEFAULT_NAV_TIMEOUT_MS,
    user_agent: str | None = None,
    executable_path: str | None = None,
) -> list[str]:
    """Load `url`, scroll it to the bottom, and return resolved image URLs.

    Args:
        url: Page to load.
        headless: Run Chromium headless (set False only for local debugging).
        scroll_pause: Seconds to wait after each scroll for lazy-load
            handlers to run.
        max_scrolls: Upper bound on scroll iterations.
        stable_rounds: Consecutive scrolls with no height growth before the
            page is considered fully scrolled.
        nav_timeout_ms: Timeout for the initial page navigation.
        user_agent: Optional custom User-Agent for the browser context.
        executable_path: Optional path to a specific Chromium/Chrome binary,
            for environments with a pre-installed browser that doesn't match
            the version Playwright would otherwise try to download.

    Returns:
        A de-duplicated, order-preserving list of absolute image URLs.
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
                    url, timeout=nav_timeout_ms, wait_until="domcontentloaded"
                )

                await _scroll_to_bottom(
                    page,
                    scroll_pause=scroll_pause,
                    max_scrolls=max_scrolls,
                    stable_rounds=stable_rounds,
                )

                raw_urls: list[str] = await page.evaluate(_EXTRACT_IMAGE_URLS_JS)
                urls = _clean_urls(raw_urls)
                logger.info("extracted %d image URL(s) from %s", len(urls), url)
                return urls
            finally:
                # Always close the context, even if navigation/scrolling
                # raised, so no page/handles are left dangling.
                await context.close()
        finally:
            await browser.close()


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Scroll a page to the bottom and extract resolved lazy-loaded image URLs."
    )
    parser.add_argument("url", help="Page URL to load")
    parser.add_argument(
        "--headed", action="store_true", help="Run with a visible browser window (debugging)"
    )
    parser.add_argument(
        "--scroll-pause",
        type=float,
        default=DEFAULT_SCROLL_PAUSE,
        help="Seconds to wait after each scroll",
    )
    parser.add_argument("--max-scrolls", type=int, default=DEFAULT_MAX_SCROLLS)
    parser.add_argument("--user-agent", default=None, help="Custom User-Agent string")
    parser.add_argument(
        "--executable-path", default=None, help="Path to a specific Chromium/Chrome binary"
    )
    parser.add_argument("-v", "--verbose", action="store_true", help="Enable info-level logging")
    return parser.parse_args()


async def _main_async() -> None:
    args = _parse_args()
    logging.basicConfig(
        level=logging.INFO if args.verbose else logging.WARNING,
        format="%(asctime)s %(levelname)s %(message)s",
    )

    urls = await extract_lazy_loaded_images(
        args.url,
        headless=not args.headed,
        scroll_pause=args.scroll_pause,
        max_scrolls=args.max_scrolls,
        user_agent=args.user_agent,
        executable_path=args.executable_path,
    )

    for image_url in urls:
        print(image_url)
    print(f"\n{len(urls)} image URL(s) found", file=sys.stderr)


def main() -> None:
    asyncio.run(_main_async())


if __name__ == "__main__":
    main()
