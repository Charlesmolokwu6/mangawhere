"""Download extracted chapter images into a structured local directory tree.

Combines the Chrome TLS/HTTP2 impersonation from `media_client.py` with
the semaphore-capped async downloading from `async_downloader.py`, and
adds the piece specific to a multi-chapter, multi-series batch: each
image lands at `downloads/{Series}/{Chapter}/001.jpg`, so a run spanning
many chapters (or many series) produces a predictable, browsable
directory tree instead of one flat folder.

One `curl_cffi.requests.AsyncSession` and one `asyncio.Semaphore` are
shared across the *entire* batch — `concurrency` caps total in-flight
downloads project-wide, not per chapter — so queuing 50 chapters doesn't
open 50 chapters' worth of simultaneous connections.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import mimetypes
import re
from dataclasses import dataclass, field
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
DEFAULT_DOWNLOADS_ROOT = "downloads"

# Characters not safe to use in a filesystem path segment on common
# platforms (Windows is the strictest: <>:"/\|?* plus control chars).
_UNSAFE_PATH_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


def _sanitize_path_component(name: str, *, fallback: str) -> str:
    """Make `name` safe to use as a single directory/file name segment."""
    cleaned = _UNSAFE_PATH_CHARS.sub("_", name).strip().strip(".")
    return cleaned or fallback


@dataclass
class ChapterRef:
    """One chapter's extracted image URLs, to be filed under its series."""

    series: str
    chapter: str
    urls: Sequence[str]
    referer: str | None = None  # e.g. the chapter page URL itself


@dataclass
class DownloadResult:
    """Outcome of downloading a single image within a chapter."""

    series: str
    chapter: str
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
    *,
    series: str,
    chapter: str,
    index: int,
    url: str,
    chapter_dir: Path,
    referer: str | None,
    timeout: float,
    retries: int,
    name_width: int,
) -> DownloadResult:
    """Download one image, retrying on network errors, under `semaphore`."""
    async with semaphore:
        request_headers = {"Referer": referer} if referer else None
        last_error: str | None = None

        for attempt in range(1, retries + 2):  # first try + `retries` retries
            try:
                response = await session.get(url, timeout=timeout, headers=request_headers)
                response.raise_for_status()

                extension = _extension_for(url, response.headers.get("Content-Type"))
                path = chapter_dir / f"{index:0{name_width}d}{extension}"
                path.write_bytes(response.content)

                logger.info("downloaded [%s/%s] %s -> %s", series, chapter, url, path)
                return DownloadResult(series=series, chapter=chapter, url=url, ok=True, path=path)

            except RequestsError as exc:
                last_error = str(exc)
                logger.warning(
                    "[%s/%s] attempt %d/%d failed for %s: %s",
                    series,
                    chapter,
                    attempt,
                    retries + 1,
                    url,
                    exc,
                )
                if attempt <= retries:
                    await asyncio.sleep(min(2**attempt, 10))

            except OSError as exc:
                # Local filesystem failure (disk full, bad path) — not
                # something a retry against the network will fix.
                last_error = f"failed to save file: {exc}"
                logger.error("[%s/%s] could not save %s: %s", series, chapter, url, exc)
                break

        return DownloadResult(series=series, chapter=chapter, url=url, ok=False, error=last_error)


async def download_series(
    chapters: Sequence[ChapterRef],
    *,
    downloads_root: str | Path = DEFAULT_DOWNLOADS_ROOT,
    referer: str | None = None,
    user_agent: str = DEFAULT_USER_AGENT,
    impersonate: str = DEFAULT_IMPERSONATE,
    concurrency: int = DEFAULT_CONCURRENCY,
    timeout: float = DEFAULT_TIMEOUT,
    retries: int = DEFAULT_RETRIES,
    extra_headers: dict[str, str] | None = None,
) -> list[DownloadResult]:
    """Download every chapter's images into `downloads_root/{series}/{chapter}/`.

    Args:
        chapters: Chapters to download, each with its own series name,
            chapter name, image URLs, and optional per-chapter `Referer`
            (falls back to `referer` below when omitted).
        downloads_root: Root directory the `{series}/{chapter}/` tree is
            created under.
        referer: Default `Referer` header for chapters that don't specify
            their own.
        user_agent: `User-Agent` header sent with every request.
        impersonate: `curl_cffi` browser fingerprint target.
        concurrency: Maximum number of downloads in flight at once,
            across the whole batch (not per chapter).
        timeout: Per-request timeout in seconds.
        retries: Extra attempts per image after the first failure.
        extra_headers: Additional headers merged into every request.

    Returns:
        One `DownloadResult` per image, in the same series/chapter/URL
        order as `chapters`. Callers should check `result.ok` — a failed
        download does not raise.
    """
    if concurrency < 1:
        raise ValueError("concurrency must be at least 1")

    root = Path(downloads_root)
    headers = {"User-Agent": user_agent}
    if extra_headers:
        headers.update(extra_headers)

    semaphore = asyncio.Semaphore(concurrency)

    async with AsyncSession(impersonate=impersonate, headers=headers) as session:
        tasks = []
        for chapter_ref in chapters:
            series_dir_name = _sanitize_path_component(chapter_ref.series, fallback="unknown-series")
            chapter_dir_name = _sanitize_path_component(chapter_ref.chapter, fallback="unknown-chapter")
            chapter_dir = root / series_dir_name / chapter_dir_name
            chapter_dir.mkdir(parents=True, exist_ok=True)

            name_width = max(3, len(str(len(chapter_ref.urls))))
            chapter_referer = chapter_ref.referer or referer

            for index, url in enumerate(chapter_ref.urls, start=1):
                tasks.append(
                    _download_one(
                        session,
                        semaphore,
                        series=chapter_ref.series,
                        chapter=chapter_ref.chapter,
                        index=index,
                        url=url,
                        chapter_dir=chapter_dir,
                        referer=chapter_referer,
                        timeout=timeout,
                        retries=retries,
                        name_width=name_width,
                    )
                )

        results = await asyncio.gather(*tasks)

    succeeded = sum(result.ok for result in results)
    logger.info(
        "%d/%d images downloaded across %d chapter(s)", succeeded, len(results), len(chapters)
    )
    return list(results)


async def download_chapter(
    series: str, chapter: str, urls: Sequence[str], **kwargs
) -> list[DownloadResult]:
    """Convenience wrapper: download a single chapter's images."""
    return await download_series([ChapterRef(series=series, chapter=chapter, urls=urls)], **kwargs)


def _load_chapters_from_json(path: str) -> list[ChapterRef]:
    """Load a batch spec: a JSON list of {series, chapter, urls, referer?}."""
    with open(path, encoding="utf-8") as f:
        raw = json.load(f)
    return [
        ChapterRef(
            series=item["series"],
            chapter=item["chapter"],
            urls=item["urls"],
            referer=item.get("referer"),
        )
        for item in raw
    ]


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Download chapter images into downloads/{Series}/{Chapter}/NNN.ext, "
            "using a Chrome-impersonating curl_cffi session."
        )
    )
    parser.add_argument(
        "--from-json",
        default=None,
        help="Path to a JSON file: a list of {series, chapter, urls, referer?} objects",
    )
    parser.add_argument("--series", default=None, help="Series name (single-chapter mode)")
    parser.add_argument("--chapter", default=None, help="Chapter name (single-chapter mode)")
    parser.add_argument("urls", nargs="*", help="Image URLs (single-chapter mode)")
    parser.add_argument("--out-dir", default=DEFAULT_DOWNLOADS_ROOT, help="Downloads root directory")
    parser.add_argument("--referer", default=None, help="Default Referer header")
    parser.add_argument("--concurrency", type=int, default=DEFAULT_CONCURRENCY)
    parser.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT)
    parser.add_argument("--retries", type=int, default=DEFAULT_RETRIES)
    parser.add_argument("--impersonate", default=DEFAULT_IMPERSONATE)
    parser.add_argument("--user-agent", default=DEFAULT_USER_AGENT)
    parser.add_argument("-v", "--verbose", action="store_true")
    return parser.parse_args()


async def _main_async() -> None:
    args = _parse_args()
    logging.basicConfig(
        level=logging.INFO if args.verbose else logging.WARNING,
        format="%(asctime)s %(levelname)s %(message)s",
    )

    if args.from_json:
        chapters = _load_chapters_from_json(args.from_json)
    elif args.series and args.chapter and args.urls:
        chapters = [ChapterRef(series=args.series, chapter=args.chapter, urls=args.urls)]
    else:
        raise SystemExit(
            "provide --from-json <file>, or --series/--chapter plus one or more image URLs"
        )

    results = await download_series(
        chapters,
        downloads_root=args.out_dir,
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
            print(f"OK   [{result.series}/{result.chapter}] {result.path}")
        else:
            print(f"FAIL [{result.series}/{result.chapter}] {result.url}: {result.error}")

    print(f"\n{len(results) - len(failures)}/{len(results)} downloaded")
    if failures:
        raise SystemExit(1)


def main() -> None:
    asyncio.run(_main_async())


if __name__ == "__main__":
    main()
