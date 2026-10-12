"""Is this text really a manga title? Checked against AniList.

Comments, post pictures and captions only propose names; AniList decides.
One GraphQL request checks a whole batch of names, and matches() makes
sure AniList's best guess really is the name given, not just the nearest
thing its search could find.
"""
import json
import re
from difflib import SequenceMatcher
from typing import Dict, List

import httpx

ANILIST = "https://graphql.anilist.co"


def _key(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", (name or "").lower())


def _names_of(media: Dict[str, object]) -> List[str]:
    t = media.get("title") or {}
    return [n for n in [t.get("english"), t.get("romaji"), t.get("native")] + list(media.get("synonyms") or []) if n]


def matches(candidate: str, media: Dict[str, object], strong: bool = True) -> bool:
    """Whether AniList's title really is what the comment said — not just
    the nearest thing its search could find for "give it time". A name
    picked out of a sentence, or a single word, has to match exactly, and
    a single word only counts when the comment stressed it (strong)."""
    c = _key(candidate)
    one_word = len(candidate.split()) == 1
    if len(c) < 4 or (one_word and not strong):
        return False
    exact_only = one_word or not strong
    t = media.get("title") or {}
    names = [t.get("english"), t.get("romaji")] if one_word else _names_of(media)
    for name in filter(None, names):
        n = _key(name)
        if not n:
            continue
        if c == n:
            return True
        if exact_only:
            continue
        # "Primal hunter" for "The Primal Hunter"; "Atsumare fushigi
        # kenkyubu" for "Atsumare! Fushigi Kenkyuubu".
        if len(c) >= 6 and c in n and len(c) >= 0.75 * len(n):
            return True
        if len(c) >= 8 and SequenceMatcher(None, c, n).ratio() >= 0.9:
            return True
    return False


def display_title(media: Dict[str, object]) -> str:
    t = media.get("title") or {}
    return t.get("english") or t.get("romaji") or t.get("native") or ""


async def search_many(client: httpx.AsyncClient, names: List[str]) -> List[List[Dict[str, object]]]:
    """AniList's top three results for each name, in one request."""
    if not names:
        return []
    parts = []
    for i, name in enumerate(names):
        parts.append(f"c{i}:Page(perPage:3){{media(search:{json.dumps(name)},type:MANGA)"
                     f"{{id title{{english romaji native}} synonyms}}}}")
    r = await client.post(ANILIST, json={"query": "query{" + " ".join(parts) + "}"},
                          headers={"Accept": "application/json"})
    r.raise_for_status()
    data = r.json().get("data") or {}
    return [((data.get(f"c{i}") or {}).get("media") or []) for i in range(len(names))]
