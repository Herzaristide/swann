"""Vérification de la langue d'une édition (lingua)."""

import re
from functools import lru_cache

from lingua import LanguageDetectorBuilder

# Russe d'avant 1918 (ѣ, і, ѳ, ѵ, ъ final) : sans cette normalisation, la
# détection y voit du mongol ou du bulgare. Sert à la détection seulement.
PRE_REFORM = str.maketrans({"ѣ": "е", "Ѣ": "Е", "і": "и", "І": "И", "ѳ": "ф", "Ѳ": "Ф", "ѵ": "и", "Ѵ": "И"})
FINAL_HARD_SIGN = re.compile(r"(?<=[бвгджзклмнпрстфхцчшщ])ъ\b", re.I)


@lru_cache(maxsize=1)
def _detector():
    # Précision haute : la basse confond le russe court avec le bulgare ou le serbe.
    return LanguageDetectorBuilder.from_all_languages().build()


def detect_language(text: str) -> str | None:
    """Code ISO 639-1 (ex : "fr") de la langue majoritaire du texte, ou None."""
    if len(text.strip()) < 50:
        return None
    text = FINAL_HARD_SIGN.sub("", text.translate(PRE_REFORM))
    language = _detector().detect_language_of(text)
    if language is None:
        return None
    return language.iso_code_639_1.name.lower()


# En deçà, la détection n'est pas fiable (chansons, poèmes courts) : un
# désaccord avec la fiche n'est qu'un avertissement.
LANGUAGE_CHECK_MIN_CHARS = 1500


def same_language(declared: str, detected: str | None) -> bool:
    return detected is None or declared.split("-")[0].lower() == detected


def detect_majority(paragraphs: list[str]) -> str | None:
    """Langue majoritaire sur trois extraits (début, milieu, fin du corps) :
    un seul extrait plein de noms propres trompe parfois la détection."""
    from collections import Counter

    if not paragraphs:
        return None
    votes = []
    for frac in (0.25, 0.5, 0.75):
        i = int(len(paragraphs) * frac)
        sample, j = [], i
        while j < len(paragraphs) and sum(map(len, sample)) < 3000:
            sample.append(paragraphs[j])
            j += 1
        if lang := detect_language("\n".join(sample)):
            votes.append(lang)
    return Counter(votes).most_common(1)[0][0] if votes else None
