"""Extract manga page image URLs from an HTML response using BeautifulSoup.

Many manga-reader pages lazy-load their page images: the real image URL
lives in a `data-src` / `data-lazy-src` attribute (or similar) while `src`
holds a tiny placeholder (or is empty) until JavaScript swaps it in. This
module locates the `<img>` tags inside a designated "reader" container and
picks the first usable URL for each one, falling back through the common
lazy-loading attributes down to `src`.
"""

from __future__ import annotations

from typing import Iterable
from urllib.parse import urljoin

from bs4 import BeautifulSoup

# Attributes checked for each <img>, in priority order. Sites vary in which
# attribute holds the "real" URL while an image is lazy-loaded, so we try
# the lazy-loading attributes first and only fall back to `src` last.
LAZY_LOAD_ATTRS: tuple[str, ...] = (
    "data-src",
    "data-lazy-src",
    "data-original",
    "data-url",
    "src",
)

# Placeholder markers that show up in `src` while the real image hasn't
# loaded yet (base64 blur-ups, 1x1 gifs, "loading.svg", etc.). Any URL
# containing one of these is skipped rather than treated as a real page.
_PLACEHOLDER_MARKERS = ("data:image", "placeholder", "loading.gif", "blank.gif")


def extract_image_urls(
    html: str,
    container_class: str,
    *,
    base_url: str | None = None,
    attrs: Iterable[str] = LAZY_LOAD_ATTRS,
) -> list[str]:
    """Extract a clean, ordered, de-duplicated list of image URLs.

    Args:
        html: Raw HTML of the page (e.g. `response.text` from `requests`).
        container_class: CSS class of the element that wraps the manga
            page images (e.g. "reader-container", "page-list").
        base_url: If given, relative URLs (e.g. "/uploads/1.jpg") are
            resolved against this URL into absolute ones.
        attrs: Attribute names to check on each `<img>`, in priority
            order. Defaults to the common lazy-loading attributes.

    Returns:
        A list of image URLs in document order, with duplicates removed
        and empty/placeholder values discarded. Empty list if the
        container or no valid images are found.
    """
    soup = BeautifulSoup(html, "html.parser")

    container = soup.find(class_=container_class)
    if container is None:
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

    return urls


def _resolve_image_url(img, attrs: Iterable[str]) -> str | None:
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


def _is_placeholder(url: str) -> bool:
    lowered = url.lower()
    return any(marker in lowered for marker in _PLACEHOLDER_MARKERS)


if __name__ == "__main__":
    sample_html = """
    <html>
      <body>
        <div class="reader-container">
          <img src="data:image/gif;base64,R0lGOD..." data-src="/uploads/manga/001.jpg" />
          <img data-lazy-src="/uploads/manga/002.jpg" src="/img/placeholder.svg" />
          <img src="https://cdn.example.com/uploads/manga/003.jpg" />
          <img data-src="" src="" />
        </div>
      </body>
    </html>
    """

    for url in extract_image_urls(
        sample_html, "reader-container", base_url="https://example.com"
    ):
        print(url)
