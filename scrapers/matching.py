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


_STOP_WORDS = {"the", "a", "an", "of"}


def _words(s: str) -> set:
    # Apostrophes dropped and a trailing plural/possessive "s" trimmed, so
    # "Reader's", "Readers" and "Reader" all count as the same word.
    words = re.findall(r"[a-z0-9]+", (s or "").lower().replace("'", "").replace("’", ""))
    return {w[:-1] if len(w) > 3 and w.endswith("s") else w for w in words} - _STOP_WORDS


def _word_recall(query: str, candidate: str) -> float:
    q = _words(query)
    if not q:
        return 1.0
    return len(q & _words(candidate)) / len(q)


def title_score(query: str, candidate: str) -> float:
    """similar(), except a sequel/spin-off of the searched title scores 0,
    and the score is scaled by how many of the query's words appear in the
    candidate as whole words. Character bigrams alone let "Solo Max-Level
    Newbie" clear the bar for "Solo Leveling" ("level" sits inside
    "leveling"), and since the source with the most chapters wins, that
    unrelated series was served for every Solo Leveling search."""
    if is_spinoff(query, candidate):
        return 0.0
    return similar(query, candidate) * _word_recall(query, candidate)
