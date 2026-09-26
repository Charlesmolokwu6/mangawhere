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
import tempfile
from pathlib import Path
from typing import Optional

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


async def lookup(url: str) -> dict:
    """Everything yt-dlp/whisper can pull for a video link: title,
    description, and (when short enough) a spoken-audio transcript. Never
    raises — a failed metadata fetch just means an empty result, which the
    caller treats the same as "nothing extra found"."""
    try:
        meta = await fetch_metadata(url)
    except Exception as e:
        print(f"[video] metadata fetch failed for {url}: {e}")
        meta = {"title": "", "description": "", "duration": 0}

    transcript = await transcribe_audio(url, meta.get("duration") or 0)

    return {
        "title": meta.get("title", ""),
        "description": meta.get("description", ""),
        "transcript": transcript or "",
    }
