import re
from typing import List

# Dice's-coefficient bigram similarity — good enough for "is this the same
# title" across two sites' slightly different naming/punctuation without
# needing a real NLP dependency.


def _bigrams(s: str) -> List[str]:
    s = re.sub(r"[^a-z0-9 ]", "", (s or "").lower()).strip()
    return [s[i : i + 2] for i in range(len(s) - 1)]


def similar(a: str, b: str) -> float:
    A, B = _bigrams(a), _bigrams(b)
    if not A or not B:
        return 1.0 if a == b else 0.0
    counts = {}
    for g in A:
        counts[g] = counts.get(g, 0) + 1
    hits = 0
    for g in B:
        if counts.get(g, 0) > 0:
            counts[g] -= 1
            hits += 1
    return 2 * hits / (len(A) + len(B))


# "Solo Leveling: Ragnarok" is a separate series from "Solo Leveling", but
# shares so much of its name that it clears the similarity bar on its own.
# A candidate that is the searched title plus a ":"/"-" subtitle is treated
# as a sequel/spin-off rather than the same series. A plain longer name
# ("Omniscient Reader" -> "Omniscient Reader's Viewpoint") or a bracketed
# alias ("Gosu (The Master)") has no such separator, so still matches.
_SUBTITLE_SEPARATOR = re.compile(r"^\s*(?::|[-–—]\s)")


def _plain(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "").lower()).strip()


def is_spinoff(query: str, candidate: str) -> bool:
    q, c = _plain(query), _plain(candidate)
    if not q or not c.startswith(q) or len(c) == len(q):
        return False
    return bool(_SUBTITLE_SEPARATOR.match(c[len(q):]))


def title_score(query: str, candidate: str) -> float:
    """similar(), except a sequel/spin-off of the searched title scores 0."""
    if is_spinoff(query, candidate):
        return 0.0
    return similar(query, candidate)
