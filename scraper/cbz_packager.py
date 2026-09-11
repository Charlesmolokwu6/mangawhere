"""Package downloaded chapter image folders into .cbz comic archives.

A .cbz file is just a zip archive of a chapter's page images, readable by
any comic reader. This module scans a chapter's image folder — e.g. the
`downloads/{Series}/{Chapter}/` directory `series_downloader.py` produces
— sorts the files numerically (not lexicographically, so `2.jpg` sorts
before `10.jpg` regardless of zero-padding), and zips them into
`downloads/{Series}/{Chapter}.cbz` using the standard library's `zipfile`
module (`ZIP_STORED`: page images are already compressed formats, so
re-compressing them just costs CPU for no size benefit).

Packaging a chapter whose `.cbz` already exists is a no-op by default
(`overwrite=False`) — the archive isn't rewritten, and `is_chapter_packaged()`
/ `filter_pending_chapters()` expose that same check so a caller can skip
*downloading* a chapter's images in the first place once it's already
packaged. This module doesn't import `series_downloader.ChapterRef`
directly (kept standalone like its sibling modules); glue code composing
them is a one-liner:

    pending = filter_pending_chapters(
        [(c.series, c.chapter) for c in chapters], downloads_root=root
    )
"""

from __future__ import annotations

import argparse
import logging
import re
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Sequence

logger = logging.getLogger(__name__)

DEFAULT_DOWNLOADS_ROOT = "downloads"
DEFAULT_IMAGE_EXTENSIONS = (".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp")

# Same sanitization convention as series_downloader.py, so a chapter's
# .cbz path and its downloaded-images directory always agree.
_UNSAFE_PATH_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
_DIGIT_RUN = re.compile(r"(\d+)")


def _sanitize_path_component(name: str, *, fallback: str) -> str:
    cleaned = _UNSAFE_PATH_CHARS.sub("_", name).strip().strip(".")
    return cleaned or fallback


def _natural_sort_key(name: str) -> list[int | str]:
    """Sort key ordering embedded numbers numerically: '2.jpg' < '10.jpg'."""
    parts = _DIGIT_RUN.split(name)
    return [int(part) if part.isdigit() else part.lower() for part in parts]


@dataclass
class PackageResult:
    """Outcome of packaging a single chapter."""

    series: str
    chapter: str
    cbz_path: Path
    ok: bool
    image_count: int = 0
    skipped: bool = False
    error: str | None = None


def series_dir_for(series: str, *, downloads_root: str | Path = DEFAULT_DOWNLOADS_ROOT) -> Path:
    return Path(downloads_root) / _sanitize_path_component(series, fallback="unknown-series")


def chapter_dir_for(
    series: str, chapter: str, *, downloads_root: str | Path = DEFAULT_DOWNLOADS_ROOT
) -> Path:
    """The directory holding a chapter's raw downloaded images."""
    return series_dir_for(series, downloads_root=downloads_root) / _sanitize_path_component(
        chapter, fallback="unknown-chapter"
    )


def cbz_path_for(
    series: str, chapter: str, *, downloads_root: str | Path = DEFAULT_DOWNLOADS_ROOT
) -> Path:
    """The canonical .cbz path for a series/chapter."""
    chapter_name = _sanitize_path_component(chapter, fallback="unknown-chapter")
    return series_dir_for(series, downloads_root=downloads_root) / f"{chapter_name}.cbz"


def is_chapter_packaged(
    series: str, chapter: str, *, downloads_root: str | Path = DEFAULT_DOWNLOADS_ROOT
) -> bool:
    """True if this chapter's .cbz already exists on disk (and isn't empty)."""
    cbz_path = cbz_path_for(series, chapter, downloads_root=downloads_root)
    return cbz_path.is_file() and cbz_path.stat().st_size > 0


def filter_pending_chapters(
    chapters: Sequence[tuple[str, str]],
    *,
    downloads_root: str | Path = DEFAULT_DOWNLOADS_ROOT,
) -> list[tuple[str, str]]:
    """Return only the `(series, chapter)` pairs that don't have a .cbz yet.

    Meant to run before downloading: filter a chapter list down to what
    actually still needs fetching, so a repeat run doesn't re-download
    (and re-package) chapters already finished.
    """
    pending = []
    for series, chapter in chapters:
        if is_chapter_packaged(series, chapter, downloads_root=downloads_root):
            logger.info("skipping %s/%s: .cbz already exists", series, chapter)
            continue
        pending.append((series, chapter))
    return pending


def package_chapter(
    series: str,
    chapter: str,
    *,
    downloads_root: str | Path = DEFAULT_DOWNLOADS_ROOT,
    chapter_dir: str | Path | None = None,
    image_extensions: Sequence[str] = DEFAULT_IMAGE_EXTENSIONS,
    overwrite: bool = False,
    delete_source_images: bool = False,
) -> PackageResult:
    """Zip one chapter's image folder into a .cbz.

    Args:
        series: Series name (used to compute the default paths below).
        chapter: Chapter name.
        downloads_root: Root of the `{series}/{chapter}/` tree.
        chapter_dir: Explicit source directory, overriding the default
            `downloads_root/{series}/{chapter}/` location.
        image_extensions: File extensions treated as page images.
        overwrite: If False (default) and the .cbz already exists, this is
            a no-op — returns `ok=True, skipped=True` without touching it.
        delete_source_images: If True, remove the source image files (and
            the now-empty chapter directory) after a successful package.

    Returns:
        A `PackageResult` — check `.ok`; packaging one chapter failing
        doesn't raise, so a caller looping over many chapters can continue.
    """
    cbz_path = cbz_path_for(series, chapter, downloads_root=downloads_root)

    if cbz_path.exists() and not overwrite:
        logger.info("skipping %s/%s: %s already exists", series, chapter, cbz_path)
        return PackageResult(series=series, chapter=chapter, cbz_path=cbz_path, ok=True, skipped=True)

    source_dir = Path(chapter_dir) if chapter_dir is not None else chapter_dir_for(
        series, chapter, downloads_root=downloads_root
    )

    if not source_dir.is_dir():
        error = f"chapter directory not found: {source_dir}"
        logger.error(error)
        return PackageResult(series=series, chapter=chapter, cbz_path=cbz_path, ok=False, error=error)

    images = sorted(
        (p for p in source_dir.iterdir() if p.is_file() and p.suffix.lower() in image_extensions),
        key=lambda p: _natural_sort_key(p.name),
    )
    if not images:
        error = f"no images found in {source_dir}"
        logger.warning(error)
        return PackageResult(series=series, chapter=chapter, cbz_path=cbz_path, ok=False, error=error)

    cbz_path.parent.mkdir(parents=True, exist_ok=True)
    # Write to a temp file and rename into place, so a failure or
    # interruption mid-write never leaves a corrupt/partial .cbz sitting
    # at the path is_chapter_packaged() checks.
    tmp_path = cbz_path.with_suffix(cbz_path.suffix + ".tmp")
    try:
        with zipfile.ZipFile(tmp_path, "w", zipfile.ZIP_STORED) as archive:
            for image_path in images:
                archive.write(image_path, arcname=image_path.name)
        tmp_path.replace(cbz_path)
    except OSError as exc:
        tmp_path.unlink(missing_ok=True)
        error = f"failed to write {cbz_path}: {exc}"
        logger.error(error)
        return PackageResult(series=series, chapter=chapter, cbz_path=cbz_path, ok=False, error=error)

    logger.info("packaged %s/%s -> %s (%d images)", series, chapter, cbz_path, len(images))

    if delete_source_images:
        for image_path in images:
            image_path.unlink()
        try:
            source_dir.rmdir()  # only succeeds if nothing else is left in it
        except OSError:
            pass

    return PackageResult(
        series=series, chapter=chapter, cbz_path=cbz_path, ok=True, image_count=len(images)
    )


def package_series(series: str, chapters: Sequence[str], **kwargs) -> list[PackageResult]:
    """Package every named chapter of one series."""
    return [package_chapter(series, chapter, **kwargs) for chapter in chapters]


def scan_and_package_all(
    downloads_root: str | Path = DEFAULT_DOWNLOADS_ROOT, **kwargs
) -> list[PackageResult]:
    """Scan `downloads_root/{series}/{chapter}/` and package every chapter
    that doesn't already have a .cbz (or all of them, with `overwrite=True`).
    """
    root = Path(downloads_root)
    if not root.is_dir():
        logger.warning("downloads root not found: %s", root)
        return []

    results: list[PackageResult] = []
    for series_dir in sorted((p for p in root.iterdir() if p.is_dir()), key=lambda p: p.name.lower()):
        chapter_dirs = sorted(
            (p for p in series_dir.iterdir() if p.is_dir()), key=lambda p: _natural_sort_key(p.name)
        )
        for chapter_dir in chapter_dirs:
            results.append(
                package_chapter(series_dir.name, chapter_dir.name, downloads_root=downloads_root, **kwargs)
            )
    return results


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Package downloaded chapter image folders into .cbz archives."
    )
    parser.add_argument("--downloads-root", default=DEFAULT_DOWNLOADS_ROOT)
    parser.add_argument("--series", default=None, help="Package only this series")
    parser.add_argument("--chapter", default=None, help="Package only this chapter (requires --series)")
    parser.add_argument("--overwrite", action="store_true", help="Re-package even if a .cbz already exists")
    parser.add_argument(
        "--delete-source-images",
        action="store_true",
        help="Remove the source image files after a successful package",
    )
    parser.add_argument("-v", "--verbose", action="store_true")
    return parser.parse_args()


def main() -> None:
    args = _parse_args()
    logging.basicConfig(
        level=logging.INFO if args.verbose else logging.WARNING,
        format="%(asctime)s %(levelname)s %(message)s",
    )

    kwargs = dict(overwrite=args.overwrite, delete_source_images=args.delete_source_images)

    if args.chapter and not args.series:
        raise SystemExit("--chapter requires --series")

    if args.series and args.chapter:
        results = [package_chapter(args.series, args.chapter, downloads_root=args.downloads_root, **kwargs)]
    elif args.series:
        series_dir = series_dir_for(args.series, downloads_root=args.downloads_root)
        chapter_names = sorted(
            (p.name for p in series_dir.iterdir() if p.is_dir()) if series_dir.is_dir() else [],
            key=_natural_sort_key,
        )
        results = package_series(args.series, chapter_names, downloads_root=args.downloads_root, **kwargs)
    else:
        results = scan_and_package_all(downloads_root=args.downloads_root, **kwargs)

    failures = [r for r in results if not r.ok]
    skipped = [r for r in results if r.skipped]
    packaged = [r for r in results if r.ok and not r.skipped]

    for result in results:
        if result.skipped:
            print(f"SKIP [{result.series}/{result.chapter}] {result.cbz_path} already exists")
        elif result.ok:
            print(f"OK   [{result.series}/{result.chapter}] {result.cbz_path} ({result.image_count} images)")
        else:
            print(f"FAIL [{result.series}/{result.chapter}] {result.error}")

    print(f"\n{len(packaged)} packaged, {len(skipped)} skipped, {len(failures)} failed")
    if failures:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
