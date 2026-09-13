"""Extract page image URLs from a "numbered sequence" comic viewer.

Some comic-reader pages don't put every page's `<img>` in the DOM at
once (what `dom_asset_extractor.py` expects) — only the current page is
loaded, and flipping to the next one swaps the same `<img>`'s `src` to
`{base}/{n}.{ext}` via JS. But the total page count and that base URL
are both already present in the page's *initial*, unrendered HTML (an
inline `<script>` variable for the count, and the first page's `src`
attribute for the base) — so the full page list can be derived directly
with two regexes and a URL pattern. No browser needed for pages like
this at all, which is a meaningfully lighter fetch than the rest of this
pipeline's Playwright-based stages.

Built and verified against a real site's comic-viewer page during
development (see this module's commit message for specifics) rather
than a synthetic fixture, including confirming the derived URLs for
pages beyond the first are Referer-gated (return the real image with a
same-site Referer, 403 without one) — exactly what
`media_client.py`/`series_downloader.py` already send.
"""

from __future__ import annotations

import argparse
import logging
import re
from urllib.parse import urljoin

logger = logging.getLogger(__name__)

# Matches a JS assignment like "comicnumpages=36" or "comicnumpages = 36;"
# giving the total page count.
DEFAULT_PAGE_COUNT_PATTERN = r"comicnumpages\s*=\s*(\d+)"

# Matches the current-page <img>'s src attribute, tolerant of attribute
# order (src before or after id="...").
DEFAULT_IMAGE_SRC_PATTERNS = (
    r'id="maincomic"[^>]*\bsrc="([^"]+)"',
    r'<img[^>]*\bsrc="([^"]+)"[^>]*\bid="maincomic"',
)


def extract_sequential_pages(
    html: str,
    base_url: str,
    *,
    page_count_pattern: str = DEFAULT_PAGE_COUNT_PATTERN,
    image_src_patterns: tuple[str, ...] = DEFAULT_IMAGE_SRC_PATTERNS,
    start_index: int = 0,
) -> list[str]:
    """Derive every page's image URL from a page-count var + one sample src.

    Args:
        html: The comic-viewer page's HTML (a plain fetch is enough —
            this doesn't require JS execution).
        base_url: Used to resolve a relative sample `src` to absolute.
        page_count_pattern: Regex (one capture group) matching the total
            page count somewhere in an inline `<script>`.
        image_src_patterns: Regexes (one capture group each) tried in
            order to find the currently-loaded page image's `src`.
        start_index: The numbering the site starts pages at (0 for the
            site this was built against; override if a given site's
            viewer starts at 1).

    Returns:
        Page image URLs in reading order. Empty if the page count or a
        sample image src couldn't be found — this page likely isn't a
        viewer of this kind.
    """
    count_match = re.search(page_count_pattern, html)
    if not count_match:
        logger.warning("page_count_pattern found no match; not a recognized viewer page?")
        return []
    count = int(count_match.group(1))

    sample_src = None
    for pattern in image_src_patterns:
        match = re.search(pattern, html)
        if match:
            sample_src = urljoin(base_url, match.group(1))
            break
    if sample_src is None:
        logger.warning("no image_src_patterns matched; not a recognized viewer page?")
        return []

    base, _, filename = sample_src.rpartition("/")
    name_match = re.match(r"(\d+)(\.\w+)$", filename)
    if not name_match:
        logger.warning("sample image filename %r doesn't look numbered", filename)
        return []
    ext = name_match.group(2)

    pages = [f"{base}/{i}{ext}" for i in range(start_index, start_index + count)]
    logger.info("derived %d sequential page URL(s) from %s", len(pages), sample_src)
    return pages


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Derive sequential page image URLs from a saved viewer-page HTML file."
    )
    parser.add_argument("html_file", help="Path to a saved viewer-page HTML file")
    parser.add_argument("base_url", help="URL the page was fetched from (for resolving relative srcs)")
    parser.add_argument("--start-index", type=int, default=0)
    parser.add_argument("-v", "--verbose", action="store_true")
    return parser.parse_args()


def main() -> None:
    args = _parse_args()
    logging.basicConfig(
        level=logging.INFO if args.verbose else logging.WARNING,
        format="%(asctime)s %(levelname)s %(message)s",
    )
    html = open(args.html_file, encoding="utf-8").read()
    pages = extract_sequential_pages(html, args.base_url, start_index=args.start_index)
    for url in pages:
        print(url)
    print(f"\n{len(pages)} page(s) found")


if __name__ == "__main__":
    main()
