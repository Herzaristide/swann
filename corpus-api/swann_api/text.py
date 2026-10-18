"""Traitements de texte sans base de données : recherche insensible à la casse
et aux accents avec positions exactes, recalage d'ancres, extraits."""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass

# Lettres que la décomposition Unicode ne ramène pas à l'ASCII mais que
# unaccent (Postgres) remplace : la recherche doit trouver « oeuvre » dans
# « œuvre », comme le préfiltre SQL.
SPECIAL = {"œ": "oe", "æ": "ae", "ø": "o", "ł": "l", "đ": "d", "ð": "d", "þ": "th", "ı": "i", "ß": "ss"}


def normalize_with_map(text: str) -> tuple[str, list[int]]:
    """Texte en minuscules sans accents, et pour chaque caractère normalisé la
    position du caractère d'origine dont il provient."""
    out: list[str] = []
    origin: list[int] = []
    for i, ch in enumerate(text):
        folded = ch.casefold()
        for c in folded:
            c = SPECIAL.get(c, c)
            for d in unicodedata.normalize("NFKD", c):
                if not unicodedata.combining(d):
                    out.append(d)
                    origin.append(i)
    return "".join(out), origin


def normalize(text: str) -> str:
    return normalize_with_map(text)[0]


_SPECIAL_TABLE = str.maketrans(SPECIAL)
_COMBINING = re.compile("[\u0300-\u036f\u0483-\u0489\u1ab0-\u1aff\u1dc0-\u1dff\u20d0-\u20ff\ufe20-\ufe2f]")


def quick_normalize(text: str) -> str:
    """Approximation rapide de normalize() (sans table de positions), pour
    préfiltrer un grand volume de texte avant le calcul exact."""
    return _COMBINING.sub("", unicodedata.normalize("NFKD", text.casefold().translate(_SPECIAL_TABLE)))


def find_all(text: str, query: str) -> list[tuple[int, int]]:
    """Occurrences de `query` dans `text` (casse et accents ignorés), en
    positions [début, fin[ du texte d'origine."""
    needle = normalize(query).strip()
    if not needle:
        return []
    hay, origin = normalize_with_map(text)
    spans = []
    start = hay.find(needle)
    while start != -1:
        end = start + len(needle)
        spans.append((origin[start], origin[end - 1] + 1))
        start = hay.find(needle, end)
    return spans


def snippet(text: str, start: int, end: int, context: int = 80) -> tuple[str, int]:
    """Extrait autour de [start, end[ ; renvoie (extrait, position du début de
    la correspondance dans l'extrait). Coupe sur des espaces quand c'est possible."""
    a = max(0, start - context)
    b = min(len(text), end + context)
    if a > 0:
        space = text.find(" ", a, start)
        a = space + 1 if space != -1 else a
    if b < len(text):
        space = text.rfind(" ", end, b)
        b = space if space != -1 else b
    prefix = "…" if a > 0 else ""
    suffix = "…" if b < len(text) else ""
    return prefix + text[a:b] + suffix, start - a + len(prefix)


def excerpt(text: str, limit: int, focus: tuple[int, int] | None = None) -> tuple[str, int]:
    """Texte réduit à `limit` caractères, centré sur `focus` s'il est donné ;
    renvoie (texte, décalage du début : à retrancher des positions)."""
    if len(text) <= limit:
        return text, 0
    center = (focus[0] + focus[1]) // 2 if focus else 0
    a = max(0, min(center - limit // 2, len(text) - limit))
    return text[a : a + limit], a


# --------------------------------------------------------------------- ancres
@dataclass
class Located:
    seq: int
    offset: int
    score: tuple[int, int]


def _common_suffix(a: str, b: str) -> int:
    n = 0
    while n < len(a) and n < len(b) and a[-1 - n] == b[-1 - n]:
        n += 1
    return n


def _common_prefix(a: str, b: str) -> int:
    n = 0
    while n < len(a) and n < len(b) and a[n] == b[n]:
        n += 1
    return n


def locate_quote(
    segments: list[tuple[int, str]], exact: str, prefix: str, suffix: str, near_seq: int
) -> Located | None:
    """Retrouve un extrait dans le texte (liste de (seq, texte)). Parmi les
    occurrences, retient celle dont le contexte (prefix / suffix) correspond le
    mieux, puis la plus proche de `near_seq`."""
    # Passe rapide sur le texte brut ; la comparaison normalisée (lente, en
    # Python) ne sert qu'en repli, si le texte a changé de graphie.
    exact_hits = [(seq, text) for seq, text in segments if exact in text]
    if not exact_hits:
        needle = quick_normalize(exact).strip()
        exact_hits = [(seq, text) for seq, text in segments if needle in quick_normalize(text)]
    return _best_match(exact_hits, exact, prefix, suffix, near_seq)


def _best_match(
    segments: list[tuple[int, str]], exact: str, prefix: str, suffix: str, near_seq: int
) -> Located | None:
    best: Located | None = None
    n_prefix, n_suffix = normalize(prefix), normalize(suffix)
    for seq, text in segments:
        for start, end in find_all(text, exact):
            before = normalize(text[max(0, start - len(prefix) - 20) : start])
            after = normalize(text[end : end + len(suffix) + 20])
            context = _common_suffix(before.rstrip(), n_prefix.rstrip()) + _common_prefix(
                after.lstrip(), n_suffix.lstrip()
            )
            score = (context, -abs(seq - near_seq))
            if best is None or score > best.score:
                best = Located(seq, start, score)
    return best
