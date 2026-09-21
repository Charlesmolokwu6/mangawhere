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
