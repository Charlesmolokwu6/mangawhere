"""Crawl a site's catalog/browse page into a list of (title, URL) entries.

The rest of this pipeline starts from one series at a time —
`index_crawler.crawl_series_index()` takes a single series landing page.
This module is the piece before that: it reads a site's catalog/browse
page (which lists many series at once, sometimes hundreds or thousands)
and returns the series names and URLs on it, so a caller can loop over
them and feed each one into `index_crawler` in turn.

Catalog pages are laid out very differently across sites, so — same
approach as `dom_asset_extractor.py`'s reader-container selectors — this
takes a configurable strategy rather than assuming one layout:

  "links"          A flat grid/list: every <a> matching `link_selector`
                    within `container_selector` is a series link.

  "labeled-group"   Titles grouped under a text label repeated many times
                    on the page (e.g. a publisher-by-publisher listing
                    where each publisher's block has a "Titles:" row
                    followed by that publisher's series links). Finds
                    every element matching `label_tag` whose text equals
                    `label_text`, then collects the <a> tags inside its
                    next `value_tag` sibling.

Developed and verified against a real catalog page (Comic Book Plus's
public-domain-comics index, which groups ~1,700 series by publisher on
one page with no pagination) saved to a local file first — one fetch,
then iterated on locally, rather than re-requesting the live page on
every test run.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import re
from dataclasses import dataclass
from typing import Sequence
from urllib.parse import urljoin

from bs4 import BeautifulSoup
from bs4.element import Tag
from playwright.async_api import Browser, async_playwright

logger = logging.getLogger(__name__)

DEFAULT_NAV_TIMEOUT_MS = 30_000
DEFAULT_WAIT_UNTIL = "domcontentloaded"

# Strips a trailing "(N)" issue/item count some catalog pages append to
# each title's link text, e.g. "Baffling Mysteries (24)" -> "Baffling
# Mysteries". Harmless no-op on titles that don't have one.
_TRAILING_COUNT = re.compile(r"\s*\(\d+\)\s*$")


@dataclass
class CatalogEntry:
    """One series found on a catalog/browse page."""

    title: str
    url: str


def _labeled_group_links(soup: BeautifulSoup, *, label_tag: str, label_text: str, value_tag: str) -> list[Tag]:
    """Collect <a> tags from every `value_tag` that immediately follows
    a `label_tag` element whose text exactly matches `label_text`."""
    links: list[Tag] = []
    for label in soup.find_all(label_tag):
        if label.get_text(strip=True) == label_text:
            value = label.find_next_sibling(value_tag)
            if value:
                links.extend(value.find_all("a", href=True))
    return links


def extract_catalog(
    html: str,
    base_url: str,
    *,
    strategy: str = "links",
    container_selector: str | None = None,
    link_selector: str = "a[href]",
    label_tag: str = "td",
    label_text: str = "Titles:",
    value_tag: str = "td",
    strip_trailing_count: bool = True,
) -> list[CatalogEntry]:
    """Parse a catalog page's HTML into a de-duplicated list of series.

    Args:
        html: The catalog page's HTML.
        base_url: Used to resolve relative hrefs to absolute URLs.
        strategy: `"links"` (a flat container of series links) or
            `"labeled-group"` (titles grouped under a repeated label —
            see module docstring).
        container_selector, link_selector: Used by the `"links"` strategy.
        label_tag, label_text, value_tag: Used by the `"labeled-group"`
            strategy.
        strip_trailing_count: Strip a trailing `"(N)"` count from each
            link's text (common on these catalog pages).

    Returns:
        `CatalogEntry` list in document order, de-duplicated by URL.
    """
    soup = BeautifulSoup(html, "html.parser")

    if strategy == "labeled-group":
        anchors = _labeled_group_links(soup, label_tag=label_tag, label_text=label_text, value_tag=value_tag)
    elif strategy == "links":
        scope = soup.select_one(container_selector) if container_selector else soup
        anchors = scope.select(link_selector) if scope is not None else []
    else:
        raise ValueError(f"unknown strategy: {strategy!r} (expected 'links' or 'labeled-group')")

    entries: list[CatalogEntry] = []
    seen_urls: set[str] = set()

    for a in anchors:
        href = a.get("href")
        if not href or href.startswith("#") or href.lower().startswith("javascript:"):
            continue

        url = urljoin(base_url, href.strip())
        title = a.get_text(strip=True)
        if strip_trailing_count:
            title = _TRAILING_COUNT.sub("", title).strip()
        if not title:
            continue
        if url in seen_urls:
            continue

        seen_urls.add(url)
        entries.append(CatalogEntry(title=title, url=url))

    logger.info("extracted %d catalog entr%s", len(entries), "y" if len(entries) == 1 else "ies")
    return entries


async def crawl_catalog_index(
    catalog_url: str,
    *,
    strategy: str = "links",
    container_selector: str | None = None,
    link_selector: str = "a[href]",
    label_tag: str = "td",
    label_text: str = "Titles:",
    value_tag: str = "td",
    strip_trailing_count: bool = True,
    headless: bool = True,
    wait_until: str = DEFAULT_WAIT_UNTIL,
    nav_timeout_ms: float = DEFAULT_NAV_TIMEOUT_MS,
    user_agent: str | None = None,
    executable_path: str | None = None,
) -> list[CatalogEntry]:
    """Load a catalog page with headless Playwright and extract its series list.

    Same navigation shape as `index_crawler.crawl_series_index()` — see
    its docstring for the shared parameters. `extract_catalog()` above
    does the actual parsing and is usable standalone against saved HTML,
    which is how this module was developed and tested.
    """
    async with async_playwright() as playwright:
        browser: Browser = await playwright.chromium.launch(
            headless=headless, executable_path=executable_path
        )
        try:
            context = await browser.new_context(user_agent=user_agent)
            page = await context.new_page()
            try:
                await page.goto(catalog_url, timeout=nav_timeout_ms, wait_until=wait_until)
                html = await page.content()
                base_url = page.url
            finally:
                await context.close()
        finally:
            await browser.close()

    return extract_catalog(
        html,
        base_url,
        strategy=strategy,
        container_selector=container_selector,
        link_selector=link_selector,
        label_tag=label_tag,
        label_text=label_text,
        value_tag=value_tag,
        strip_trailing_count=strip_trailing_count,
    )


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Crawl a site's catalog/browse page into a list of series (title, URL)."
    )
    parser.add_argument("url", help="Catalog/browse page URL")
    parser.add_argument("--strategy", choices=["links", "labeled-group"], default="links")
    parser.add_argument("--container", default=None, help="[links] CSS selector scoping the search")
    parser.add_argument("--link-selector", default="a[href]", help="[links] CSS selector for series links")
    parser.add_argument("--label-tag", default="td", help="[labeled-group] tag holding the label text")
    parser.add_argument("--label-text", default="Titles:", help="[labeled-group] label text to match exactly")
    parser.add_argument("--value-tag", default="td", help="[labeled-group] sibling tag holding the links")
    parser.add_argument("--no-strip-count", action="store_true", help="Keep a trailing '(N)' in title text")
    parser.add_argument("--limit", type=int, default=None, help="Only keep the first N entries")
    parser.add_argument("--out-json", default=None, help="Write results as JSON to this path instead of stdout")
    parser.add_argument("--headed", action="store_true")
    parser.add_argument("--executable-path", default=None)
    parser.add_argument("-v", "--verbose", action="store_true")
    return parser.parse_args()


async def _main_async() -> None:
    args = _parse_args()
    logging.basicConfig(
        level=logging.INFO if args.verbose else logging.WARNING,
        format="%(asctime)s %(levelname)s %(message)s",
    )

    entries = await crawl_catalog_index(
        args.url,
        strategy=args.strategy,
        container_selector=args.container,
        link_selector=args.link_selector,
        label_tag=args.label_tag,
        label_text=args.label_text,
        value_tag=args.value_tag,
        strip_trailing_count=not args.no_strip_count,
        headless=not args.headed,
        executable_path=args.executable_path,
    )

    if args.limit is not None:
        entries = entries[: args.limit]

    if args.out_json:
        with open(args.out_json, "w", encoding="utf-8") as f:
            json.dump([{"title": e.title, "url": e.url} for e in entries], f, indent=2)
        print(f"wrote {len(entries)} entries to {args.out_json}")
    else:
        for e in entries:
            print(f"{e.title}\t{e.url}")
        print(f"\n{len(entries)} series found", flush=True)


def main() -> None:
    asyncio.run(_main_async())


if __name__ == "__main__":
    main()
