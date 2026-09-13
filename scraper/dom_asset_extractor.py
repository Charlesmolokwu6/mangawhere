"""Extract sanitized, sequential image URLs from already-rendered chapter HTML.

Takes the fully rendered HTML a headless browser produced (e.g. the
output of `dom_renderer.render_chapters()`) and parses it with
BeautifulSoup — no browser needed here, the DOM is already static text at
this point. Unlike `image_extractor.extract_image_urls()`, which requires
the caller to already know a specific site's container class, this module
tries a prioritized list of CSS selectors commonly used by manga/webtoon
reader layouts and uses the first one that actually contains images,
falling back to scanning the whole document if none match.

For each `<img>` found, the real URL is resolved through the same
fallback chain as `image_extractor` — `data-src`, `data-lazy-src`,
`data-original`, `data-url`, then `src` last — skipping placeholder values
(base64 blur-ups, 1x1 gifs, etc.), and the result is de-duplicated while
preserving document order (the reading sequence).
"""

from __future__ import annotations

import argparse
import logging
import sys
from typing import Sequence
from urllib.parse import urljoin

from bs4 import BeautifulSoup

logger = logging.getLogger(__name__)

# Prioritized, generic guesses at a "reader container" — the element that
# wraps a chapter's page images — covering conventions common across many
# manga/webtoon reader layouts. The first selector that matches an element
# containing at least one <img> wins; none of these are tied to any one
# specific site.
DEFAULT_READER_SELECTORS: tuple[str, ...] = (
    "#readerarea",
    ".reader-area",
    ".reading-content",
    ".read-container",
    ".container-chapter-reader",
    ".page-break",
    ".chapter-content",
    "#chapter-images",
    ".viewer-container",
    ".entry-content",
)

# Attributes checked for each <img>, in priority order — lazy-loading
# attributes first, falling back to `src` last. Same convention as
# image_extractor.LAZY_LOAD_ATTRS.
LAZY_LOAD_ATTRS: tuple[str, ...] = (
    "data-src",
    "data-lazy-src",
    "data-original",
    "data-url",
    "src",
)

# Placeholder markers that show up in `src` while the real image hasn't
# swapped in yet (base64 blur-ups, 1x1 gifs, "loading.svg", etc.).
_PLACEHOLDER_MARKERS = ("data:image", "placeholder", "loading.gif", "blank.gif")


def _is_placeholder(url: str) -> bool:
    lowered = url.lower()
    return any(marker in lowered for marker in _PLACEHOLDER_MARKERS)


def _resolve_image_url(img, attrs: Sequence[str]) -> str | None:
    """Return the first usable, non-placeholder URL found on `img`."""
    for attr in attrs:
        value = img.get(attr)
        if not value:
            continue
        value = value.strip()
        if not value or _is_placeholder(value):
            continue
        return value
    return None


def _find_reader_container(soup: BeautifulSoup, selectors: Sequence[str]):
    """Return the first (element, selector) pair that contains an <img>."""
    for selector in selectors:
        try:
            match = soup.select_one(selector)
        except Exception as exc:  # an invalid/unsupported CSS selector string
            logger.debug("skipping invalid selector %r: %s", selector, exc)
            continue
        if match is not None and match.find("img") is not None:
            return match, selector
    return None, None


def extract_reader_images(
    html: str,
    *,
    container_selectors: Sequence[str] = DEFAULT_READER_SELECTORS,
    attrs: Sequence[str] = LAZY_LOAD_ATTRS,
    base_url: str | None = None,
    fallback_to_full_document: bool = True,
) -> list[str]:
    """Extract a clean, sequential list of image URLs from rendered HTML.

    Args:
        html: Already-rendered chapter HTML (a static string — this
            function does no browser work of its own).
        container_selectors: CSS selectors tried in order to find the
            reader container; the first one matching an element that
            contains at least one `<img>` is used.
        attrs: `<img>` attributes checked in priority order for the real
            image URL.
        base_url: If given, relative URLs are resolved against this URL
            into absolute ones.
        fallback_to_full_document: If none of `container_selectors` match,
            scan the whole document for `<img>` tags instead of returning
            an empty list.

    Returns:
        Image URLs in document order (the reading sequence), with
        duplicates and placeholder/empty values removed.
    """
    soup = BeautifulSoup(html, "html.parser")

    container, matched_selector = _find_reader_container(soup, container_selectors)
    if container is not None:
        logger.info("using reader container selector %r", matched_selector)
    elif fallback_to_full_document:
        logger.info(
            "no known reader container selector matched; scanning whole document"
        )
        container = soup
    else:
        logger.warning("no known reader container selector matched")
        return []

    urls: list[str] = []
    seen: set[str] = set()

    for img in container.find_all("img"):
        url = _resolve_image_url(img, attrs)
        if url is None:
            continue

        if base_url:
            url = urljoin(base_url, url)

        if url not in seen:
            seen.add(url)
            urls.append(url)

    logger.info("extracted %d image URL(s)", len(urls))
    return urls


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Extract sanitized, sequential image URLs from rendered chapter HTML."
    )
    parser.add_argument(
        "html_file", help="Path to a saved HTML file, or '-' to read from stdin"
    )
    parser.add_argument("--base-url", default=None, help="Base URL to resolve relative image URLs against")
    parser.add_argument(
        "--selectors",
        default=None,
        help="Comma-separated CSS selectors to try instead of the built-in defaults",
    )
    parser.add_argument(
        "--no-fallback",
        action="store_true",
        help="Return nothing (instead of scanning the whole document) if no selector matches",
    )
    parser.add_argument("-v", "--verbose", action="store_true")
    return parser.parse_args()


def main() -> None:
    args = _parse_args()
    logging.basicConfig(
        level=logging.INFO if args.verbose else logging.WARNING,
        format="%(asctime)s %(levelname)s %(message)s",
    )

    html = sys.stdin.read() if args.html_file == "-" else open(args.html_file, encoding="utf-8").read()

    selectors = (
        tuple(s.strip() for s in args.selectors.split(",") if s.strip())
        if args.selectors
        else DEFAULT_READER_SELECTORS
    )

    urls = extract_reader_images(
        html,
        container_selectors=selectors,
        base_url=args.base_url,
        fallback_to_full_document=not args.no_fallback,
    )

    for url in urls:
        print(url)
    print(f"\n{len(urls)} image URL(s) found", file=sys.stderr)


if __name__ == "__main__":
    main()
