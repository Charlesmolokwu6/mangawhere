"""Server-side fallback for the "paste a video link" search flow.

YouTube's oEmbed endpoint answers with 401 when the uploader has disabled
embedding, and even when it succeeds it only ever hands back the video's
*title* — never its description. Neither restriction touches yt-dlp: an
embed-disabled video is still fully public, so yt-dlp can read its title,
description and download its audio track exactly as it would for any other
video. That gives two extra signals a bare caption doesn't: the upload
description (where a poster's "Sauce: ..." link usually lives) and, for
edits that only *say* the manga's name out loud, a speech-to-text
transcript of the audio itself.

Both are best-effort. If yt-dlp or the transcription step fails for any
reason (rate limiting, an unsupported site quirk, a corrupt download), this
returns whatever it already has rather than raising — the caller falls
back to the manual-paste box either way.
"""
import asyncio
import json
import os
import re
import tempfile
from pathlib import Path
from typing import Optional

import httpx
import yt_dlp

# Shorts and clips are the target here — a full episode's audio would take
# far too long to transcribe on a free-tier CPU box, and the manga name (if
# spoken at all) is said in the first few seconds either way.
MAX_TRANSCRIBE_SECONDS = 180

# "tiny.en" is the smallest Whisper checkpoint (~75MB) — accuracy is rougher
# than the bigger models, but exCh()/exTitle() on the client only need a
# recognisable title fragment, not a perfect transcript.
WHISPER_MODEL_SIZE = "tiny.en"

_whisper_model = None

# A Netscape-format cookies.txt from a real, signed-in YouTube session
# (README.md explains how to export one). YouTube's "Sign in to confirm
# you're not a bot" challenge is checking for exactly this — a request that
# carries a real account's session looks like a browser instead of a bare
# script, so it mostly avoids the block that a cookie-less request hits.
# Entirely optional: everything here still runs without it, just less
# reliably. Cached to a temp file once per process rather than re-written
# on every lookup, since the env var can't change without a redeploy anyway.
_cookie_file_path: Optional[str] = None
_cookie_file_loaded = False


def _cookies_path() -> Optional[str]:
    global _cookie_file_path, _cookie_file_loaded
    if _cookie_file_loaded:
        return _cookie_file_path
    _cookie_file_loaded = True
    raw = os.environ.get("YOUTUBE_COOKIES", "").strip()
    if raw:
        fd, path = tempfile.mkstemp(prefix="ytcookies-", suffix=".txt")
        os.chmod(path, 0o600)
        with os.fdopen(fd, "w") as f:
            f.write(raw + "\n")
        _cookie_file_path = path
    return _cookie_file_path


def _get_whisper_model():
    # Imported lazily, and the model loaded lazily within that: importing
    # faster_whisper (and ctranslate2 underneath it) at process startup, or
    # downloading the model before it's ever needed, would slow down every
    # boot for a feature most requests never touch.
    global _whisper_model
    if _whisper_model is None:
        from faster_whisper import WhisperModel

        _whisper_model = WhisperModel(WHISPER_MODEL_SIZE, device="cpu", compute_type="int8")
    return _whisper_model


def _extract_metadata(url: str) -> dict:
    opts = {
        "quiet": True,
        "no_warnings": True,
        "skip_download": True,
        "noplaylist": True,
    }
    cookies = _cookies_path()
    if cookies:
        opts["cookiefile"] = cookies
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=False)
    return {
        "title": info.get("title") or "",
        "description": info.get("description") or "",
        "duration": info.get("duration") or 0,
    }


def _download_audio(url: str, dest_dir: str) -> Optional[str]:
    # bestaudio-only, no postprocessing: that's the DASH audio stream
    # YouTube/TikTok already serve separately from video, so this needs no
    # ffmpeg merge/re-encode step — just the raw file, which faster-whisper
    # (via PyAV) can decode directly.
    opts = {
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        "format": "bestaudio/best",
        "outtmpl": str(Path(dest_dir) / "audio.%(ext)s"),
    }
    cookies = _cookies_path()
    if cookies:
        opts["cookiefile"] = cookies
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=True)
        path = ydl.prepare_filename(info)
        return path if Path(path).exists() else None


def _transcribe(path: str) -> str:
    model = _get_whisper_model()
    segments, _ = model.transcribe(path, beam_size=1, vad_filter=True)
    return " ".join(seg.text.strip() for seg in segments).strip()


async def fetch_metadata(url: str) -> dict:
    # YouTube's bot-check ("Sign in to confirm you're not a bot") shows up
    # intermittently rather than consistently -- the same URL can fail and
    # then succeed moments later with no other change -- so one retry
    # recovers a real share of these instead of surfacing a transient block
    # as a hard failure.
    try:
        return await asyncio.to_thread(_extract_metadata, url)
    except Exception:
        await asyncio.sleep(1.5)
        return await asyncio.to_thread(_extract_metadata, url)


async def transcribe_audio(url: str, duration_seconds: int) -> Optional[str]:
    if duration_seconds and duration_seconds > MAX_TRANSCRIBE_SECONDS:
        return None

    def job() -> Optional[str]:
        with tempfile.TemporaryDirectory() as tmp:
            path = _download_audio(url, tmp)
            if not path:
                return None
            return _transcribe(path)

    try:
        return await asyncio.to_thread(job)
    except Exception as e:
        print(f"[video] audio transcription failed for {url}: {e}")
        return None


# YouTube refuses yt-dlp from server addresses ("Sign in to confirm you're
# not a bot") but still serves the video's own page, whose player data has
# the title and the full description, where uploaders write
# "Manhwa Name: ...". Only ever fetched from www.youtube.com, by video id.
_YT_ID = re.compile(r"(?:youtube\.com/(?:watch\?(?:.*&)?v=|shorts/|embed/|live/)|youtu\.be/)([A-Za-z0-9_-]{11})")
_PLAYER_RESPONSE = re.compile(r"ytInitialPlayerResponse\s*=\s*(\{.*?\});\s*(?:var |</script>)", re.S)


def youtube_id(url: str) -> Optional[str]:
    m = _YT_ID.search(url or "")
    return m.group(1) if m else None


def parse_watch_page(html: str) -> dict:
    m = _PLAYER_RESPONSE.search(html or "")
    if not m:
        return {}
    try:
        details = json.loads(m.group(1)).get("videoDetails") or {}
    except ValueError:
        return {}
    try:
        duration = int(details.get("lengthSeconds") or 0)
    except (TypeError, ValueError):
        duration = 0
    return {
        "title": details.get("title") or "",
        "description": details.get("shortDescription") or "",
        "duration": duration,
    }


async def youtube_page_metadata(url: str) -> dict:
    video_id = youtube_id(url)
    if not video_id:
        return {}
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                      "(KHTML, like Gecko) Chrome/130.0 Safari/537.36",
        "Accept-Language": "en-US,en;q=0.9",
    }
    # SOCS skips the EU cookie-consent page, which has no video data. The
    # Shorts page and the watch page carry the same data, and one is
    # sometimes rate-limited when the other isn't, so both are tried.
    pages = [f"https://www.youtube.com/shorts/{video_id}", f"https://www.youtube.com/watch?v={video_id}"]
    if "/shorts/" not in url:
        pages.reverse()
    last_error: Optional[Exception] = None
    async with httpx.AsyncClient(timeout=20.0, follow_redirects=True, cookies={"SOCS": "CAI"}) as client:
        for page in pages:
            try:
                r = await client.get(page, headers=headers)
                r.raise_for_status()
            except httpx.HTTPError as e:
                last_error = e
                continue
            meta = parse_watch_page(r.text)
            if meta.get("title") or meta.get("description"):
                return meta
    if last_error:
        raise last_error
    return {}


async def lookup(url: str) -> dict:
    """Everything yt-dlp/whisper can pull for a video link: title,
    description, and (when short enough) a spoken-audio transcript. Never
    raises — a failed metadata fetch just means an empty result, which the
    caller treats the same as "nothing extra found"."""
    blocked = False
    try:
        meta = await fetch_metadata(url)
    except Exception as e:
        print(f"[video] metadata fetch failed for {url}: {e}")
        blocked = True
        meta = {"title": "", "description": "", "duration": 0}
        if youtube_id(url):
            try:
                meta = await youtube_page_metadata(url) or meta
            except Exception as page_error:
                print(f"[video] watch page fallback failed for {url}: {page_error}")

    # When yt-dlp was refused, downloading the audio would be refused too.
    transcript = None if blocked else await transcribe_audio(url, meta.get("duration") or 0)

    return {
        "title": meta.get("title", ""),
        "description": meta.get("description", ""),
        "transcript": transcript or "",
    }
