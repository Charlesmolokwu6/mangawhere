"""Chapter narration ("Storyteller mode"): turns a scraped chapter into an
MP3 someone can listen to instead of reading. 100% free — nothing here
needs a paid API.

A chapter is only images, so it goes through three steps:

1. OCR (RapidOCR, local, CPU) reads the text on every page, grouped into
   speech bubbles in reading order. This is the source of truth for what
   is actually said — a small vision model like moondream was tried here
   first and it paraphrased dialogue, invented lines that weren't on the
   page, and misdescribed scenes, so it isn't used to read pages.
2. The script is assembled from that text alone: every spoken line in
   order, word for word, with a short attribution ("a voice shouts") and
   a neutral cue where a page only has sound effects. If an Ollama server
   is reachable (OLLAMA_HOST, model NARRATION_MODEL), a small local model
   picks how each line is delivered (whispers, screams...) from a fixed
   list; otherwise punctuation decides. The model never writes text:
   given free rein, even a 3B model strictly told not to invented
   dialogue and events that weren't in the chapter.
3. Kokoro (open-source, Apache 2.0, runs locally) speaks it to an MP3, in
   whichever of VOICES the listener picked. Microsoft Edge's online voices
   were used first, but they're only reachable through an unofficial route
   that heavy traffic would get blocked; Kokoro has no per-use limits.

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

# Listener-facing voice id -> (Kokoro voice, label shown in the picker).
# Each chapter's script is shared; only the recording is per voice, and a
# voice is only recorded for a chapter once someone actually picks it.
VOICES = {
    "fenrir": ("am_fenrir", "Fenrir (male)"),
    "echo": ("am_echo", "Echo (male)"),
    "eric": ("am_eric", "Eric (male)"),
    "emma": ("bf_emma", "Emma (female, British)"),
    "jessica": ("af_jessica", "Jessica (female)"),
}
DEFAULT_VOICE = "fenrir"

# Kokoro's model files (from the kokoro-onnx project's model release),
# downloaded on first use into KOKORO_DIR. The full-precision model is the
# default: on a 4-core CPU it records ~2.8x faster than real time using
# ~1GB of RAM, where the int8 one (KOKORO_MODEL=kokoro-v1.0.int8.onnx) saves
# memory but ran slower than real time on the same machine.
KOKORO_DIR = Path(os.environ.get("KOKORO_DIR", str(DATA_DIR / "kokoro")))
KOKORO_MODEL = os.environ.get("KOKORO_MODEL", "kokoro-v1.0.onnx")
KOKORO_VOICES_FILE = "voices-v1.0.bin"
KOKORO_RELEASE = "https://github.com/thewh1teagle/kokoro-onnx/releases/download/model-files-v1.0/"
MP3_BITRATE = 64  # kbps mono: speech stays clear, ~0.5MB per minute
PARAGRAPH_PAUSE = 0.45  # seconds of silence between paragraphs

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
        import importlib.util

        return all(importlib.util.find_spec(m) is not None for m in ("kokoro_onnx", "lameenc"))
    except Exception:
        return False


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


# Narration needs ~600MB at peak (OCR), more than Render's free plan has —
# running it there would crash the whole app, not just narration. So it is
# off by default on Render (which sets RENDER=true) and on everywhere else;
# NARRATION_ENABLED overrides either way.
def enabled_here() -> bool:
    setting = os.environ.get("NARRATION_ENABLED", "").strip().lower()
    if setting in ("1", "true", "yes", "on"):
        return True
    if setting in ("0", "false", "no", "off"):
        return False
    return not os.environ.get("RENDER")


# Where narration runs when it isn't here: another deployment of this same
# app, on a machine with enough memory (see README.md). The reader then
# sends its narration requests straight there.
def remote_url() -> str:
    return os.environ.get("NARRATION_URL", "").strip().rstrip("/")


async def availability() -> Dict[str, Any]:
    if remote_url():
        # The reader asks that server itself what it can do.
        return {"available": True, "base": remote_url()}
    if not enabled_here():
        return {"available": False}
    available = _tts_available() and _ocr_installed()
    return {
        "available": available,
        "storyteller": available and await storyteller_available(),
        "voices": [{"id": vid, "name": label} for vid, (_, label) in VOICES.items()],
        "default_voice": DEFAULT_VOICE,
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


def is_noise(text: str) -> bool:
    """OCR garbage — stray symbols or a misread panel border ("53>>3") — or
    a game-style status screen repeating one label ("Injury C Injury A
    Injury B ...")."""
    letters = sum(c.isalpha() for c in text)
    if letters < 2 or letters < 0.5 * len(text.replace(" ", "")):
        return True
    words = re.findall(r"[a-z]{3,}", text.lower())
    return len(words) >= 4 and max(words.count(w) for w in set(words)) >= 0.5 * len(words)


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
    return [" ".join(b["lines"]) for b in _bubbles(results)]


def _bubbles(results: List[Any]) -> List[Dict[str, Any]]:
    """group_into_bubbles, keeping where each bubble is on the page."""
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
    return bubbles


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

# How a line is delivered. The storyteller model only ever picks one of
# these per line — it never writes text of its own. An earlier version let
# it write narration freely around the dialogue, and even a strictly
# prompted 3B model invented lines nobody says, creatures and settings
# that aren't in the chapter. Assembling the script from the real OCR text
# makes that impossible.
DELIVERIES = ["says", "shouts", "asks", "whispers", "mutters", "gasps", "groans", "screams", "laughs", "sighs"]

DELIVERY_SYSTEM_PROMPT = """You label how each line of comic dialogue is spoken, for an audiobook.
For each numbered line, choose exactly one word from this list: """ + ", ".join(DELIVERIES) + """.
Judge only from the words and punctuation of the line and the lines around it.
Reply with JSON only, mapping each line number to its word, e.g. {"1": "shouts", "2": "asks"}."""


# --- OCR text repair -------------------------------------------------------
# Comic fonts trip OCR in the same few ways on every series: words run
# together when lettering is small ("HOWCOMESUNGJIN"), U read as L in some
# fonts ("MLCH", "HLNTERS"), mixed-case output from all-caps lettering
# ("ARen'T"), CJK symbols from system-window icons, and ".." endings. The
# repairs below only ever swap in real dictionary words (wordninja's
# 126k-word English list); anything they can't explain is left as it was.

_word_cost: Optional[Dict[str, float]] = None


def _words() -> Dict[str, float]:
    global _word_cost
    if _word_cost is None:
        try:
            import wordninja

            _word_cost = dict(wordninja.DEFAULT_LANGUAGE_MODEL._wordcost)
        except Exception:
            _word_cost = {}
    return _word_cost


L_AS_U_PENALTY = 4.0  # an L->U reading must be worth it: real words always win
_MAX_WORD = 20
# When a token is split into several words, every piece of 3 letters or
# less must be a common word. Without this, names get shredded into rare
# dictionary fragments ("UREK MAZINO" -> "lr ek maz ino") instead of being
# left alone.
_SHORT_PIECE_MAX_COST = 12.5
_CONTRACTION = re.compile(r"^(.+?)('(?:s|t|re|ll|m|ve|d))$")


def _variants(chunk: str):
    """The chunk as read, plus every reading with some L's taken as U's."""
    yield chunk, 0.0
    positions = [i for i, c in enumerate(chunk) if c == "l"][:3]
    for mask in range(1, 1 << len(positions)):
        chars = list(chunk)
        swaps = 0
        for bit, pos in enumerate(positions):
            if mask >> bit & 1:
                chars[pos] = "u"
                swaps += 1
        yield "".join(chars), swaps * L_AS_U_PENALTY


def repair_word(token: str) -> str:
    """Best reading of one OCR token (lowercase letters/apostrophes) as one
    or more dictionary words, allowing L->U misreads. Single letters other
    than "a"/"i" aren't allowed as words, and if no reading made only of
    dictionary words exists, the token comes back unchanged."""
    words = _words()
    if not words or token in words:
        return token
    n = len(token)
    best: List[Optional[tuple]] = [None] * (n + 1)  # (cost, words)
    best[0] = (0.0, [])
    for end in range(1, n + 1):
        for start in range(max(0, end - _MAX_WORD), end):
            if best[start] is None:
                continue
            for word, penalty in _variants(token[start:end]):
                if word not in words or (len(word) == 1 and word not in ("a", "i")):
                    continue
                cost = best[start][0] + words[word] + penalty
                if best[end] is None or cost < best[end][0]:
                    best[end] = (cost, best[start][1] + [word])
    pieces = best[n][1] if best[n] else None
    if pieces and len(pieces) > 1 and any(
        len(p) <= 3 and p not in ("a", "i") and "'" not in p and words[p] > _SHORT_PIECE_MAX_COST
        for p in pieces
    ):
        pieces = None
    if pieces:
        return " ".join(pieces)
    contraction = _CONTRACTION.match(token)
    if contraction:  # "girl's": repair "girl", keep the "'s" attached
        return repair_word(contraction.group(1)) + contraction.group(2)
    return token


# A run of non-Latin characters (system-window icons, stray CJK), along
# with any letters glued to it — OCR reads the icon "区可" plus the start of
# the next glyph as "区可B".
_NON_LATIN = re.compile(r"\S*[^\x00-\u024f\u2018-\u201f\u2026\s]\S*")
_CREDITS = re.compile(
    r"\b(?:art ?by|story ?by|comic ?by|novel ?by|adapted ?by|original ?(?:story|novel)|translat\w*|proofread\w*"
    r"|typeset\w*|scanlat\w*|discord|patreon|read (?:it )?(?:at|on)|episode \d+|chapter \d+"
    # Publishers' end pages: staff lists and copyright notices.
    r"|copyright|unauthori[sz]ed|all rights reserved|head ?of ?\w+|\w*quality ?(?:control|check)|editor ?in ?chief)\b",
    re.I,
)


# Copyright notices are often OCR'd without spaces ("Thisworkisprotectedby
# copyrightlaws"), where \b can't find the word.
_CREDITS_ANYWHERE = re.compile(r"copyright|unauthori[sz]ed|allrightsreserved", re.I)


def _mostly_upper(text: str) -> bool:
    letters = [c for c in text if c.isalpha()]
    return bool(letters) and sum(c.isupper() for c in letters) >= 0.7 * len(letters)


def _capitalise_sentences(text: str) -> str:
    text = re.sub(r"(^|[.!?]\s+|[\"\u201c]\s*)([a-z])", lambda m: m.group(1) + m.group(2).upper(), text)
    return re.sub(r"\bi(?=\b|')", "I", text)


def normalise_line(text: str) -> str:
    text = _NON_LATIN.sub(" ", text)
    text = text.replace("[", " ").replace("]", " ").replace("|", " ")
    text = re.sub(r"(?<=[A-Za-z])(?=\d)|(?<=\d)(?=[A-Za-z])", " ", text)  # "JUST2YEARS"
    text = text.replace("\u2026", "...")
    text = re.sub(r"\.{2,}", "...", text)
    text = re.sub(r"([.,!?]+)(?=[A-Za-z])", r"\1 ", text)  # "association.how may", "out,but"
    text = re.sub(r"(?<=[A-Za-z])- (?=[A-Za-z])", "", text)  # a word hyphenated across lines: "EVERY- ONE"
    text = re.sub(r"\s+", " ", text).strip()
    if not _mostly_upper(text):
        return text  # already normal lettering — leave it alone
    text = text.lower()
    text = re.sub(r"[a-z']+", lambda m: repair_word(m.group(0)), text)
    text = re.sub(r"(?<=[a-z]) '(s|t|re|ll|m|ve|d)\b", r"'\1", text)  # "girl 's" -> "girl's"
    return _capitalise_sentences(text)


_sentence_case = normalise_line  # kept for readability at call sites


def prepare_pages(pages: List[List[str]]) -> List[Dict[str, Any]]:
    """Each page's OCR bubbles split into spoken lines (sentence-cased, so
    the voice doesn't shout or spell words out) and whether the page also
    had sound effects. Noise is dropped. Doing this before the model sees
    anything leaves it less to get wrong."""
    prepared = []
    for bubbles in pages:
        lines, effects = [], False
        for bubble in bubbles:
            line, effect = _speakable(bubble)
            effects = effects or effect
            if line:
                lines.append(line)
        prepared.append({"lines": lines, "effects": effects})
    return prepared


def _speakable(bubble: str):
    """(the bubble as a line to speak, or None; whether it was a sound effect)."""
    if is_noise(bubble) or _CREDITS.search(bubble) or _CREDITS_ANYWHERE.search(bubble):
        return None, False
    if is_sound_effect(bubble):
        return None, True
    line = normalise_line(bubble)
    return (line if line and not is_noise(line) else None), False


def right_to_left(bubbles: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Bubbles in a manga page's reading order: top to bottom by row, and
    right to left along each row. A row is the bubbles whose tops sit
    beside the row's first (highest) bubble; measuring against that one
    bubble, not the whole row, keeps a staircase of bubbles running down
    the page from merging into one long row."""
    rows: List[List[Dict[str, Any]]] = []
    for bubble in sorted(bubbles, key=lambda b: b["y0"]):
        if rows and bubble["y0"] < rows[-1][0]["y1"]:
            rows[-1].append(bubble)
        else:
            rows.append([bubble])
    return [b for row in rows for b in sorted(row, key=lambda b: -b["x1"])]


def speakable_lines(results: List[Any], rtl: bool = False) -> Dict[str, Any]:
    """One page's raw OCR lines as the lines to speak, the way prepare_pages
    has them, each with the top of its bubble on the page (y, in the page
    image's pixels) so the reader can scroll to it while it's spoken.
    rtl: the page is manga, read right to left."""
    lines, effects = [], False
    bubbles = _bubbles(results)
    for bubble in right_to_left(bubbles) if rtl else bubbles:
        line, effect = _speakable(" ".join(bubble["lines"]))
        effects = effects or effect
        if line:
            lines.append({"text": line, "delivery": delivery_by_punctuation(line), "y": round(bubble["y0"])})
    return {"lines": lines, "effects": effects}


def delivery_by_punctuation(line: str) -> str:
    if line.endswith("?") or line.endswith("?!") or line.endswith("!?"):
        return "asks"
    if line.endswith("!"):
        return "shouts"
    if line.endswith("...") or line.endswith("…"):
        return "mutters"
    return "says"


def _quoted(line: str) -> str:
    # Ends a quoted line so the attribution after it reads naturally:
    # "We go." she says -> "We go," she says; "?" / "!" / "..." stay.
    if line.endswith(".") and not line.endswith("..."):
        line = line[:-1] + ","
    elif not line.endswith((",", "?", "!", "...", "…")):
        line = line + ","
    return f"\u201c{line}\u201d"


_SPEAKERS = ["a voice", "someone"]
# Deliberately vague: OCR can tell a page has sound effects, not what made
# them, so the cue can't claim anything more specific than a sound.
_EFFECT_CUES = ["A sound rings out.", "Noise fills the air.", "There's a loud sound.", "The noise grows."]


def assemble_script(prepared: List[Dict[str, Any]], deliveries: Optional[List[str]] = None) -> str:
    """The narration itself: every spoken line, in order, word for word.
    The first line on each page gets an attribution ("a voice shouts"),
    since lines on the same page usually continue the same exchange; a
    page with only sound effects gets a short, fixed cue. Nothing else is
    added, so nothing can be made up."""
    paragraphs = []
    index = 0
    speaker = 0
    last_was_effect = False
    effect = 0
    for page in prepared:
        lines = page["lines"]
        if not lines:
            if page["effects"] and paragraphs and not last_was_effect:
                paragraphs.append(_EFFECT_CUES[effect % len(_EFFECT_CUES)])
                effect += 1
                last_was_effect = True
            continue
        last_was_effect = False
        delivery = (deliveries[index] if deliveries else None) or delivery_by_punctuation(lines[0])
        first = f"{_quoted(lines[0])} {_SPEAKERS[speaker % len(_SPEAKERS)]} {delivery}."
        speaker += 1
        rest = f" \u201c{' '.join(lines[1:])}\u201d" if len(lines) > 1 else ""
        paragraphs.append(first + rest)
        index += len(lines)
    return "\n\n".join(paragraphs)


def dialogue_script(pages: List[List[str]]) -> str:
    """No-LLM script: the dialogue, attributed by punctuation alone."""
    return assemble_script(prepare_pages(pages))


async def storyteller_script(pages: List[List[str]], progress=None) -> str:
    """Same script as dialogue_script(), but with the local Ollama model
    choosing how each line is delivered (whispered, screamed...) from the
    fixed DELIVERIES list, a chunk of pages at a time. Anything it answers
    that isn't on that list falls back to the punctuation rule."""
    prepared = prepare_pages(pages)
    all_lines = [line for page in prepared for line in page["lines"]]
    deliveries: List[Optional[str]] = [None] * len(all_lines)
    chunk_size = PAGES_PER_LLM_CHUNK * 2  # lines, not pages
    async with httpx.AsyncClient(timeout=600.0) as client:
        for start in range(0, len(all_lines), chunk_size):
            if progress:
                progress(start, len(all_lines))
            chunk = all_lines[start : start + chunk_size]
            numbered = "\n".join(f'{i + 1}. "{line}"' for i, line in enumerate(chunk))
            response = await client.post(
                f"{OLLAMA_HOST}/api/chat",
                json={
                    "model": NARRATION_MODEL,
                    "stream": False,
                    "format": "json",
                    "messages": [
                        {"role": "system", "content": DELIVERY_SYSTEM_PROMPT},
                        {"role": "user", "content": numbered},
                    ],
                    "options": {"temperature": 0.1, "num_predict": 20 + 12 * len(chunk)},
                },
            )
            response.raise_for_status()
            try:
                answer = json.loads(response.json().get("message", {}).get("content", "") or "{}")
            except ValueError:
                answer = {}
            if not isinstance(answer, dict):
                answer = {}
            for i in range(len(chunk)):
                word = str(answer.get(str(i + 1), "")).strip().lower()
                if word in DELIVERIES:
                    deliveries[start + i] = word
    if progress:
        progress(len(all_lines), len(all_lines))
    return assemble_script(prepared, deliveries)


# --------------------------------------------------------------------------
# Step 3: speech
# --------------------------------------------------------------------------

_kokoro = None
_kokoro_lock = asyncio.Lock()


def _download(name: str) -> None:
    target = KOKORO_DIR / name
    if target.exists():
        return
    KOKORO_DIR.mkdir(parents=True, exist_ok=True)
    tmp = target.with_suffix(target.suffix + ".part")
    with httpx.stream("GET", KOKORO_RELEASE + name, follow_redirects=True, timeout=600.0) as response:
        response.raise_for_status()
        with open(tmp, "wb") as out:
            for block in response.iter_bytes(1 << 20):
                out.write(block)
    tmp.replace(target)


def _load_kokoro():
    from kokoro_onnx import Kokoro

    _download(KOKORO_MODEL)
    _download(KOKORO_VOICES_FILE)
    return Kokoro(str(KOKORO_DIR / KOKORO_MODEL), str(KOKORO_DIR / KOKORO_VOICES_FILE))


async def _get_kokoro():
    global _kokoro
    async with _kokoro_lock:
        if _kokoro is None:
            _kokoro = await asyncio.to_thread(_load_kokoro)
    return _kokoro


def _record(kokoro, script: str, kokoro_voice: str) -> bytes:
    """Speak the script paragraph by paragraph (short inputs keep Kokoro's
    pacing natural), with a short pause between them, then encode as MP3."""
    import lameenc
    import numpy as np

    pieces, rate = [], 24000
    for paragraph in [p.strip() for p in script.split("\n\n") if p.strip()]:
        samples, rate = kokoro.create(paragraph, voice=kokoro_voice, speed=1.0, lang="en-us")
        pieces.append(samples)
        pieces.append(np.zeros(int(rate * PARAGRAPH_PAUSE), dtype=samples.dtype))
    audio = np.concatenate(pieces) if pieces else np.zeros(0, dtype="float32")
    pcm = (np.clip(audio, -1.0, 1.0) * 32767).astype("<i2").tobytes()

    encoder = lameenc.Encoder()
    encoder.set_bit_rate(MP3_BITRATE)
    encoder.set_in_sample_rate(rate)
    encoder.set_channels(1)
    encoder.set_quality(2)
    return encoder.encode(pcm) + encoder.flush()


async def synthesize(script: str, voice: str, output_file: Path) -> None:
    kokoro = await _get_kokoro()
    mp3 = await asyncio.to_thread(_record, kokoro, script, VOICES[voice][0])
    tmp = output_file.with_suffix(".part")
    tmp.write_bytes(mp3)
    tmp.replace(output_file)


# --------------------------------------------------------------------------
# Jobs
# --------------------------------------------------------------------------

_jobs: Dict[str, Dict[str, Any]] = {}
_run_lock = asyncio.Lock()  # one chapter at a time: OCR + LLM saturate the CPU


# Part of every cached narration's key — bump it whenever the script a
# chapter produces changes, so old cached MP3s aren't served again.
SCRIPT_VERSION = 4


def job_id(chapter_url: str, voice: str, mode: str) -> str:
    key = f"{chapter_url}|{voice}|{mode}|v{SCRIPT_VERSION}"
    return hashlib.sha1(key.encode()).hexdigest()[:20]


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

            state.update(
                state="speaking",
                message="Recording the voice…" if _kokoro else "Loading the voice (first time only)…",
            )
            await synthesize(script, state["voice"], audio_path(job))
            (NARRATION_DIR / f"{job}.txt").write_text(script)

            state.update(state="done", message="Ready", done=state["total"])
            (NARRATION_DIR / f"{job}.json").write_text(json.dumps(_public(state)))
    except Exception as e:
        print(f"[narrate] job {job} failed: {e}")
        state.update(state="error", error=str(e) or "Narration failed.", message="Failed")
