"""Normalisation par source : chaque bibliothèque numérique a ses
conventions (rôles, noms, titres, licence). Sortie : un `Draft` par EPUB."""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from urllib.parse import unquote

from swann_ingest.identify import clean_person_name, edition_language, ratio

TRANSLATOR_ROLES = {"trl", "translator", "tr"}
AUTHOR_ROLES = {"aut", "author", "cre"}

# Auteurs qui n'en sont pas : folklore, anonymes, recueils collectifs.
ANONYMOUS_RE = re.compile(
    r"народн|неизвест|аноним|^anonym|^an[oó]nim|^inconnu|^nieznan|^unknown|^unbekannt|^ludow|"
    r"^autor (nieznany|desconocido)|^auteur inconnu",
    re.I,
)
COLLECTIVE_RE = re.compile(r"^(various|varios|divers|collectif|różni|разные)\b", re.I)
# Auteurs institutionnels (textes officiels, lois…) : hors du périmètre littéraire.
INSTITUTION_RE = re.compile(
    r"министерств|служба|трибунал|директива|указ|комиссар|комитет|правительств|федераци|"
    r"ссср|рсфср|верховн|совет\b|госуд|передовая|правда»|ministerstwo|sejm|rząd|"
    r"^fundaci[óo]n\b|^fundacja\b|^instituto\b|^institut\b|^universi|^academia\b|^sociedad\b|^société\b",
    re.I,
)
OFFICIAL_TITLE_RE = re.compile(
    r"^(об?|о)\s+(утверждении|внесении|порядке|мерах|введении)|постановлени|приказы?\b|указы?\b|законодательств|"
    r"федеральн\w+ закон|^закон\b|^декрет|^конституци|^декларац|^кодекс|^резолюци|"
    r"^ustawa\b|^rozporządzenie",
    re.I,
)
VOLUME_RE = re.compile(
    r"[\s,(]*\b(?:vol\.?|volume|tome|t\.|tom|том|band|bd\.|livre|tomo|część|partie)\s*"
    r"(?P<n>[0-9]+|[IVXLC]+)\b(?:\s*/\s*[0-9]+)?\)?",
    re.I,
)
TRAILING_NUMBER_RE = re.compile(r"\s+(?P<n>[0-9]{1,2})$")
DATES_RE = re.compile(r"\(\s*(?P<b>\d{3,4}|\?)?\s*[-–—]\s*(?P<d>\d{3,4}|\?)?\s*\)")
YEAR_RE = re.compile(r"\b(1[4-9]\d\d|20[0-2]\d)\b")

PUBLISHERS = {
    "wikisource": "Wikisource",
    "wolnelektury": "Fundacja Wolne Lektury",
    "gutenberg": "Project Gutenberg",
    "textos": "textos.info",
    "beq": "La Bibliothèque électronique du Québec",
}


@dataclass
class Name:
    name: str
    birth: int | None = None
    death: int | None = None
    raw: str = ""


@dataclass
class Draft:
    file: str
    sha: str
    source: str  # wikisource, wolnelektury, gutenberg, textos, beq, autre
    source_url: str | None
    title: str
    language: str | None  # langue déclarée (normalisée)
    authors: list[Name]
    translators: list[Name]
    anonymous: bool = False
    collective: bool = False
    publisher: str | None = None
    year: int | None = None
    # Licence déclarée par la source : free (licence libre), pd (domaine public
    # déclaré), pd-us (domaine public aux États-Unis), copyrighted, unknown.
    license: str = "unknown"
    wikisource: tuple[str, str] | None = None  # (langue, titre de page)
    gutenberg_id: str | None = None
    volume: str | None = None
    flags: list[str] = field(default_factory=list)


# ------------------------------------------------------------------ noms
def split_name(raw: str) -> Name:
    """« автор Константин Бальмонт (1867—1942) » → Name("Константин Бальмонт", 1867, 1942)."""
    text = " ".join(raw.split())
    text = re.sub(r"^(автор[ыа]?:?|переводчик[и]?:?|перевод:?|пер\.)\s+", "", text, flags=re.I)
    # Titres de noblesse et de civilité : « graf Leo Tolstoy », « граф Лев Толстой »
    text = re.sub(
        r"^(graf|gräfin|hrabia|hrabina|comte|comtesse|count|countess|граф|графиня|князь|княгиня|"
        r"sir|lord|lady|baron|baronne|vicomte|marquis|marquise|duc|duchesse)\s+",
        "",
        text,
        flags=re.I,
    )
    birth = death = None
    if m := DATES_RE.search(text):
        birth = int(m["b"]) if m["b"] and m["b"] != "?" else None
        death = int(m["d"]) if m["d"] and m["d"] != "?" else None
        text = (text[: m.start()] + text[m.end() :]).strip()
    # Dates d'une autre forme (« ок. 87 до н. э.— ок. 54 до н. э. »), parenthèse parfois coupée
    text = re.sub(r"\s*\([^)]*\d[^)]*(\)|$)", "", text).strip()
    # Appels de note de Wikisource (« [1] », « [ВТ 1] ») et précisions entre
    # parenthèses (« (ojciec) », « (Clarín) ») : le nom d'usage vient de Wikidata.
    text = re.sub(r"\s*\[[^\]]*\]|\s*\([^)]*\)", "", text).strip()
    text = clean_person_name(text).strip(" ,;")
    if not re.search(r"(^|\s)\w\.$", text):  # « К. Р. » garde son point
        text = text.rstrip(".")
    return Name(text, birth, death, raw)


def _dedupe(names: list[Name]) -> list[Name]:
    seen, out = set(), []
    for n in names:
        key = n.name.casefold()
        if n.name and key not in seen:
            seen.add(key)
            out.append(n)
    return out


def fold(text: str) -> str:
    """Comparaison sans casse, accents ni ponctuation."""
    text = unicodedata.normalize("NFKD", text)
    text = "".join(c for c in text if not unicodedata.combining(c)).casefold()
    return re.sub(r"[\W_]+", "", text)


def filename_author(stem: str, title: str) -> str | None:
    """Les noms de fichier suivent « Auteur-Titre-langue[-(n)] » : l'auteur est
    ce qui précède le titre."""
    stem = re.sub(r"-\(\d+\)$", "", stem)
    stem = re.sub(r"-[A-Za-z]{2,3}$", "", stem)
    tokens = stem.split("-")
    target = fold(title)
    if not target:
        return None
    for k in range(1, len(tokens)):
        tail = fold("".join(tokens[k:]))
        if tail == target or (len(tail) >= 8 and target.startswith(tail)):
            return " ".join(tokens[:k]).strip() or None
    return None


# --------------------------------------------------------------- sources
def _source(opf: dict) -> tuple[str, str | None]:
    urls = [u for u in opf["identifiers"] + opf["sources"] if re.match(r"https?://", u)]
    for u in urls:
        if "wikisource.org" in u:
            return "wikisource", u
        if "wolnelektury.pl" in u:
            return "wolnelektury", u
        if "gutenberg.org" in u:
            return "gutenberg", u
        if "textos.info" in u or "textos.com" in u:
            return "textos", u
    if any("Québec" in p for p in opf["publishers"]):
        return "beq", None
    return "autre", urls[0] if urls else None


def normalize(file: str, record: dict) -> Draft:
    opf = record["opf"]
    source, url = _source(opf)
    title = " ".join((opf["titles"] or [""])[0].split()).rstrip("/ ").strip()
    languages = [c for x in opf["languages"] if x.lower() not in ("und", "mul") if (c := edition_language(x))]
    authors: list[Name] = []
    translators: list[Name] = []
    for p in opf["people"]:
        role = (p["role"] or "").lower()
        # « авторы: А, Б » : plusieurs personnes dans un seul champ
        raw = p["name"]
        parts = re.split(r"\s*[,;]\s*", raw) if re.match(r"^(авторы|переводчики):", raw, re.I) else [raw]
        if parts != [raw]:
            parts[0] = re.sub(r"^\w+:\s*", "", parts[0])
        for part in parts:
            if role in TRANSLATOR_ROLES:
                translators.append(split_name(part))
            elif role in AUTHOR_ROLES or (p["tag"] == "creator" and not role):
                authors.append(split_name(part))
    draft = Draft(
        file=file,
        sha=record["sha"],
        source=source,
        source_url=url,
        title=title,
        language=languages[0] if languages else None,
        authors=_dedupe(authors),
        translators=_dedupe(translators),
        publisher=PUBLISHERS.get(source),
    )
    if len(languages) > 1:
        draft.flags.append(f"plusieurs langues déclarées : {', '.join(languages)}")

    if source == "wikisource":
        draft.license = "free"
        m = re.match(r"https?://([a-z-]+)\.wikisource\.org/wiki/(.+)$", url or "")
        if m:
            draft.wikisource = (m[1], unquote(m[2]).replace("_", " "))
    elif source == "wolnelektury":
        last = opf.get("last_page") or ""
        draft.license = "pd" if "domenie publicznej" in last else "free"
        if m := re.search(r"Tekst opracowany na podstawie:(.{0,300}?)(?:Wydawca|Opracowanie|$)", last):
            years = YEAR_RE.findall(m[1])
            draft.year = int(years[-1]) if years else None
        if m := re.search(r"Tłumaczenie:\s*([^.]{3,80})\.", last):
            for n in re.split(r",| i ", m[1]):
                if n.strip():
                    draft.translators.append(split_name(n))
            draft.translators = _dedupe(draft.translators)
    elif source == "gutenberg":
        draft.license = "copyrighted" if any("Copyrighted" in r for r in opf["rights"]) else "pd-us"
        if m := re.search(r"gutenberg\.org/(?:ebooks/|files/)?(\d+)", " ".join(opf["identifiers"])):
            draft.gutenberg_id = m[1]
        # Gutenberg sépare titre et sous-titre par un saut de ligne, lu « / ».
        draft.title = re.sub(r"\s*/\s*", " : ", draft.title).strip(" :")

    # Auteur absent, inconnu ou confondu avec le titre : le nom de fichier sait.
    stem = file.rsplit(".", 1)[0]
    if not draft.authors or any(
        fold(a.name) == fold(title) or ANONYMOUS_RE.search(a.name) for a in draft.authors
    ):
        from_file = filename_author(stem, title)
        if (
            from_file
            and not ANONYMOUS_RE.search(from_file)
            and from_file.lower() != "anonyme"
            and source in ("autre", "beq", "gutenberg", "textos")
        ):
            draft.authors = [Name(from_file, raw=from_file)]
            draft.flags.append(f"auteur repris du nom de fichier : {from_file}")

    # Titre recopié dans le champ auteur (« Les trois mousquetaires 2 ») : pas d'auteur
    title_fold = fold(VOLUME_RE.sub("", title))
    confused = [
        a for a in draft.authors if title_fold and ratio(fold(a.name).rstrip("0123456789"), title_fold) >= 0.8
    ]
    if confused:
        draft.authors = [a for a in draft.authors if a not in confused]
        draft.flags.append(f"auteur confondu avec le titre : {confused[0].name}")

    if any(COLLECTIVE_RE.search(a.name) for a in draft.authors):
        draft.collective = True
        draft.authors = [a for a in draft.authors if not COLLECTIVE_RE.search(a.name)]
    if any(ANONYMOUS_RE.search(a.name) for a in draft.authors):
        draft.anonymous = not [a for a in draft.authors if not ANONYMOUS_RE.search(a.name)]
        draft.authors = [a for a in draft.authors if not ANONYMOUS_RE.search(a.name)]
    if not draft.authors and not draft.collective:
        draft.anonymous = True

    if (m := VOLUME_RE.search(draft.title)) or (
        source in ("autre", "beq") and (m := TRAILING_NUMBER_RE.search(draft.title))
    ):
        draft.volume = m["n"]
    return draft


def non_literary(draft: Draft) -> str | None:
    """Motif d'exclusion d'un texte officiel (lois, décrets, directives…)."""
    for a in draft.authors:
        if INSTITUTION_RE.search(a.raw or a.name):
            return f"auteur institutionnel : {a.name}"
    # Un texte signé (projet de décret de Lénine, « Резолюция » de Derjavine)
    # appartient à l'œuvre de son auteur : seul l'anonyme est jugé au titre.
    if not draft.authors and OFFICIAL_TITLE_RE.search(draft.title):
        return f"titre de texte officiel : {draft.title}"
    return None
