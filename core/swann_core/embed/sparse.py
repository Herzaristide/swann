"""Vecteurs creux (mots exacts) : BM25, calculé sur CPU.

Chaque mot est mis en minuscules puis ramené à sa racine (Snowball, selon la
langue de l'édition), et identifié par un hachage stable sur 32 bits. Le
vecteur d'un document porte la partie "fréquence" de BM25 ; la partie IDF
(rareté du mot dans tout le corpus) est calculée par Qdrant
(sparse vector avec modifier=IDF).
"""

from __future__ import annotations

import hashlib
import re
from collections import Counter
from functools import lru_cache

import snowballstemmer

NAME = "swann-bm25-v1"
K1 = 1.2
B = 0.75
AVG_DOC_LEN = 256  # longueur moyenne approximative d'un chunk, en mots

WORD_RE = re.compile(r"\w+", re.UNICODE)

SNOWBALL = {
    "ar": "arabic",
    "hy": "armenian",
    "eu": "basque",
    "ca": "catalan",
    "cs": "czech",
    "da": "danish",
    "nl": "dutch",
    "en": "english",
    "eo": "esperanto",
    "et": "estonian",
    "fi": "finnish",
    "fr": "french",
    "de": "german",
    "el": "greek",
    "hi": "hindi",
    "hu": "hungarian",
    "id": "indonesian",
    "ga": "irish",
    "it": "italian",
    "lt": "lithuanian",
    "ne": "nepali",
    "no": "norwegian",
    "nb": "norwegian",
    "nn": "norwegian",
    "fa": "persian",
    "pl": "polish",
    "pt": "portuguese",
    "ro": "romanian",
    "ru": "russian",
    "sr": "serbian",
    "es": "spanish",
    "sv": "swedish",
    "ta": "tamil",
    "tr": "turkish",
    "yi": "yiddish",
}


@lru_cache(maxsize=64)
def _stemmer(language: str | None):
    name = SNOWBALL.get((language or "").split("-")[0].lower())
    return snowballstemmer.stemmer(name) if name else None


def _token_id(token: str) -> int:
    return int.from_bytes(hashlib.blake2b(token.encode(), digest_size=4).digest(), "little")


def terms(text: str, language: str | None) -> list[str]:
    words = WORD_RE.findall(text.casefold())
    stemmer = _stemmer(language)
    return stemmer.stemWords(words) if stemmer else words


def encode_document(text: str, language: str | None) -> tuple[list[int], list[float]]:
    tokens = terms(text, language)
    counts = Counter(_token_id(t) for t in tokens)
    norm = K1 * (1 - B + B * len(tokens) / AVG_DOC_LEN)
    indices = list(counts)
    values = [tf * (K1 + 1) / (tf + norm) for tf in counts.values()]
    return indices, values


def encode_query(text: str, language: str | None) -> tuple[list[int], list[float]]:
    ids = sorted({_token_id(t) for t in terms(text, language)})
    return ids, [1.0] * len(ids)
