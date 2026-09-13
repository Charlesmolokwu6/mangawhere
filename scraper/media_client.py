"""HTTP client session for fetching media assets (e.g. manga page images).

Some source/CDN hosts serve images differently — or block the request
outright — when the request doesn't look like it came from a real browser
(TLS/HTTP fingerprint, missing `Referer`, generic `User-Agent`, etc.).
`curl_cffi` builds requests using the same TLS/HTTP2 fingerprint as a real
Chrome build ("impersonation"), which combined with realistic browser
headers lets a script fetch publicly served assets the way a browser tab
would.

Only use this against sites you're authorized to access (your own
infrastructure, or a source's public assets you have permission/rights to
fetch) and respect their `robots.txt` and terms of service.
"""

from __future__ import annotations

import mimetypes
from pathlib import Path
from typing import Iterable

from curl_cffi import requests as cffi_requests
from curl_cffi.requests import Response, Session

# curl_cffi target for the browser/TLS fingerprint to impersonate. See
# `curl_cffi.requests.Session` docs for the full list of supported targets
# (other "chromeNNN", "safari...", "firefox..." strings, etc.).
DEFAULT_IMPERSONATE = "chrome124"

DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)

# Headers a real browser sends when requesting an <img> resource. Sites
# that gate on User-Agent/Referer/Sec-Fetch-* alone (rather than deeper
# fingerprinting) are satisfied by this set.
_BASE_MEDIA_HEADERS = {
    "Accept": "image/avif,image/webp,image/apng,image/svg+xml,image/*,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Accept-Encoding": "gzip, deflate, br",
    "Sec-Fetch-Dest": "image",
    "Sec-Fetch-Mode": "no-cors",
    "Sec-Fetch-Site": "cross-site",
    "Connection": "keep-alive",
}


def build_session(
    referer: str,
    *,
    user_agent: str = DEFAULT_USER_AGENT,
    impersonate: str = DEFAULT_IMPERSONATE,
    extra_headers: dict[str, str] | None = None,
) -> Session:
    """Create a `curl_cffi` session configured to look like a Chrome tab.

    Args:
        referer: Value for the `Referer` header — many media hosts reject
            or downgrade requests that don't carry the referring page's URL.
        user_agent: Browser `User-Agent` string to send.
        impersonate: `curl_cffi` fingerprint target (TLS/HTTP2 handshake
            shape). Should generally match the browser implied by
            `user_agent`.
        extra_headers: Any additional headers to merge in, overriding the
            defaults if the keys collide.

    Returns:
        A configured, reusable `curl_cffi.requests.Session`.
    """
    session = cffi_requests.Session(impersonate=impersonate)
    session.headers.update(_BASE_MEDIA_HEADERS)
    session.headers.update({"User-Agent": user_agent, "Referer": referer})
    if extra_headers:
        session.headers.update(extra_headers)
    return session


def fetch_media(session: Session, url: str, *, timeout: float = 15.0) -> Response:
    """GET a single media URL through `session` and return the response.

    Raises `curl_cffi.requests.errors.RequestsError` (network issues) or
    an `HTTPError` (via `raise_for_status`) on failure.
    """
    response = session.get(url, timeout=timeout)
    response.raise_for_status()
    return response


def download_media(
    session: Session,
    urls: Iterable[str],
    out_dir: str | Path,
    *,
    timeout: float = 15.0,
) -> list[Path]:
    """Fetch each URL in `urls` and save it under `out_dir`.

    Files are named by sequence order (`001`, `002`, ...) with an
    extension guessed from the response's `Content-Type`, falling back to
    the URL's own suffix.

    Returns the list of saved file paths, in the same order as `urls`.
    """
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    saved: list[Path] = []
    for index, url in enumerate(urls, start=1):
        response = fetch_media(session, url, timeout=timeout)
        extension = _guess_extension(response, url)
        path = out_dir / f"{index:03d}{extension}"
        path.write_bytes(response.content)
        saved.append(path)

    return saved


def _guess_extension(response: Response, url: str) -> str:
    content_type = response.headers.get("Content-Type", "").split(";")[0].strip()
    if content_type:
        guessed = mimetypes.guess_extension(content_type)
        if guessed:
            return guessed

    suffix = Path(url.split("?", 1)[0]).suffix
    return suffix if suffix else ".bin"


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(
        description="Fetch media URLs through a Chrome-impersonating session."
    )
    parser.add_argument("urls", nargs="+", help="Media (image) URLs to fetch")
    parser.add_argument(
        "--referer", required=True, help="Referer URL to send (the page that links the media)"
    )
    parser.add_argument(
        "--out-dir", default="./downloads", help="Directory to save fetched files into"
    )
    parser.add_argument(
        "--impersonate", default=DEFAULT_IMPERSONATE, help="curl_cffi impersonation target"
    )
    parser.add_argument("--user-agent", default=DEFAULT_USER_AGENT, help="User-Agent header")
    args = parser.parse_args()

    http_session = build_session(
        referer=args.referer, user_agent=args.user_agent, impersonate=args.impersonate
    )
    saved_paths = download_media(http_session, args.urls, args.out_dir)

    for saved_path in saved_paths:
        print(f"saved {saved_path}")
