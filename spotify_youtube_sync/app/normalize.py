"""Title / artist normalization and similarity helpers.

Everything here is pure and deterministic so it is cheap to unit-test.
The goal is to make "Beyoncé - Halo (Official Video)" and
"Beyonce  Halo   [OFFICIAL VIDEO]" comparable without destroying signal that
actually matters (a real "(Live)" or "(Remix)" tag).
"""

from __future__ import annotations

import re
import unicodedata
from difflib import SequenceMatcher

__all__ = [
    "strip_accents",
    "normalize_text",
    "normalize_title",
    "normalize_artist",
    "split_artists",
    "extract_parentheticals",
    "title_core",
    "similarity",
    "artist_similarity",
    "token_set_ratio",
    "contains_any",
]

# Words that describe *packaging* of an otherwise-correct match. Removing them
# from the "core" title makes comparison fair.
_DECORATION_PHRASES = [
    "official music video",
    "official lyric video",
    "official lyrics video",
    "official audio",
    "official video",
    "official visualizer",
    "official visualiser",
    "official hd video",
    "official 4k video",
    "official",
    "lyric video",
    "lyrics video",
    "lyrics",
    "lyric",
    "visualizer",
    "visualiser",
    "audio only",
    "full audio",
    "hq audio",
    "hd audio",
    "audio",
    "hd",
    "4k",
    "m/v",
    "mv",
    "with lyrics",
    "explicit",
    "clean version",
    "clean",
]

# Feature markers -> collapsed so "feat.", "ft", "featuring", "with" all match.
_FEAT_RE = re.compile(
    r"\s*[\(\[]?\s*(feat\.?|ft\.?|featuring|with)\s+.*?[\)\]]?\s*$",
    re.IGNORECASE,
)
_FEAT_INLINE_RE = re.compile(r"\s*(feat\.?|ft\.?|featuring)\s+", re.IGNORECASE)

_PAREN_RE = re.compile(r"[\(\[\{]([^\(\)\[\]\{\}]*)[\)\]\}]")
_MULTISPACE_RE = re.compile(r"\s+")
_DASH_RE = re.compile(r"[‐-―−\-]+")
_NON_ALNUM_RE = re.compile(r"[^0-9a-z\s]")

_ARTIST_SPLIT_RE = re.compile(
    r"\s*(?:,|;|/|\bx\b|\bX\b|&|\+|\band\b|\bvs\.?\b|\bwith\b|feat\.?|ft\.?|featuring|、|・|×)\s*",
    re.IGNORECASE,
)


def strip_accents(text: str) -> str:
    decomposed = unicodedata.normalize("NFKD", text)
    return "".join(ch for ch in decomposed if not unicodedata.combining(ch))


def normalize_text(text: str) -> str:
    """Lowercase, de-accent, drop punctuation, collapse whitespace."""
    if not text:
        return ""
    text = strip_accents(text).lower()
    text = text.replace("&", " and ")
    text = _DASH_RE.sub(" ", text)
    text = _NON_ALNUM_RE.sub(" ", text)
    text = _MULTISPACE_RE.sub(" ", text).strip()
    return text


def extract_parentheticals(text: str) -> list[str]:
    return [m.group(1).strip() for m in _PAREN_RE.finditer(text or "")]


def _remove_decorations(text: str) -> str:
    out = text
    for phrase in _DECORATION_PHRASES:
        out = re.sub(rf"\b{re.escape(phrase)}\b", " ", out, flags=re.IGNORECASE)
    return _MULTISPACE_RE.sub(" ", out).strip()


def normalize_title(title: str) -> str:
    """Normalized full title, keeping meaningful parentheticals like (Live)."""
    if not title:
        return ""
    working = title
    working = _FEAT_RE.sub("", working)
    working = _FEAT_INLINE_RE.sub(" ", working)
    # Drop bracket groups that are *only* decoration; keep the rest inline.
    def _paren_sub(match: re.Match[str]) -> str:
        inner = match.group(1)
        cleaned = _remove_decorations(normalize_text(inner))
        return f" {cleaned} " if cleaned else " "

    working = _PAREN_RE.sub(_paren_sub, working)
    working = _remove_decorations(working)
    return normalize_text(working)


def title_core(title: str) -> str:
    """The most aggressive form: title with *all* parentheticals removed.

    Used as a secondary comparison so "Song" matches "Song (Remastered 2011)".
    """
    if not title:
        return ""
    working = _FEAT_RE.sub("", title)
    working = _PAREN_RE.sub(" ", working)
    working = _remove_decorations(working)
    return normalize_text(working)


def normalize_artist(artist: str) -> str:
    if not artist:
        return ""
    artist = re.sub(r"\s*-\s*topic\s*$", "", artist, flags=re.IGNORECASE)
    artist = re.sub(r"vevo\s*$", "", artist, flags=re.IGNORECASE)
    artist = re.sub(r"\bofficial\b", "", artist, flags=re.IGNORECASE)
    return normalize_text(artist)


def split_artists(text: str) -> list[str]:
    """Split a combined artist string into individual normalized names."""
    if not text:
        return []
    parts = _ARTIST_SPLIT_RE.split(text)
    seen: list[str] = []
    for part in parts:
        norm = normalize_artist(part)
        if norm and norm not in seen:
            seen.append(norm)
    return seen


def similarity(a: str, b: str) -> float:
    a, b = a.strip(), b.strip()
    if not a or not b:
        return 0.0
    if a == b:
        return 1.0
    return SequenceMatcher(None, a, b).ratio()


def token_set_ratio(a: str, b: str) -> float:
    """Order-independent token overlap (like fuzzywuzzy's token_set_ratio)."""
    ta, tb = set(a.split()), set(b.split())
    if not ta or not tb:
        return 0.0
    inter = ta & tb
    union = ta | tb
    jaccard = len(inter) / len(union)
    seq = SequenceMatcher(None, " ".join(sorted(ta)), " ".join(sorted(tb))).ratio()
    return max(jaccard, seq)


def artist_similarity(spotify_artists: list[str], candidate_text: str) -> float:
    """Best match of any Spotify artist against the candidate's artist text."""
    cand_norm = normalize_artist(candidate_text)
    cand_tokens = set(cand_norm.split())
    best = 0.0
    for artist in spotify_artists:
        a = normalize_artist(artist)
        if not a:
            continue
        if a and a in cand_norm:
            best = max(best, 0.98)
        a_tokens = set(a.split())
        if a_tokens and a_tokens.issubset(cand_tokens):
            best = max(best, 0.95)
        best = max(best, similarity(a, cand_norm), token_set_ratio(a, cand_norm))
    return best


def contains_any(haystack: str, needles: list[str]) -> list[str]:
    low = haystack.lower()
    return [n for n in needles if n.lower() in low]
