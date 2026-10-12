"""Memory-heavy work takes turns.

The server has 512MB. A headless Chromium (a scraper's fallback when a site
blocks plain requests) takes ~200MB, and the OCR engine (storyteller pages,
pictures in YouTube posts) ~140MB. Several browsers started together, or a
browser next to OCR, got the server killed for running out of memory —
mid-request, so the app was left waiting on "Looking for a chapter list…".

Everything heavy runs inside SLOT, one at a time by default (HEAVY_SLOTS
raises it on a bigger server), and a browser unloads the OCR engine before
it starts.
"""
import asyncio
import ctypes
import gc
import os

SLOT = asyncio.Semaphore(max(1, int(os.environ.get("HEAVY_SLOTS", "1"))))


def release_ocr() -> None:
    """Unload the OCR engine; the next OCR call loads it again (~1s)."""
    from server import storyteller

    if storyteller._ocr_engine is None:
        return
    storyteller._ocr_engine = None
    gc.collect()
    try:
        ctypes.CDLL("libc.so.6").malloc_trim(0)   # hand the freed memory back
    except Exception:
        pass
