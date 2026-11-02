"""Curated alternate names for titles that reader sites index under a
different name than the one a clip's caption tends to use.

Some series circulate on TikTok/YouTube/Reddit under a romanized or fan name
that no reader site lists, while every site indexes them under one English
title. "Drache Heart" (a romanization of the Korean 드라코 하트) is only ever
found on readers as "I Have a Dragon's Heart" — searching the caption name
finds nothing anywhere, so the title looks unreadable even though it's right
there under its English name.

Each group below is a set of names that all refer to the same series. Looking
one up yields the group's other names, which the title search then also tries
(see main._find_names). This is deliberately a small, hand-checked list, not a
general matcher: it only exists for names our normal title matching can't
bridge on its own.
"""

import re
from typing import Dict, List, Tuple

# Each tuple is one series' interchangeable names. Include every spelling a
# clip might use and the name readers actually index it under. Order within a
# group doesn't matter — the search tries whichever ones aren't already known.
ALIAS_GROUPS: Tuple[Tuple[str, ...], ...] = (
    ("Drache Heart", "Draco Heart", "I Have a Dragon's Heart"),
)


def _norm(name: str) -> str:
    """Lowercase and collapse anything non-alphanumeric to single spaces, so
    "I Have a Dragon's Heart", "i have a dragons heart" and "I-Have-A-Dragons-
    Heart" all key to the same group."""
    return re.sub(r"[^a-z0-9]+", " ", (name or "").lower()).strip()


_GROUP_BY_NAME: Dict[str, Tuple[str, ...]] = {}
for _group in ALIAS_GROUPS:
    for _member in _group:
        _GROUP_BY_NAME[_norm(_member)] = _group


def aliases_for(name: str) -> List[str]:
    """Other names the same series is known by, or [] if none are curated.
    The passed-in name is left out of the result."""
    key = _norm(name)
    group = _GROUP_BY_NAME.get(key)
    if not group:
        return []
    return [member for member in group if _norm(member) != key]
