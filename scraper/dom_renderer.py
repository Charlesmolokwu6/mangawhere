"""Render chapter pages with headless Playwright and capture the full DOM.

Chapter pages that lazy-load their page images (via `IntersectionObserver`
or a scroll listener) only mount the real `<img>` elements — or swap real
URLs into placeholder ones — once the browser actually scrolls them into
view. This module opens each chapter URL in headless Chromium, scrolls it
incrementally to trigger those mounts (see `_scroll_to_bottom` — a single
jump to the bottom would skip elements that never pass through the
viewport), and returns `page.content()`: the fully rendered HTML, ready to
hand to something like `image_extractor.extract_image_urls()`.

One browser (and one shared context) is used for the whole batch of
chapter URLs; each gets its own page, rendered under an
`asyncio.Semaphore` cap, and a failure on one chapter (bad URL, HTTP error
status, timeout, navigation error) is caught and reported as a failed
`RenderResult` without aborting the rest of the batch.

Caveat: rendering several pages concurrently in one browser means one
page can occasionally be starved of a render turn right as it's finishing
up, so a single element's lazy-load callback lands a beat late. Waiting on
this page's own next animation frames (rather than a flat sleep) plus one
bounded extra grace round when a lazy-load marker attribute is still
present (see `_LAZY_LOAD_MARKER_ATTRS`) closes most of that gap — testing
this against a synthetic page took it from consistently missing an
element at `concurrency=2` down to roughly 1 in 40 renders. It is not
fully eliminated: `concurrency=1` removes the contention entirely if a
given site needs guaranteed-complete renders more than it needs
throughput.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Sequence

from playwright.async_api import Browser, BrowserContext, Page, async_playwright

logger = logging.getLogger(__name__)

DEFAULT_SCROLL_PAUSE = 0.5  # seconds to let lazy-load handlers fire after each scroll
DEFAULT_MAX_SCROLLS = 50  # hard cap so an infinite-scroll page can't loop forever
DEFAULT_STABLE_ROUNDS = 3  # consecutive no-growth scrolls before we call it "done"
DEFAULT_WAIT_UNTIL = "domcontentloaded"
DEFAULT_NAV_TIMEOUT_MS = 30_000
DEFAULT_CONCURRENCY = 3  # simultaneous chapter pages open at once


@dataclass
class RenderResult:
    """Outcome of rendering a single chapter URL."""

    url: str
    ok: bool
    html: str | None = None
    error: str | None = None


# Common lazy-loading marker attributes (same vocabulary as
# image_extractor.LAZY_LOAD_ATTRS). Used only as a generic "did this page
# actually finish swapping in its real sources" signal — not tied to any
# one site's markup.
_LAZY_LOAD_MARKER_ATTRS = ("data-src", "data-lazy-src", "data-original", "data-url", "data-lazy")


async def _has_pending_lazy_attrs(page: Page) -> bool:
    """True if any element still carries an un-swapped lazy-load attribute."""
    selector = ", ".join(f"[{attr}]" for attr in _LAZY_LOAD_MARKER_ATTRS)
    count = await page.eval_on_selector_all(selector, "elements => elements.length")
    return count > 0


async def _wait_for_next_frame(page: Page) -> None:
    """Wait for this page to render two animation frames.

    A double `requestAnimationFrame` resolves only once this page's own
    render pipeline has actually run — unlike `wait_for_timeout`, which
    elapses on a wall clock regardless of whether this page got a chance
    to paint. That makes it a reliable way to let a same-frame callback
    (like an `IntersectionObserver` firing) complete, even when this page
    is one of several rendering concurrently and briefly starved for a
    render turn.
    """
    await page.evaluate(
        "() => new Promise((resolve) => "
        "requestAnimationFrame(() => requestAnimationFrame(resolve)))"
    )


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
        # Wait for this page's own next paint before anything else. An
        # IntersectionObserver callback fires on this page's rendering
        # pipeline, not on a wall-clock schedule — when multiple pages
        # render concurrently in the same browser and one is momentarily
        # starved for a render turn, a fixed sleep can elapse before that
        # page ever gets a frame, silently missing the callback. Waiting
        # on two animation frames instead blocks until this specific page
        # has actually rendered, regardless of what else is contending for
        # the process.
        await _wait_for_next_frame(page)
        # Then give lazy-load handlers (IntersectionObserver, scroll
        # listeners) and any media requests they kick off a bit more time.
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


async def _render_one(
    context: BrowserContext,
    semaphore: asyncio.Semaphore,
    url: str,
    *,
    scroll_pause: float,
    max_scrolls: int,
    stable_rounds: int,
    wait_until: str,
    nav_timeout_ms: float,
) -> RenderResult:
    """Open `url` in a fresh page, scroll it, and capture the rendered HTML."""
    async with semaphore:
        page: Page | None = None
        try:
            page = await context.new_page()
            response = await page.goto(url, timeout=nav_timeout_ms, wait_until=wait_until)

            # Playwright does NOT raise on an HTTP error status (404, 500,
            # ...) by default — goto() only raises on network-level
            # failures (DNS, connection refused, timeout). Without this
            # check a broken chapter URL would "succeed" with whatever
            # error page the server returned.
            if response is not None and not response.ok:
                return RenderResult(
                    url=url,
                    ok=False,
                    error=f"HTTP {response.status} {response.status_text}",
                )

            await _scroll_to_bottom(
                page,
                scroll_pause=scroll_pause,
                max_scrolls=max_scrolls,
                stable_rounds=stable_rounds,
            )
            # One more grace period after the loop decides the page is
            # "stable": the very last scroll step's IntersectionObserver
            # callback has no following step to catch it if it lands late,
            # so without this an element right at the final viewport edge
            # can occasionally still show its placeholder in page.content().
            await _wait_for_next_frame(page)
            await page.wait_for_timeout(scroll_pause * 1000)

            # Belt-and-suspenders: if something is still mid-swap (e.g.
            # this page was briefly starved for a render turn by another
            # page rendering concurrently in the same browser), give it
            # exactly one more bounded grace round rather than guessing a
            # bigger fixed pause up front for every render.
            if await _has_pending_lazy_attrs(page):
                logger.info("%s: lazy attributes still pending after settling, one more grace round", url)
                await _wait_for_next_frame(page)
                await page.wait_for_timeout(scroll_pause * 1000)

            html = await page.content()
            logger.info("rendered %s (%d bytes)", url, len(html))
            return RenderResult(url=url, ok=True, html=html)

        except Exception as exc:
            # Broad on purpose: Playwright raises its own TimeoutError /
            # Error subclasses for navigation failures, DNS errors, closed
            # targets, etc. — any of them should fail just this one URL,
            # not the whole batch.
            logger.warning("failed to render %s: %s", url, exc)
            return RenderResult(url=url, ok=False, error=str(exc))

        finally:
            if page is not None:
                await page.close()


async def render_chapters(
    urls: Sequence[str],
    *,
    concurrency: int = DEFAULT_CONCURRENCY,
    scroll_pause: float = DEFAULT_SCROLL_PAUSE,
    max_scrolls: int = DEFAULT_MAX_SCROLLS,
    stable_rounds: int = DEFAULT_STABLE_ROUNDS,
    wait_until: str = DEFAULT_WAIT_UNTIL,
    nav_timeout_ms: float = DEFAULT_NAV_TIMEOUT_MS,
    headless: bool = True,
    user_agent: str | None = None,
    executable_path: str | None = None,
) -> list[RenderResult]:
    """Render each chapter URL's fully-scrolled DOM and return its HTML.

    Args:
        urls: Chapter page URLs to render, in order.
        concurrency: Maximum number of chapter pages open at once.
        scroll_pause: Seconds to wait after each scroll step for lazy-load
            handlers to run.
        max_scrolls: Upper bound on scroll iterations per page.
        stable_rounds: Consecutive scrolls with no height growth before a
            page is considered fully scrolled.
        wait_until: Playwright navigation wait condition for each chapter.
        nav_timeout_ms: Timeout for each chapter's initial navigation.
        headless: Run Chromium headless.
        user_agent: Optional custom User-Agent for the shared browser context.
        executable_path: Optional path to a specific Chromium/Chrome binary.

    Returns:
        One `RenderResult` per URL, in the same order as `urls`. Callers
        should check `result.ok` — a failed render does not raise.
    """
    if concurrency < 1:
        raise ValueError("concurrency must be at least 1")

    async with async_playwright() as playwright:
        browser: Browser = await playwright.chromium.launch(
            headless=headless, executable_path=executable_path
        )
        try:
            context = await browser.new_context(user_agent=user_agent)
            try:
                semaphore = asyncio.Semaphore(concurrency)
                tasks = [
                    _render_one(
                        context,
                        semaphore,
                        url,
                        scroll_pause=scroll_pause,
                        max_scrolls=max_scrolls,
                        stable_rounds=stable_rounds,
                        wait_until=wait_until,
                        nav_timeout_ms=nav_timeout_ms,
                    )
                    for url in urls
                ]
                results = await asyncio.gather(*tasks)
            finally:
                await context.close()
        finally:
            await browser.close()

    succeeded = sum(result.ok for result in results)
    logger.info("%d/%d chapters rendered", succeeded, len(results))
    return list(results)


async def render_chapter_html(url: str, **kwargs) -> str:
    """Convenience wrapper: render a single URL and return its HTML.

    Raises `RuntimeError` if the render failed. For batches, prefer
    `render_chapters()` so one bad URL doesn't raise out of the whole set.
    """
    [result] = await render_chapters([url], **kwargs)
    if not result.ok:
        raise RuntimeError(f"failed to render {url}: {result.error}")
    return result.html  # type: ignore[return-value]  # ok=True guarantees html is set


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Render chapter URLs with headless Playwright, scrolled to trigger lazy-loaded media."
    )
    parser.add_argument("urls", nargs="+", help="Chapter page URLs to render")
    parser.add_argument(
        "--out-dir",
        default=None,
        help="Directory to save each rendered page's HTML into (001.html, 002.html, ...); prints a summary if omitted",
    )
    parser.add_argument("--concurrency", type=int, default=DEFAULT_CONCURRENCY)
    parser.add_argument("--scroll-pause", type=float, default=DEFAULT_SCROLL_PAUSE)
    parser.add_argument("--max-scrolls", type=int, default=DEFAULT_MAX_SCROLLS)
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

    results = await render_chapters(
        args.urls,
        concurrency=args.concurrency,
        scroll_pause=args.scroll_pause,
        max_scrolls=args.max_scrolls,
        headless=not args.headed,
        user_agent=args.user_agent,
        executable_path=args.executable_path,
    )

    if args.out_dir:
        out_dir = Path(args.out_dir)
        out_dir.mkdir(parents=True, exist_ok=True)
        width = max(3, len(str(len(results))))
        for index, result in enumerate(results, start=1):
            if result.ok:
                path = out_dir / f"{index:0{width}d}.html"
                path.write_text(result.html, encoding="utf-8")
                print(f"OK   {result.url} -> {path}")
            else:
                print(f"FAIL {result.url}: {result.error}")
    else:
        for result in results:
            if result.ok:
                print(f"OK   {result.url} ({len(result.html)} bytes)")
            else:
                print(f"FAIL {result.url}: {result.error}")

    failures = [r for r in results if not r.ok]
    print(f"\n{len(results) - len(failures)}/{len(results)} rendered")
    if failures:
        raise SystemExit(1)


def main() -> None:
    asyncio.run(_main_async())


if __name__ == "__main__":
    main()
