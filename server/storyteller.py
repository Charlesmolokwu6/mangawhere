"""Chapter narration ("Storyteller mode"): turns a scraped chapter into an
MP3 someone can listen to instead of reading. 100% free — nothing here
needs a paid API.

A chapter is only images, so it goes through three steps:

1. OCR (RapidOCR, local, CPU) reads the text on every page, grouped into
   speech bubbles in reading order. This is the source of truth for what
   is actually said — a small vision model like moondream was tried here
   first and it paraphrased dialogue, invented lines that weren't on the
   page, and misdescribed scenes, so it isn't used to read pages.
2. If an Ollama server is reachable (OLLAMA_HOST, model NARRATION_MODEL),
   a small local text model rewrites that dialogue as audiobook-style
   narration — every line kept word-for-word, sound effects described,
   nothing invented. Without Ollama, the dialogue is read out as-is
   ("dialogue" mode), minus sound effects.
3. edge-tts (Microsoft Edge's free online voices) speaks it to an MP3.

Chapters take minutes on a CPU, so narration runs as a background job the
reader polls; finished audio is cached on disk by chapter + voice + mode.
"""
import asyncio
import hashlib
import json
import os
import re
import time
from pathlib import Path
from typing import Any, Dict, List, Optional
from urllib.parse import urlparse

import httpx

DATA_DIR = Path(__file__).resolve().parent.parent / "data"
NARRATION_DIR = DATA_DIR / "narration"

OLLAMA_HOST = os.environ.get("OLLAMA_HOST", "http://localhost:11434").rstrip("/")
if not OLLAMA_HOST.startswith("http"):
    OLLAMA_HOST = "http://" + OLLAMA_HOST
NARRATION_MODEL = os.environ.get("NARRATION_MODEL", "llama3.2:3b")

VOICES = {
    "christopher": "en-US-ChristopherNeural",
    "aria": "en-US-AriaNeural",
}
DEFAULT_VOICE = "christopher"

MAX_PAGES = 150
PAGES_PER_LLM_CHUNK = 8
MIN_OCR_CONFIDENCE = 0.6

# Image CDNs that reject a request without the right Referer (same rule as
# main.py's proxy); every other host gets the chapter's own site.
REFERER_OVERRIDES = {"cmzcdn.org": "https://comizy.io/"}
IMAGE_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
    "Accept": "image/avif,image/webp,image/*,*/*;q=0.8",
}


# --------------------------------------------------------------------------
# Availability
# --------------------------------------------------------------------------

_ocr_engine = None
_ocr_import_error: Optional[str] = None


def _get_ocr():
    # Imported lazily: onnxruntime + OpenCV are heavy, and most requests
    # never narrate anything.
    global _ocr_engine, _ocr_import_error
    if _ocr_engine is None and _ocr_import_error is None:
        try:
            from rapidocr_onnxruntime import RapidOCR

            _ocr_engine = RapidOCR()
        except Exception as e:  # ImportError, or OpenCV missing a system lib
            _ocr_import_error = str(e)
    return _ocr_engine


def _tts_available() -> bool:
    try:
        import edge_tts  # noqa: F401
    except ImportError:
        return False
    return True


def _ocr_installed() -> bool:
    if _ocr_engine is not None:
        return True
    if _ocr_import_error is not None:
        return False
    try:
        import importlib.util

        return importlib.util.find_spec("rapidocr_onnxruntime") is not None
    except Exception:
        return False


_ollama_checked_at = 0.0
_ollama_ok = False


async def storyteller_available() -> bool:
    """Whether a local Ollama with NARRATION_MODEL is reachable. Cached for
    a minute so /api/config doesn't probe it on every page load."""
    global _ollama_checked_at, _ollama_ok
    if time.time() - _ollama_checked_at < 60:
        return _ollama_ok
    _ollama_checked_at = time.time()
    try:
        async with httpx.AsyncClient(timeout=2.0) as client:
            response = await client.get(f"{OLLAMA_HOST}/api/tags")
            response.raise_for_status()
            names = {m.get("name", "") for m in response.json().get("models", [])}
        wanted = NARRATION_MODEL if ":" in NARRATION_MODEL else NARRATION_MODEL + ":latest"
        _ollama_ok = wanted in names
    except Exception:
        _ollama_ok = False
    return _ollama_ok


async def availability() -> Dict[str, Any]:
    available = _tts_available() and _ocr_installed()
    return {
        "available": available,
        "storyteller": available and await storyteller_available(),
        "voices": list(VOICES),
    }


# --------------------------------------------------------------------------
# Step 1: OCR
# --------------------------------------------------------------------------

# Short all-caps words that are real speech rather than a sound effect —
# anything else under 6 letters in an all-short-words bubble is treated as
# an effect ("KRR", "WOO", "SWOO").
_SPOKEN_SHORT_WORDS = {
    "A", "I", "AM", "AN", "AND", "ARE", "BUT", "CAN", "DID", "DO", "DON'T", "FOR",
    "GET", "GO", "HE", "HER", "HEY", "HIM", "HIS", "HOW", "IF", "IN", "IS", "IT",
    "IT'S", "ME", "MY", "NO", "NOT", "NOW", "OF", "OK", "OKAY", "ON", "OR", "OUR",
    "OUT", "RUN", "SHE", "SIR", "SO", "STOP", "THAT", "THE", "THEM", "THEN",
    "THERE", "THEY", "THIS", "TO", "UP", "US", "WAIT", "WAS", "WE", "WHAT", "WHEN",
    "WHERE", "WHO", "WHY", "WILL", "YES", "YOU", "YOUR", "HELP", "COME", "LOOK",
    "HERE", "THAT'S", "WHAT'S", "I'M", "YEAH", "SORRY", "THANK", "THANKS", "WELL",
}
_WORD = re.compile(r"[A-Z']+")


def is_sound_effect(text: str) -> bool:
    words = _WORD.findall(text.upper())
    if not words or len(words) > 6:
        return False
    return all(len(w) <= 5 and w not in _SPOKEN_SHORT_WORDS for w in words)


def group_into_bubbles(results: List[Any]) -> List[str]:
    """RapidOCR returns one entry per detected text line: [box, text, score]
    with box as four [x, y] corners. Lines belonging to the same speech
    bubble sit directly under one another and overlap horizontally, so
    they're merged into one bubble; bubbles come back top-to-bottom (the
    reading order of a vertical webtoon page)."""
    lines = []
    for box, text, score in results or []:
        if float(score) < MIN_OCR_CONFIDENCE or len(text.strip()) < 2:
            continue
        xs = [p[0] for p in box]
        ys = [p[1] for p in box]
        lines.append({"x0": min(xs), "x1": max(xs), "y0": min(ys), "y1": max(ys), "text": text.strip()})
    lines.sort(key=lambda l: (l["y0"], l["x0"]))

    bubbles: List[Dict[str, Any]] = []
    for line in lines:
        height = line["y1"] - line["y0"]
        for bubble in reversed(bubbles[-3:]):
            close_below = line["y0"] - bubble["y1"] < 0.9 * height
            overlaps = line["x0"] < bubble["x1"] and line["x1"] > bubble["x0"]
            if close_below and overlaps:
                bubble["lines"].append(line["text"])
                bubble["y1"] = max(bubble["y1"], line["y1"])
                bubble["x0"] = min(bubble["x0"], line["x0"])
                bubble["x1"] = max(bubble["x1"], line["x1"])
                break
        else:
            bubbles.append({**line, "lines": [line["text"]]})
    return [" ".join(b["lines"]) for b in bubbles]


# Comic lettering is tightly kerned, and at its original size the OCR model
# reads "GET US TOO IF" as "GETUSTOOIF". At 1.5x the gaps between words are
# wide enough to survive (checked on real pages: every run-together phrase
# came out correctly spaced). Very tall webtoon strips are capped so the
# enlarged image stays a sane size.
OCR_UPSCALE = 1.5
OCR_MAX_SIDE = 6000


def _upscale(image_bytes: bytes) -> bytes:
    import io

    from PIL import Image

    image = Image.open(io.BytesIO(image_bytes)).convert("RGB")
    scale = min(OCR_UPSCALE, OCR_MAX_SIDE / max(image.size))
    if scale > 1:
        image = image.resize((int(image.width * scale), int(image.height * scale)), Image.LANCZOS)
    out = io.BytesIO()
    image.save(out, "PNG")
    return out.getvalue()


def ocr_page(image_bytes: bytes) -> List[str]:
    engine = _get_ocr()
    if engine is None:
        raise RuntimeError(f"OCR unavailable: {_ocr_import_error}")
    try:
        image_bytes = _upscale(image_bytes)
    except Exception as e:  # not an image Pillow can read — let OCR try the original
        print(f"[narrate] couldn't upscale page: {e}")
    results, _ = engine(image_bytes)
    return group_into_bubbles(results)


async def fetch_image(client: httpx.AsyncClient, url: str, chapter_url: str) -> Optional[bytes]:
    host = urlparse(url).hostname or ""
    referer = next(
        (ref for suffix, ref in REFERER_OVERRIDES.items() if host == suffix or host.endswith("." + suffix)),
        f"{urlparse(chapter_url).scheme}://{urlparse(chapter_url).netloc}/",
    )
    try:
        response = await client.get(url, headers={**IMAGE_HEADERS, "Referer": referer})
        response.raise_for_status()
        return response.content
    except Exception as e:
        print(f"[narrate] image fetch failed for {url}: {e}")
        return None


# --------------------------------------------------------------------------
# Step 2: script
# --------------------------------------------------------------------------

STORYTELLER_SYSTEM_PROMPT = """You turn comic-page text into an audiobook narration that will be read aloud by a text-to-speech voice.
Rules:
- Include EVERY line of dialogue, in order, in quotation marks, with its spacing and capitalisation fixed (text is OCR output, so words may be run together; use normal sentence case).
- Short all-caps words like KRR, WOO, WAA are sound effects: never quote them; describe the sound in a few words instead.
- Between lines, add at most one short sentence of narration. Only describe what the dialogue and sounds imply. Never invent names, places, settings or events.
- Write plain prose paragraphs only. No labels like "Narrator:", no stage directions in brackets or parentheses, no headings."""

_LABEL = re.compile(r"^\s*(?:narrator|narration)\s*:\s*", re.I | re.M)
_STAGE_DIRECTION = re.compile(r"^\s*[\(\[][^\)\]]*[\)\]]\s*$", re.M)
_HEADING = re.compile(r"^\s*(?:#+|\*\*).*$", re.M)


def clean_llm_script(text: str) -> str:
    """Strip what a small model adds despite being told not to — speaker
    labels, bracketed stage directions, markdown headings — since the TTS
    voice would read them aloud."""
    text = _STAGE_DIRECTION.sub("", text)
    text = _HEADING.sub("", text)
    text = _LABEL.sub("", text)
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def _sentence_case(text: str) -> str:
    text = re.sub(r"\s+", " ", text).strip()
    if text.isupper():
        text = text.lower()
        text = re.sub(r"(^|[.!?]\s+)([a-z])", lambda m: m.group(1) + m.group(2).upper(), text)
        text = re.sub(r"\bi\b", "I", text)
        text = re.sub(r"\bi'", "I'", text)
    return text


def dialogue_script(pages: List[List[str]]) -> str:
    """No-LLM fallback: the dialogue itself, in reading order, with sound
    effects dropped and ALL-CAPS lettering normalised so the voice doesn't
    shout or spell words out."""
    paragraphs = []
    for bubbles in pages:
        spoken = [_sentence_case(b) for b in bubbles if not is_sound_effect(b)]
        if spoken:
            paragraphs.append(" ".join(spoken))
    return "\n\n".join(paragraphs)


def _pages_prompt(pages: List[List[str]], first_page_number: int) -> str:
    lines = []
    for offset, bubbles in enumerate(pages):
        text = " / ".join(bubbles) if bubbles else "(no text)"
        lines.append(f"[Page {first_page_number + offset}] {text}")
    return "\n".join(lines)


async def storyteller_script(pages: List[List[str]], progress=None) -> str:
    """Narration written by the local Ollama model, a few pages at a time
    (a 3B model loses track of long inputs), with the end of the previous
    chunk passed along so the story flows across chunks."""
    chunks = []
    previous = ""
    async with httpx.AsyncClient(timeout=600.0) as client:
        for start in range(0, len(pages), PAGES_PER_LLM_CHUNK):
            group = pages[start : start + PAGES_PER_LLM_CHUNK]
            if progress:
                progress(start, len(pages))
            if not any(group):
                continue
            user = _pages_prompt(group, start + 1)
            if previous:
                user = f"(The narration so far ended with: \"{previous[-300:]}\")\n\n" + user
            response = await client.post(
                f"{OLLAMA_HOST}/api/chat",
                json={
                    "model": NARRATION_MODEL,
                    "stream": False,
                    "messages": [
                        {"role": "system", "content": STORYTELLER_SYSTEM_PROMPT},
                        {"role": "user", "content": user},
                    ],
                    "options": {"temperature": 0.2, "num_predict": 700},
                },
            )
            response.raise_for_status()
            chunk = clean_llm_script(response.json().get("message", {}).get("content", ""))
            if chunk:
                chunks.append(chunk)
                previous = chunk
    return "\n\n".join(chunks)


# --------------------------------------------------------------------------
# Step 3: speech
# --------------------------------------------------------------------------

async def synthesize(script: str, voice: str, output_file: Path) -> None:
    import edge_tts

    tmp = output_file.with_suffix(".part")
    await edge_tts.Communicate(script, VOICES[voice]).save(str(tmp))
    tmp.replace(output_file)


# --------------------------------------------------------------------------
# Jobs
# --------------------------------------------------------------------------

_jobs: Dict[str, Dict[str, Any]] = {}
_run_lock = asyncio.Lock()  # one chapter at a time: OCR + LLM saturate the CPU


def job_id(chapter_url: str, voice: str, mode: str) -> str:
    return hashlib.sha1(f"{chapter_url}|{voice}|{mode}".encode()).hexdigest()[:20]


def audio_path(job: str) -> Path:
    return NARRATION_DIR / f"{job}.mp3"


def _public(job: Dict[str, Any]) -> Dict[str, Any]:
    return {k: v for k, v in job.items() if not k.startswith("_")}


_JOB_ID = re.compile(r"[0-9a-f]{20}")


def get_job(job: str) -> Optional[Dict[str, Any]]:
    if not _JOB_ID.fullmatch(job or ""):
        return None  # also keeps anything path-like out of the file lookups below
    if job in _jobs:
        return _public(_jobs[job])
    meta = NARRATION_DIR / f"{job}.json"
    if audio_path(job).exists() and meta.exists():
        return json.loads(meta.read_text())
    return None


async def start(chapter_url: str, images: List[str], voice: str) -> Dict[str, Any]:
    """Start (or reuse) narration for a chapter whose page images the caller
    already scraped. Returns the job's current public state."""
    if voice not in VOICES:
        voice = DEFAULT_VOICE
    mode = "storyteller" if await storyteller_available() else "dialogue"
    job = job_id(chapter_url, voice, mode)

    existing = get_job(job)
    if existing and existing.get("state") != "error":
        return existing

    state = {
        "job": job,
        "state": "queued",
        "mode": mode,
        "voice": voice,
        "done": 0,
        "total": min(len(images), MAX_PAGES),
        "message": "Waiting for another chapter to finish…",
        "error": None,
    }
    _jobs[job] = state
    if len(_jobs) > 100:
        for old in list(_jobs)[:-100]:
            if _jobs[old]["state"] in ("done", "error"):
                del _jobs[old]
    state["_task"] = asyncio.create_task(_run(state, chapter_url, images[:MAX_PAGES]))
    return _public(state)


async def _run(state: Dict[str, Any], chapter_url: str, images: List[str]) -> None:
    job = state["job"]
    try:
        async with _run_lock:
            NARRATION_DIR.mkdir(parents=True, exist_ok=True)

            state.update(state="reading", message="Reading the pages…")
            pages: List[List[str]] = []
            async with httpx.AsyncClient(timeout=30.0, follow_redirects=True) as client:
                for i, url in enumerate(images):
                    state["done"] = i
                    image = await fetch_image(client, url, chapter_url)
                    pages.append(await asyncio.to_thread(ocr_page, image) if image else [])
            state["done"] = len(images)

            if not any(pages):
                raise RuntimeError("Couldn't find any text on this chapter's pages.")

            script = ""
            if state["mode"] == "storyteller":
                state.update(state="writing", done=0, message="Writing the narration…")

                def progress(done, total):
                    state.update(done=done, total=total)

                try:
                    script = await storyteller_script(pages, progress)
                except Exception as e:
                    print(f"[narrate] storyteller model failed, reading dialogue instead: {e}")
                    state["mode"] = "dialogue"
            if not script:
                state["mode"] = "dialogue"
                script = dialogue_script(pages)
            if not script:
                raise RuntimeError("This chapter has no dialogue to read — only sound effects.")

            state.update(state="speaking", message="Recording the voice…")
            await synthesize(script, state["voice"], audio_path(job))
            (NARRATION_DIR / f"{job}.txt").write_text(script)

            state.update(state="done", message="Ready", done=state["total"])
            (NARRATION_DIR / f"{job}.json").write_text(json.dumps(_public(state)))
    except Exception as e:
        print(f"[narrate] job {job} failed: {e}")
        state.update(state="error", error=str(e) or "Narration failed.", message="Failed")
