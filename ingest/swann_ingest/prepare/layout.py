"""Droits, noms de dossiers et écriture de books/ (work.toml, persons.toml,
liens durs vers les EPUB)."""

from __future__ import annotations

import json
import os
import re
import shutil
import unicodedata
from datetime import date
from pathlib import Path

from swann_ingest.identify import _translit
from swann_ingest.prepare.persons import Person
from swann_ingest.prepare.works import Edition, Work

# Domaine public dans l'UE : auteur et traducteur morts depuis plus de 70 ans.
PD_CUTOFF = date.today().year - 71
EXTRA = str.maketrans({"ł": "l", "Ł": "L", "ß": "ss", "æ": "ae", "œ": "oe", "ø": "o", "đ": "d", "ı": "i"})


def slugify(text: str, limit: int = 60) -> str:
    text = unicodedata.normalize("NFKD", text.translate(EXTRA))
    text = "".join(c for c in text if not unicodedata.combining(c)).lower()
    text = _translit(text)
    text = re.sub(r"[^a-z0-9]+", "-", text).strip("-")
    return text[:limit].rstrip("-")


def toml_str(value: str) -> str:
    # Une chaîne JSON (sans échappement \/) est une chaîne de base TOML valide.
    return json.dumps(value, ensure_ascii=False)


def toml_list(values: list[str]) -> str:
    return "[" + ", ".join(toml_str(v) for v in values) + "]"


# ------------------------------------------------------------------ droits
def access(e: Edition, w: Work, persons: dict[str, Person]) -> tuple[str, str]:
    """« open » seulement quand c'est sûr ; sinon « excerpt » (extraits
    courts dans la recherche), avec le motif."""
    lic = e.draft.license
    if lic == "free":
        return "open", "licence libre déclarée par la source"
    if lic == "pd":
        return "open", "domaine public déclaré par la source"
    if lic == "copyrighted":
        return "excerpt", "sous droits (Project Gutenberg : « Copyrighted »)"
    translated = bool(w.original_language) and e.language.split("-")[0] != w.original_language
    if translated and not e.translator_keys:
        return "excerpt", "traduction sans traducteur nommé : droits du traducteur inconnus"
    people = [persons[k] for k in e.author_keys + e.translator_keys if k in persons]
    if not people:
        if e.draft.license == "pd-us" or e.draft.source in ("beq", "autre"):
            return "open", "anonyme, publié par une bibliothèque du domaine public"
        return "excerpt", "anonyme : date du texte inconnue"
    unknown = [p.display for p in people if not p.death]
    if unknown:
        return "excerpt", f"date de mort inconnue : {', '.join(unknown)}"
    late = [f"{p.display} († {p.death})" for p in people if p.death and p.death > PD_CUTOFF]
    if late:
        return "excerpt", f"mort après {PD_CUTOFF} : {', '.join(late)}"
    return "open", f"auteurs et traducteurs morts avant {PD_CUTOFF + 1}"


# ------------------------------------------------------------------ dossiers
class Namer:
    """Dossiers books/<auteur>/<œuvre>/ et fichiers <langue>[--<variante>].epub, uniques."""

    def __init__(self) -> None:
        self.authors: dict[str, str] = {}  # slug -> identité (QID ou clé)
        self.works: dict[str, set[str]] = {}  # slug d'auteur -> slugs d'œuvres

    def author_dir(self, w: Work, persons: dict[str, Person]) -> str:
        if not w.author_keys:
            return "anonyme"
        p = persons[w.author_keys[0]]
        identity = p.qid or p.display
        base = slugify(p.sort_name or p.display) or "auteur"
        slug, i = base, 2
        while self.authors.setdefault(slug, identity) != identity:
            slug, i = f"{base}-{i}", i + 1
        return slug

    def work_dir(self, author: str, w: Work) -> str:
        taken = self.works.setdefault(author, set())
        base = slugify(w.title) or (w.qid or "oeuvre").lower()
        slug, i = base, 2
        while slug in taken:
            slug, i = f"{base}-{i}", i + 1
        taken.add(slug)
        return slug


def edition_files(w: Work, persons: dict[str, Person]) -> dict[int, str]:
    """id(édition) -> nom de fichier ; l'original (ou la plus longue) garde <langue>.epub."""
    names: dict[int, str] = {}
    used: set[str] = set()
    live = [e for e in w.editions if e.duplicate_of is None]
    for e in sorted(live, key=lambda x: (not x.original, -x.body_chars)):
        lang = e.language
        name = f"{lang}.epub"
        if name in used:
            variant = (
                slugify(persons[e.translator_keys[0]].display, 30)
                if e.translator_keys and e.translator_keys[0] in persons
                else ""
            ) or e.draft.source
            name, i = f"{lang}--{variant}.epub", 2
            while name in used:
                name, i = f"{lang}--{variant}-{i}.epub", i + 1
        used.add(name)
        names[id(e)] = name
    return names


def link(src: Path, dest: Path) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.link(src, dest)
    except OSError:
        shutil.copy2(src, dest)  # autre système de fichiers


# ---------------------------------------------------------------- écriture
def work_toml(
    w: Work, files: dict[int, str], persons: dict[str, Person], rights: dict[int, tuple[str, str]]
) -> str:
    lines = ["# Généré par `swann prepare` : à relire, corriger ici puis relancer `swann extract`.", "[work]"]
    lines.append(f"title = {toml_str(w.title)}")
    if w.original_language:
        lines.append(f"original_language = {toml_str(w.original_language)}")
    if w.year:
        lines.append(f"first_published_year = {w.year}")
    authors = [persons[k].display for k in w.author_keys if k in persons] or ["Anonyme"]
    lines.append(f"authors = {toml_list(authors)}")
    if w.qid:
        lines.append(f"wikidata = {toml_str(w.qid)}")
    for note in w.notes:
        lines.append(f"# à vérifier : {note}")
    for e in sorted((e for e in w.editions if e.duplicate_of is None), key=lambda x: files[id(x)]):
        d = e.draft
        lines += ["", "[[editions]]", f"# source : {d.file}" + (f" — {d.source_url}" if d.source_url else "")]
        level, why = rights[id(e)]
        lines.append(f"# droits : {why}")
        if e.how:
            lines.append(f"# œuvre : {e.how}")
        lines.append(f"file = {toml_str(files[id(e)])}")
        lines.append(f"language = {toml_str(e.language)}")
        if e.original:
            lines.append("original = true")
        if d.title and d.title != w.title:
            lines.append(f"title = {toml_str(d.title)}")
        translators = [persons[k].display for k in e.translator_keys if k in persons]
        if translators:
            lines.append(f"translators = {toml_list(translators)}")
        if d.publisher:
            lines.append(f"publisher = {toml_str(d.publisher)}")
        if d.year:
            lines.append(f"year = {d.year}")
        lines.append(f"access = {toml_str(level)}")
    return "\n".join(lines) + "\n"


def persons_toml(persons: dict[str, Person], used: set[str]) -> str:
    """Une entrée par nom d'usage : identité Wikidata, dates, graphies."""
    merged: dict[str, list[Person]] = {}
    for p in persons.values():
        if p.key in used:
            merged.setdefault(p.display, []).append(p)
    lines = [
        "# Personnes citées dans les work.toml (généré par `swann prepare`).",
        "# Lu par `swann extract` : identité Wikidata, dates et graphies de chaque nom.",
    ]
    for display in sorted(merged):
        group = merged[display]
        lines += ["", f"[{toml_str(display)}]"]
        qid = next((p.qid for p in group if p.qid), None)
        if qid:
            lines.append(f"wikidata = {toml_str(qid)}")
        if birth := next((p.birth for p in group if p.birth), None):
            lines.append(f"birth_year = {birth}")
        if death := next((p.death for p in group if p.death), None):
            lines.append(f"death_year = {death}")
        if sort := group[0].sort_name:
            lines.append(f"sort_name = {toml_str(sort)}")
        names: dict[str, set[str]] = {}
        for p in group:
            for lang, values in p.variants.items():
                names.setdefault(lang, set()).update(values)
            for n in p.names:
                for lang in p.languages or {"und"}:
                    names.setdefault(lang, set()).add(n)
        for lang in sorted(names):
            values = sorted(v for v in names[lang] if v != display)
            if values:
                lines.append(f"names.{lang} = {toml_list(values)}")
    return "\n".join(lines) + "\n"
