"""Asynchronous, concurrency-limited file downloader built on curl_cffi.

Downloads a list of URLs into a target folder, naming files sequentially
with zero-padded numbers (001.jpg, 002.jpg, ...). Concurrency is capped
with an `asyncio.Semaphore` so a large URL list doesn't open hundreds of
simultaneous connections. Each download is isolated: a network failure on
one URL is retried a bounded number of times, then recorded as a failed
`DownloadResult` without cancelling the rest of the batch.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import mimetypes
from dataclasses import dataclass
from pathlib import Path
from typing import Sequence

from curl_cffi.requests import AsyncSession, RequestsError

logger = logging.getLogger(__name__)

DEFAULT_IMPERSONATE = "chrome124"
DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)
DEFAULT_CONCURRENCY = 5
DEFAULT_TIMEOUT = 15.0
DEFAULT_RETRIES = 2


@dataclass
class DownloadResult:
    """Outcome of downloading a single URL."""

    url: str
    ok: bool
    path: Path | None = None
    error: str | None = None


def _extension_for(url: str, content_type: str | None) -> str:
    """Pick a file extension from the response Content-Type, else the URL."""
    if content_type:
        guessed = mimetypes.guess_extension(content_type.split(";")[0].strip())
        if guessed:
            return guessed

    suffix = Path(url.split("?", 1)[0]).suffix
    return suffix if suffix else ".jpg"


async def _download_one(
    session: AsyncSession,
    semaphore: asyncio.Semaphore,
    index: int,
    url: str,
    out_dir: Path,
    *,
    timeout: float,
    retries: int,
    name_width: int,
) -> DownloadResult:
    """Download a single URL, retrying on network errors, under `semaphore`."""
    async with semaphore:
        last_error: str | None = None

        for attempt in range(1, retries + 2):  # first try + `retries` retries
            try:
                response = await session.get(url, timeout=timeout)
                response.raise_for_status()

                extension = _extension_for(url, response.headers.get("Content-Type"))
                path = out_dir / f"{index:0{name_width}d}{extension}"
                path.write_bytes(response.content)

                logger.info("downloaded %s -> %s", url, path)
                return DownloadResult(url=url, ok=True, path=path)

            except RequestsError as exc:
                # Network/timeout/DNS/HTTP-status failures — worth retrying.
                last_error = str(exc)
                logger.warning(
                    "attempt %d/%d failed for %s: %s", attempt, retries + 1, url, exc
                )
                if attempt <= retries:
                    await asyncio.sleep(min(2**attempt, 10))

            except OSError as exc:
                # Local filesystem failure (disk full, bad path) — not
                # something a retry against the network will fix.
                last_error = f"failed to save file: {exc}"
                logger.error("could not save %s: %s", url, exc)
                break

        return DownloadResult(url=url, ok=False, error=last_error)


async def download_all(
    urls: Sequence[str],
    target_folder: str | Path,
    *,
    referer: str | None = None,
    user_agent: str = DEFAULT_USER_AGENT,
    impersonate: str = DEFAULT_IMPERSONATE,
    concurrency: int = DEFAULT_CONCURRENCY,
    timeout: float = DEFAULT_TIMEOUT,
    retries: int = DEFAULT_RETRIES,
    extra_headers: dict[str, str] | None = None,
) -> list[DownloadResult]:
    """Download every URL in `urls` into `target_folder` concurrently.

    Args:
        urls: URLs to download, in the order they should be numbered.
        target_folder: Directory to save files into (created if missing).
        referer: Optional `Referer` header value to send with every request.
        user_agent: `User-Agent` header value.
        impersonate: `curl_cffi` browser fingerprint target.
        concurrency: Maximum number of downloads in flight at once.
        timeout: Per-request timeout in seconds.
        retries: Extra attempts per URL after the first failure.
        extra_headers: Additional headers merged into every request.

    Returns:
        One `DownloadResult` per URL, in the same order as `urls`. Callers
        should check `result.ok` — a failed download does not raise.
    """
    if concurrency < 1:
        raise ValueError("concurrency must be at least 1")

    out_dir = Path(target_folder)
    out_dir.mkdir(parents=True, exist_ok=True)

    headers = {"User-Agent": user_agent}
    if referer:
        headers["Referer"] = referer
    if extra_headers:
        headers.update(extra_headers)

    semaphore = asyncio.Semaphore(concurrency)
    # Zero-pad wide enough for the whole batch, minimum 3 digits (001, 002, ...).
    name_width = max(3, len(str(len(urls))))

    async with AsyncSession(impersonate=impersonate, headers=headers) as session:
        tasks = [
            _download_one(
                session,
                semaphore,
                index,
                url,
                out_dir,
                timeout=timeout,
                retries=retries,
                name_width=name_width,
            )
            for index, url in enumerate(urls, start=1)
        ]
        results = await asyncio.gather(*tasks)

    succeeded = sum(result.ok for result in results)
    logger.info("%d/%d downloads succeeded", succeeded, len(results))
    return list(results)


def run(urls: Sequence[str], target_folder: str | Path, **kwargs) -> list[DownloadResult]:
    """Synchronous convenience wrapper around `download_all()`."""
    return asyncio.run(download_all(urls, target_folder, **kwargs))


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Download a list of URLs concurrently, named 001.jpg, 002.jpg, ..."
    )
    parser.add_argument("urls", nargs="+", help="URLs to download, in order")
    parser.add_argument("--out-dir", required=True, help="Target folder for downloaded files")
    parser.add_argument("--referer", default=None, help="Referer header to send")
    parser.add_argument("--user-agent", default=DEFAULT_USER_AGENT, help="User-Agent header")
    parser.add_argument(
        "--impersonate", default=DEFAULT_IMPERSONATE, help="curl_cffi impersonation target"
    )
    parser.add_argument(
        "--concurrency", type=int, default=DEFAULT_CONCURRENCY, help="Max simultaneous downloads"
    )
    parser.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT, help="Per-request timeout (s)")
    parser.add_argument(
        "--retries", type=int, default=DEFAULT_RETRIES, help="Retries per URL after first failure"
    )
    parser.add_argument("-v", "--verbose", action="store_true", help="Enable info-level logging")
    return parser.parse_args()


def main() -> None:
    args = _parse_args()
    logging.basicConfig(
        level=logging.INFO if args.verbose else logging.WARNING,
        format="%(asctime)s %(levelname)s %(message)s",
    )

    results = run(
        args.urls,
        args.out_dir,
        referer=args.referer,
        user_agent=args.user_agent,
        impersonate=args.impersonate,
        concurrency=args.concurrency,
        timeout=args.timeout,
        retries=args.retries,
    )

    failures = [r for r in results if not r.ok]
    for result in results:
        if result.ok:
            print(f"OK   {result.path}")
        else:
            print(f"FAIL {result.url}: {result.error}")

    print(f"\n{len(results) - len(failures)}/{len(results)} downloaded")
    if failures:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
