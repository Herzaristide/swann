"""Analyse des titres de sections, en plusieurs langues.

"Chapitre III. La taverne"  -> kind=chapter, number=3, label="Chapitre III", title="La taverne"
"PREMIÈRE PARTIE"           -> kind=part,    number=1, label="PREMIÈRE PARTIE"
"XII"                       -> kind=None,    number=12, label="XII" (numéro seul)
"Préface"                   -> kind=preface
"""

from __future__ import annotations

import re
from dataclasses import dataclass

# Mots-clés par type de section (comparaison insensible à la casse).
KEYWORDS: dict[str, list[str]] = {
    "chapter": [
        "chapitre",
        "chap",
        "chapter",
        "kapitel",
        "capítulo",
        "capitulo",
        "capitolo",
        "capítol",
        "hoofdstuk",
        "rozdział",
        "глава",
        "kapitola",
        "luku",
        "fejezet",
        "κεφάλαιο",
        "bölüm",
    ],
    "part": ["partie", "part", "teil", "parte", "deel", "część", "часть", "část", "osa", "rész", "μέρος"],
    "book": [
        "livre",
        "book",
        "buch",
        "libro",
        "livro",
        "boek",
        "księga",
        "книга",
        "kniha",
        "kirja",
        "könyv",
        "βιβλίο",
    ],
    "volume": ["tome", "volume", "band", "tomo", "volumen", "том", "svazek"],
    "prologue": ["prologue", "prolog", "prólogo", "prologo", "пролог", "πρόλογος"],
    "epilogue": ["épilogue", "epilogue", "epilog", "epílogo", "epilogo", "эпилог", "επίλογος"],
    "preface": [
        "préface",
        "preface",
        "vorrede",
        "prefacio",
        "prefazione",
        "prefácio",
        "voorwoord",
        "przedmowa",
        "предисловие",
    ],
    "foreword": ["avant-propos", "foreword", "vorwort", "proemio", "предуведомление"],
    "introduction": [
        "introduction",
        "einleitung",
        "einführung",
        "introducción",
        "introduzione",
        "introdução",
        "inleiding",
        "wstęp",
        "введение",
    ],
    "afterword": ["postface", "afterword", "nachwort", "postfazione", "posfácio", "послесловие"],
    "appendix": ["annexe", "appendice", "appendix", "anhang", "apéndice", "anexo", "приложение"],
    "notes": ["notes", "endnotes", "footnotes", "anmerkungen", "notas", "примечания", "noten", "przypisy"],
    "glossary": ["glossaire", "glossary", "glossar", "glosario", "glossario", "словарь"],
    "dedication": ["dédicace", "dedication", "widmung", "dedicatoria", "dedica", "посвящение"],
}

# Titres de péritexte sans type de section dédié -> (kind, matter).
FRONT_TITLES = [
    "table des matières",
    "sommaire",
    "table",
    "contents",
    "table of contents",
    "inhalt",
    "inhaltsverzeichnis",
    "índice",
    "indice",
    "sumário",
    "содержание",
    "оглавление",
    "copyright",
    "title page",
    "page de titre",
    "titre",
    "avertissement",
    "note de l'éditeur",
    "note de l’éditeur",
    "publisher's note",
]
BACK_TITLES = [
    "transcriber's note",
    "transcriber’s note",
    "transcriber's notes",
    "note de transcription",
    "notes de transcription",
    "colophon",
    "achevé d'imprimer",
    "à propos de cette édition",
    "about this ebook",
    "about the author",
    "à propos de l'auteur",
    "imprint",
    "credits",
    "crédits",
    "bibliographie",
    "bibliography",
    "index",
    "acknowledgments",
    "acknowledgements",
    "remerciements",
]

ORDINALS: dict[str, int] = {}
for _words in [
    # fr
    "premier première un une|deuxième second seconde deux|troisième trois|quatrième quatre|"
    "cinquième cinq|sixième six|septième sept|huitième huit|neuvième neuf|dixième dix|"
    "onzième onze|douzième douze",
    # en
    "first one|second two|third three|fourth four|fifth five|sixth six|seventh seven|"
    "eighth eight|ninth nine|tenth ten|eleventh eleven|twelfth twelve",
    # de
    "erster erste erstes eins|zweiter zweite zweites zwei|dritter dritte drittes drei|"
    "vierter vierte viertes vier|fünfter fünfte fünftes fünf|sechster sechste sechs|"
    "siebter siebte sieben|achter achte acht|neunter neunte neun|zehnter zehnte zehn",
    # es / it / pt
    "primero primera primo prima primeiro|segundo segunda secondo seconda|"
    "tercero tercera terzo terza terceiro|cuarto cuarta quarto quarta|quinto quinta|"
    "sexto sexta sesto sesta|séptimo séptima settimo settima sétimo|octavo octava ottavo oitavo|"
    "noveno novena nono nona|décimo décima decimo decima",
    # ru
    "первая первый первое первая|вторая второй второе|третья третий третье|"
    "четвёртая четвертая четвёртый четвертый|пятая пятый|шестая шестой|седьмая седьмой|"
    "восьмая восьмой|девятая девятый|десятая десятый",
]:
    for _n, _group in enumerate(_words.split("|"), start=1):
        for _w in _group.split():
            ORDINALS[_w] = _n

ROMAN_RE = re.compile(r"^M{0,3}(CM|CD|D?C{0,3})(XC|XL|L?X{0,3})(IX|IV|V?I{0,3})$", re.I)
SEP = r"[\s.:;,—–\-]*"


def roman_to_int(s: str) -> int | None:
    if not s or not ROMAN_RE.match(s):
        return None
    values = {"i": 1, "v": 5, "x": 10, "l": 50, "c": 100, "d": 500, "m": 1000}
    total, prev = 0, 0
    for ch in reversed(s.lower()):
        v = values[ch]
        total = total - v if v < prev else total + v
        prev = max(prev, v)
    return total


def parse_number(token: str) -> int | None:
    token = token.strip(".:").lower()
    if token.isdigit():
        return int(token)
    m = re.match(r"^(\d+)(er|re|e|ème|eme|st|nd|rd|th|\.)?$", token)
    if m:
        return int(m[1])
    if token in ORDINALS:
        return ORDINALS[token]
    return roman_to_int(token)


_KW_TO_KIND = {kw: kind for kind, kws in KEYWORDS.items() for kw in kws}
_KW_ALT = "|".join(sorted((re.escape(k) for k in _KW_TO_KIND), key=len, reverse=True))
_NUM = r"(?:\d+(?:er|re|e|ème|eme|st|nd|rd|th)?|[ivxlcdm]+|[^\W\d_]+)"
# "Chapitre III. Titre", "Chapter the First"
KW_FIRST = re.compile(
    rf"^(?P<kw>{_KW_ALT})\.?(?:\s+(?:the\s+)?(?P<num>{_NUM})\b)?(?P<sep>{SEP})(?P<rest>.*)$",
    re.I | re.S,
)
# "Première partie", "Livre premier" est couvert par KW_FIRST
NUM_FIRST = re.compile(
    rf"^(?P<num>[^\W\d_]+|\d+(?:er|re|e|ème)?)\s+(?P<kw>{_KW_ALT})\b(?P<sep>{SEP})(?P<rest>.*)$",
    re.I | re.S,
)
# Numéro seul : "XII", "12", "XII. Titre" (séparateur obligatoire avant un titre)
NUM_ONLY = re.compile(r"^(?P<num>\d+|[ivxlcdm]+)(?:(?P<sep>\s*[.:—–\-]\s*|\s*$)(?P<rest>.*))$", re.I | re.S)


@dataclass
class HeadingInfo:
    kind: str | None = None  # type de section, None si inconnu
    number: int | None = None
    label: str | None = None
    title: str | None = None
    matter: str | None = None  # "front" / "back" si le titre l'indique


def _clean(s: str | None) -> str | None:
    if s is None:
        return None
    s = s.strip(" .:—–-\n")
    return s or None


def parse_heading(text: str) -> HeadingInfo:
    text = " ".join(text.split())
    if not text:
        return HeadingInfo()
    lowered = text.lower().rstrip(".")
    if lowered in FRONT_TITLES:
        return HeadingInfo(kind="other", title=text, matter="front")
    if lowered in BACK_TITLES:
        return HeadingInfo(kind="other", title=text, matter="back")

    m = KW_FIRST.match(text)
    if m:
        kind = _KW_TO_KIND[m["kw"].lower()]
        num_token = m["num"]
        number = parse_number(num_token) if num_token else None
        if num_token and number is None:
            # "Chapter Wolves" : le mot suivant n'est pas un numéro
            rest = text[m.end("kw") :]
            return HeadingInfo(kind=kind, label=m["kw"], title=_clean(rest))
        end = m.end("num") if num_token else m.end("kw")
        return HeadingInfo(kind=kind, number=number, label=_clean(text[:end]), title=_clean(m["rest"]))

    m = NUM_FIRST.match(text)
    if m and parse_number(m["num"]) is not None:
        return HeadingInfo(
            kind=_KW_TO_KIND[m["kw"].lower()],
            number=parse_number(m["num"]),
            label=_clean(text[: m.end("kw")]),
            title=_clean(m["rest"]),
        )

    m = NUM_ONLY.match(text)
    if m and (number := parse_number(m["num"])) is not None:
        return HeadingInfo(number=number, label=m["num"], title=_clean(m["rest"]))

    return HeadingInfo(title=text)


def merge_heading_texts(texts: list[str]) -> HeadingInfo:
    """Combine plusieurs titres consécutifs (ex : <h2>I</h2><p>Le titre</p>)."""
    if not texts:
        return HeadingInfo()
    info = parse_heading(texts[0])
    extra = [t for t in texts[1:] if t.strip()]
    if extra:
        rest = " — ".join(extra)
        if info.label and not info.title:
            info.title = rest
        elif info.title and info.kind is None and info.label is None:
            # Premier titre sans numéro : peut-être "Partie" puis "Chapitre"
            second = parse_heading(extra[0])
            if second.kind or second.number is not None:
                info = HeadingInfo(
                    second.kind,
                    second.number,
                    second.label,
                    _clean(f"{info.title} — {second.title}" if second.title else info.title),
                )
            else:
                info.title = f"{info.title} — {rest}"
        else:
            info.title = f"{info.title} — {rest}" if info.title else rest
    return info
