"""Top-level pipeline: chain every scraper/ module into one series run.

    crawl_series_index()      series landing page  -> chapter URLs (oldest first)
            |
    filter already-packaged chapters out (skip_existing)
            |
    render_chapters()         chapter URLs         -> fully rendered HTML
            |
    extract_reader_images()   rendered HTML         -> image URLs per chapter
            |
    download_series()         image URLs            -> downloads/{Series}/{Chapter}/NNN.ext
            |
    package_chapter()         downloaded images      -> downloads/{Series}/{Chapter}.cbz

Each stage runs as one batched call over every pending chapter (its own
module already handles concurrency internally — a shared Playwright
browser for rendering, a shared curl_cffi session for downloading), so
the pipeline is four sequential, self-contained phases rather than a
per-chapter interleaving. A chapter that fails at any stage is recorded
in its `ChapterOutcome` and the rest of the batch continues.

This is the one module in `scraper/` that isn't standalone — its entire
job is wiring the others together, so it imports them directly. The
`sys.path` bootstrap below makes that work whether it's run as a script
(`python scraper/pipeline.py ...`) or imported as `scraper.pipeline`.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import argparse  # noqa: E402
import asyncio  # noqa: E402
import logging  # noqa: E402
from dataclasses import dataclass, field  # noqa: E402
from typing import Any  # noqa: E402
from urllib.parse import unquote, urlparse  # noqa: E402

from cbz_packager import (  # noqa: E402
    DEFAULT_DOWNLOADS_ROOT,
    is_chapter_packaged,
    package_chapter,
)
from dom_asset_extractor import extract_reader_images  # noqa: E402
from dom_renderer import render_chapters  # noqa: E402
from index_crawler import crawl_series_index  # noqa: E402
from series_downloader import ChapterRef, download_series  # noqa: E402

logger = logging.getLogger(__name__)


def _chapter_name_from_url(url: str, index: int) -> str:
    """Derive a chapter name from a URL's last path segment.

    index_crawler.crawl_series_index() returns URLs only (no labels), and
    a clean chapter title isn't reliably recoverable from every site's
    markup — but the URL itself almost always encodes the chapter (e.g.
    ".../one-piece/chapter-12"). Falls back to "chapter-N" (N = position
    in the crawled, already oldest-first list) if the URL has no usable
    trailing segment. Assumes distinct chapters produce distinct trailing
    segments, which holds for the URL shapes chapter readers actually use.
    """
    path = urlparse(url).path.rstrip("/")
    segment = unquote(path.rsplit("/", 1)[-1]).strip() if path else ""
    return segment or f"chapter-{index}"


@dataclass
class ChapterOutcome:
    """Where one chapter ended up after running the pipeline."""

    chapter: str
    url: str
    stage: str  # see _parse_args() --help for the full list of stage values
    image_count: int = 0
    downloaded_count: int = 0
    cbz_path: Path | None = None
    error: str | None = None


async def run_pipeline(
    series_name: str,
    landing_page_url: str,
    *,
    downloads_root: str | Path = DEFAULT_DOWNLOADS_ROOT,
    max_chapters: int | None = None,
    skip_existing: bool = True,
    overwrite_cbz: bool = False,
    crawler_kwargs: dict[str, Any] | None = None,
    renderer_kwargs: dict[str, Any] | None = None,
    extractor_kwargs: dict[str, Any] | None = None,
    downloader_kwargs: dict[str, Any] | None = None,
    packager_kwargs: dict[str, Any] | None = None,
) -> list[ChapterOutcome]:
    """Run the full crawl -> render -> extract -> download -> package pipeline.

    Args:
        series_name: Series name, used for the downloads/{series}/... tree.
        landing_page_url: The series' main/landing page URL.
        downloads_root: Root of the downloads/{series}/{chapter}/ tree.
        max_chapters: If given, only process the first N pending chapters
            (oldest first) — useful for a quick smoke test on a long series.
        skip_existing: If True (default), chapters that already have a
            .cbz are skipped before rendering/downloading them at all.
        overwrite_cbz: Passed to package_chapter() — force re-packaging
            even if a .cbz already exists for a chapter being processed.
        crawler_kwargs: Extra kwargs for index_crawler.crawl_series_index().
        renderer_kwargs: Extra kwargs for dom_renderer.render_chapters().
        extractor_kwargs: Extra kwargs for dom_asset_extractor.extract_reader_images().
        downloader_kwargs: Extra kwargs for series_downloader.download_series().
        packager_kwargs: Extra kwargs for cbz_packager.package_chapter().

    Returns:
        One `ChapterOutcome` per chapter that was crawled, in crawl order.
        `.stage` says where it ended up: "skipped" (already packaged),
        "render_failed", "no_images", "download_failed", "package_failed",
        or "packaged" (success).
    """
    crawler_kwargs = dict(crawler_kwargs or {})
    renderer_kwargs = dict(renderer_kwargs or {})
    extractor_kwargs = dict(extractor_kwargs or {})
    downloader_kwargs = dict(downloader_kwargs or {})
    packager_kwargs = dict(packager_kwargs or {})

    logger.info("crawling series index: %s", landing_page_url)
    chapter_urls = await crawl_series_index(landing_page_url, **crawler_kwargs)
    if max_chapters is not None:
        chapter_urls = chapter_urls[:max_chapters]
    logger.info("found %d chapter(s)", len(chapter_urls))

    chapters = [
        (_chapter_name_from_url(url, index), url) for index, url in enumerate(chapter_urls, start=1)
    ]

    outcomes: list[ChapterOutcome] = []
    pending: list[tuple[str, str]] = []

    for name, url in chapters:
        if skip_existing and is_chapter_packaged(series_name, name, downloads_root=downloads_root):
            logger.info("skipping %s/%s: already packaged", series_name, name)
            outcomes.append(ChapterOutcome(chapter=name, url=url, stage="skipped"))
        else:
            pending.append((name, url))

    if not pending:
        logger.info("nothing to do: every chapter is already packaged")
        return outcomes

    logger.info("rendering %d chapter(s)", len(pending))
    render_results = await render_chapters([url for _, url in pending], **renderer_kwargs)
    render_by_url = {result.url: result for result in render_results}

    to_download: list[ChapterRef] = []
    outcome_by_chapter: dict[str, ChapterOutcome] = {}

    for name, url in pending:
        render_result = render_by_url[url]

        if not render_result.ok:
            logger.warning("render failed for %s/%s: %s", series_name, name, render_result.error)
            outcome = ChapterOutcome(chapter=name, url=url, stage="render_failed", error=render_result.error)
            outcomes.append(outcome)
            continue

        image_urls = extract_reader_images(render_result.html, base_url=url, **extractor_kwargs)
        if not image_urls:
            logger.warning("no images extracted for %s/%s", series_name, name)
            outcome = ChapterOutcome(chapter=name, url=url, stage="no_images")
            outcomes.append(outcome)
            continue

        outcome = ChapterOutcome(chapter=name, url=url, stage="extracted", image_count=len(image_urls))
        outcomes.append(outcome)
        outcome_by_chapter[name] = outcome
        to_download.append(ChapterRef(series=series_name, chapter=name, urls=image_urls, referer=url))

    if not to_download:
        logger.warning("no chapters had extractable images; nothing to download")
        return outcomes

    logger.info("downloading images for %d chapter(s)", len(to_download))
    download_results = await download_series(to_download, downloads_root=downloads_root, **downloader_kwargs)

    downloaded_count_by_chapter: dict[str, int] = {}
    for result in download_results:
        if result.ok:
            downloaded_count_by_chapter[result.chapter] = downloaded_count_by_chapter.get(result.chapter, 0) + 1

    for chapter_ref in to_download:
        outcome = outcome_by_chapter[chapter_ref.chapter]
        outcome.downloaded_count = downloaded_count_by_chapter.get(chapter_ref.chapter, 0)

        if outcome.downloaded_count == 0:
            outcome.stage = "download_failed"
            logger.warning("every image failed to download for %s/%s", series_name, chapter_ref.chapter)
            continue

        package_result = package_chapter(
            series_name,
            chapter_ref.chapter,
            downloads_root=downloads_root,
            overwrite=overwrite_cbz,
            **packager_kwargs,
        )
        if package_result.ok:
            outcome.stage = "packaged"
            outcome.cbz_path = package_result.cbz_path
        else:
            outcome.stage = "package_failed"
            outcome.error = package_result.error

    succeeded = sum(o.stage == "packaged" for o in outcomes)
    logger.info("pipeline complete: %d/%d chapter(s) packaged", succeeded, len(outcomes))
    return outcomes


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Crawl a series landing page, render+extract each chapter's images, "
            "download them, and package each chapter into a .cbz. Stages: "
            "skipped (already packaged) -> render_failed -> no_images -> "
            "extracted -> download_failed -> package_failed -> packaged."
        )
    )
    parser.add_argument("series", help="Series name (used for the downloads/{series}/ tree)")
    parser.add_argument("landing_page_url", help="Series landing page URL")
    parser.add_argument("--downloads-root", default=DEFAULT_DOWNLOADS_ROOT)
    parser.add_argument("--max-chapters", type=int, default=None, help="Process only the first N pending chapters")
    parser.add_argument("--no-skip-existing", action="store_true", help="Re-process chapters that already have a .cbz")
    parser.add_argument("--overwrite-cbz", action="store_true", help="Re-package even if a .cbz already exists")
    parser.add_argument("--concurrency", type=int, default=None, help="Shared render/download concurrency cap")
    parser.add_argument("--headed", action="store_true", help="Run Playwright with a visible browser window")
    parser.add_argument("--executable-path", default=None, help="Path to a specific Chromium/Chrome binary")
    parser.add_argument("-v", "--verbose", action="store_true")
    return parser.parse_args()


async def _main_async() -> None:
    args = _parse_args()
    logging.basicConfig(
        level=logging.INFO if args.verbose else logging.WARNING,
        format="%(asctime)s %(levelname)s %(message)s",
    )

    # crawl_series_index() and render_chapters() each launch their own
    # Playwright browser, so both need the same --headed/--executable-path.
    crawler_kwargs: dict[str, Any] = {"headless": not args.headed, "executable_path": args.executable_path}
    renderer_kwargs: dict[str, Any] = {"headless": not args.headed, "executable_path": args.executable_path}
    downloader_kwargs: dict[str, Any] = {}
    if args.concurrency is not None:
        renderer_kwargs["concurrency"] = args.concurrency
        downloader_kwargs["concurrency"] = args.concurrency

    outcomes = await run_pipeline(
        args.series,
        args.landing_page_url,
        downloads_root=args.downloads_root,
        max_chapters=args.max_chapters,
        skip_existing=not args.no_skip_existing,
        overwrite_cbz=args.overwrite_cbz,
        crawler_kwargs=crawler_kwargs,
        renderer_kwargs=renderer_kwargs,
        downloader_kwargs=downloader_kwargs,
    )

    for outcome in outcomes:
        detail = outcome.error or (str(outcome.cbz_path) if outcome.cbz_path else "")
        print(f"{outcome.stage:16} {outcome.chapter:20} {detail}")

    counts: dict[str, int] = {}
    for outcome in outcomes:
        counts[outcome.stage] = counts.get(outcome.stage, 0) + 1
    summary = ", ".join(f"{count} {stage}" for stage, count in sorted(counts.items()))
    print(f"\n{len(outcomes)} chapter(s): {summary}")

    if any(o.stage not in ("packaged", "skipped") for o in outcomes):
        raise SystemExit(1)


def main() -> None:
    asyncio.run(_main_async())


if __name__ == "__main__":
    main()
