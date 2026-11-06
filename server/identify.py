"""Last-resort "what manga is this?" from a plot description.

When a clip's caption, its description's explicit "Manhwa Name:" label, and a
title search all come up empty, the story itself is often still right there in
the description or the audio transcript ("a pacifist dragged into a world where
every dispute is settled with fists..."). A language model that knows manga can
often name the series from that alone.

Provider-agnostic on purpose: it talks to any OpenAI-compatible chat-completions
endpoint, so which model — and whether it's free — is a configuration choice,
not baked in. Defaults target Groq's free tier; point LLM_BASE_URL / LLM_MODEL
at OpenRouter, Cerebras, a local Ollama, etc. to switch with no code change.

    LLM_API_KEY    the provider key (required; unset = feature off)
    LLM_BASE_URL   OpenAI-compatible base, default https://api.groq.com/openai/v1
    LLM_MODEL      model id, default llama-3.3-70b-versatile

The model's answer is never shown on its own: the caller feeds the returned
title back through the normal title search, so a guess only ever appears when
it matches a real series — a wrong or hallucinated name just finds nothing,
exactly like any other miss.
"""
import os
from typing import Optional

import httpx

_DEFAULT_BASE = "https://api.groq.com/openai/v1"
_DEFAULT_MODEL = "llama-3.3-70b-versatile"

_SYSTEM_PROMPT = (
    "You identify manga, manhwa and manhua from a plot description or a video "
    "caption. Reply with ONLY the exact official English title of the series — "
    "no quotes, no punctuation, no explanation, nothing else. If you are not "
    "confident which specific series it is, reply with exactly: UNKNOWN"
)


def _config() -> dict:
    return {
        "base": os.environ.get("LLM_BASE_URL", _DEFAULT_BASE).strip().rstrip("/") or _DEFAULT_BASE,
        "key": os.environ.get("LLM_API_KEY", "").strip(),
        "model": os.environ.get("LLM_MODEL", "").strip() or _DEFAULT_MODEL,
    }


def available() -> bool:
    """True once a key is set. Without one the whole feature stays off and the
    endpoint simply returns no guess."""
    return bool(_config()["key"])


def _clean(answer: str) -> Optional[str]:
    """A usable title, or None. Rejects the model's non-answers and any reply
    that's a sentence rather than a name, so we never search on an apology or
    an explanation the model tacked on."""
    answer = (answer or "").strip().strip('"').strip("'").strip()
    if not answer or answer.upper().strip(".!") == "UNKNOWN":
        return None
    if len(answer) > 100 or "\n" in answer or len(answer.split()) > 12:
        return None
    return answer


async def identify_from_plot(text: str) -> Optional[str]:
    cfg = _config()
    text = (text or "").strip()
    if not cfg["key"] or not text:
        return None
    body = {
        "model": cfg["model"],
        "temperature": 0,
        "max_tokens": 30,
        "messages": [
            {"role": "system", "content": _SYSTEM_PROMPT},
            {"role": "user", "content": text[:2000]},
        ],
    }
    try:
        async with httpx.AsyncClient(timeout=20.0) as client:
            response = await client.post(
                cfg["base"] + "/chat/completions",
                headers={"Authorization": "Bearer " + cfg["key"]},
                json=body,
            )
            response.raise_for_status()
            data = response.json()
        answer = (data["choices"][0]["message"].get("content") or "")
    except Exception as e:
        print(f"[identify] lookup failed ({e})")
        return None
    return _clean(answer)
