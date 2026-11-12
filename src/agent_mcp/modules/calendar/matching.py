"""Fuzzy title matching for ignore lists and duplicate detection."""

import re
import unicodedata
from difflib import SequenceMatcher

SIMILAR = 0.8


def normalize(title: str) -> str:
    """Casefold, drop punctuation and bracketed tags like "[#]"."""
    t = unicodedata.normalize("NFKC", title).casefold()
    t = re.sub(r"\[[^\]]*\]", " ", t)
    t = re.sub(r"[^\w]+", " ", t)
    return " ".join(t.split())


def similar(a: str, b: str) -> bool:
    na, nb = normalize(a), normalize(b)
    if not na or not nb:
        return False
    if na == nb or na in nb or nb in na:
        return True
    return SequenceMatcher(None, na, nb).ratio() >= SIMILAR


def matches_pattern(title: str, pattern: str) -> bool:
    """True when every pattern word starts some title word ("game" matches "games"), or the two are similar."""
    words = normalize(pattern).split()
    have = normalize(title).split()
    if words and all(any(h.startswith(w) for h in have) for w in words):
        return True
    return similar(title, pattern)
