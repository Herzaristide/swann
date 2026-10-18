"""Catalogue : le dossier books/ et les fiches work.toml.

Convention :
    books/<auteur>/<œuvre>/work.toml
    books/<auteur>/<œuvre>/<langue>.epub
    books/<auteur>/<œuvre>/<langue>--<traducteur>.epub
    books/persons.toml                (facultatif : identité des personnes citées)
"""

from __future__ import annotations

import re
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

LANGUAGE_RE = re.compile(r"^[a-z]{2,3}(-[A-Za-z0-9]{2,8})*$")
SLUG_PART_RE = re.compile(r"^[a-z0-9]+(-[a-z0-9]+)*$")
FILE_RE = re.compile(r"^(?P<lang>[a-z]{2,3}(-[A-Za-z0-9]{2,8})*)(--(?P<variant>[a-z0-9-]+))?\.epub$")
WIKIDATA_RE = re.compile(r"^Q[0-9]+$")

# Droits sur le texte d'une édition (editions.access) : "open" = texte intégral
# et EPUB servis par l'API, "excerpt" = extraits dans la recherche,
# "restricted" = métadonnées seulement.
Access = Literal["open", "excerpt", "restricted"]


def _check_language(value: str | None) -> str | None:
    if value is not None and not LANGUAGE_RE.match(value):
        raise ValueError(f"code de langue invalide : {value!r} (attendu : fr, en, ru, pt-BR...)")
    return value


def _check_wikidata(value: str | None) -> str | None:
    if value is not None and not WIKIDATA_RE.match(value):
        raise ValueError(f"identifiant Wikidata invalide : {value!r} (attendu : Q12345)")
    return value


class WorkFields(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: str
    original_language: str | None = None
    first_published_year: int | None = None
    authors: list[str] = Field(min_length=1)
    movements: list[str] = []
    wikidata: str | None = None

    _lang = field_validator("original_language")(_check_language)
    _wd = field_validator("wikidata")(_check_wikidata)


class EditionFields(BaseModel):
    model_config = ConfigDict(extra="forbid")

    file: str
    language: str
    original: bool = False
    title: str | None = None
    translators: list[str] = []
    publisher: str | None = None
    year: int | None = None
    access: Access = "restricted"

    _lang = field_validator("language")(_check_language)


class PersonSpec(BaseModel):
    """Entrée de books/persons.toml, clé = nom tel qu'écrit dans les work.toml."""

    model_config = ConfigDict(extra="forbid")

    wikidata: str | None = None
    birth_year: int | None = None
    death_year: int | None = None
    sort_name: str | None = None
    names: dict[str, list[str]] = {}  # langue -> graphies

    _wd = field_validator("wikidata")(_check_wikidata)


class Manifest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    work: WorkFields
    editions: list[EditionFields] = Field(min_length=1)


@dataclass
class EditionSpec:
    path: Path
    source_file: str  # chemin relatif à books/
    language: str
    original: bool
    title: str | None
    translators: list[str]
    publisher: str | None
    year: int | None
    access: Access = "restricted"


@dataclass
class WorkSpec:
    slug: str  # "<auteur>/<œuvre>"
    dir: Path
    title: str
    original_language: str | None
    first_published_year: int | None
    authors: list[str]
    movements: list[str]
    wikidata: str | None
    editions: list[EditionSpec] = field(default_factory=list)


@dataclass
class CatalogIssue:
    path: Path
    message: str
    fatal: bool = True


@dataclass
class Catalog:
    works: list[WorkSpec]
    issues: list[CatalogIssue]
    persons: dict[str, PersonSpec] = field(default_factory=dict)

    @property
    def errors(self) -> list[CatalogIssue]:
        return [i for i in self.issues if i.fatal]


def load_work(work_dir: Path, books_dir: Path) -> tuple[WorkSpec | None, list[CatalogIssue]]:
    issues: list[CatalogIssue] = []
    slug = work_dir.relative_to(books_dir).as_posix()
    for part in slug.split("/"):
        if not SLUG_PART_RE.match(part):
            issues.append(
                CatalogIssue(
                    work_dir,
                    f"nom de dossier {part!r} : minuscules, chiffres et tirets uniquement",
                )
            )

    manifest_path = work_dir / "work.toml"
    if not manifest_path.is_file():
        issues.append(CatalogIssue(work_dir, "work.toml manquant"))
        return None, issues
    try:
        manifest = Manifest.model_validate(tomllib.loads(manifest_path.read_text("utf-8")))
    except tomllib.TOMLDecodeError as e:
        issues.append(CatalogIssue(manifest_path, f"TOML invalide : {e}"))
        return None, issues
    except ValidationError as e:
        for err in e.errors():
            loc = ".".join(str(p) for p in err["loc"])
            issues.append(CatalogIssue(manifest_path, f"{loc} : {err['msg']}"))
        return None, issues

    work = WorkSpec(
        slug=slug,
        dir=work_dir,
        title=manifest.work.title,
        original_language=manifest.work.original_language,
        first_published_year=manifest.work.first_published_year,
        authors=manifest.work.authors,
        movements=manifest.work.movements,
        wikidata=manifest.work.wikidata,
    )

    declared: set[str] = set()
    for ed in manifest.editions:
        path = work_dir / ed.file
        if ed.file in declared:
            issues.append(CatalogIssue(manifest_path, f"{ed.file} déclaré deux fois"))
            continue
        declared.add(ed.file)
        if not path.is_file():
            issues.append(CatalogIssue(manifest_path, f"{ed.file} déclaré mais absent"))
            continue
        m = FILE_RE.match(ed.file)
        if not m:
            issues.append(
                CatalogIssue(
                    path,
                    "nom de fichier attendu : <langue>.epub ou <langue>--<variante>.epub",
                    fatal=False,
                )
            )
        elif m["lang"] != ed.language:
            issues.append(
                CatalogIssue(
                    path, f"le nom de fichier indique {m['lang']!r} mais la fiche dit {ed.language!r}"
                )
            )
        work.editions.append(
            EditionSpec(
                path=path,
                source_file=path.relative_to(books_dir).as_posix(),
                language=ed.language,
                original=ed.original,
                title=ed.title,
                translators=ed.translators,
                publisher=ed.publisher,
                year=ed.year,
                access=ed.access,
            )
        )

    for epub in sorted(work_dir.glob("*.epub")):
        if epub.name not in declared:
            issues.append(CatalogIssue(epub, "EPUB présent mais non déclaré dans work.toml"))

    originals = [e for e in work.editions if e.original]
    if len(originals) > 1:
        issues.append(CatalogIssue(manifest_path, "plusieurs éditions marquées original = true"))
    if not originals and len(work.editions) >= 2:
        issues.append(
            CatalogIssue(
                manifest_path,
                "aucune édition marquée original = true : les traductions s'alignent sur une "
                "référence provisoire (l'édition la plus longue), à refaire quand l'original arrivera",
                fatal=False,
            )
        )
    if (
        originals
        and work.original_language
        and originals[0].language.split("-")[0] != work.original_language.split("-")[0]
    ):
        issues.append(
            CatalogIssue(
                manifest_path,
                f"l'édition originale est en {originals[0].language!r} mais "
                f"original_language = {work.original_language!r}",
            )
        )
    return work, issues


def scan(books_dir: Path) -> Catalog:
    """Parcourt books/<auteur>/<œuvre>/ ; tout dossier contenant un EPUB ou un
    work.toml est une œuvre."""
    works: list[WorkSpec] = []
    issues: list[CatalogIssue] = []
    if not books_dir.is_dir():
        return Catalog([], [CatalogIssue(books_dir, "dossier introuvable")])

    work_dirs = sorted(
        {
            p.parent
            for p in books_dir.rglob("*")
            if p.is_file() and (p.suffix == ".epub" or p.name == "work.toml")
        }
    )
    persons, person_issues = load_persons(books_dir / "persons.toml")
    issues.extend(person_issues)
    for work_dir in work_dirs:
        depth = len(work_dir.relative_to(books_dir).parts)
        if depth != 2:
            issues.append(
                CatalogIssue(work_dir, "emplacement attendu : books/<auteur>/<œuvre>/ (exactement 2 niveaux)")
            )
            continue
        work, work_issues = load_work(work_dir, books_dir)
        issues.extend(work_issues)
        if work and not any(i.fatal for i in work_issues):
            works.append(work)
    return Catalog(works, issues, persons)


def load_persons(path: Path) -> tuple[dict[str, PersonSpec], list[CatalogIssue]]:
    if not path.is_file():
        return {}, []
    try:
        raw = tomllib.loads(path.read_text("utf-8"))
    except tomllib.TOMLDecodeError as e:
        return {}, [CatalogIssue(path, f"TOML invalide : {e}")]
    persons: dict[str, PersonSpec] = {}
    issues: list[CatalogIssue] = []
    for name, value in raw.items():
        try:
            persons[name] = PersonSpec.model_validate(value)
        except ValidationError as e:
            for err in e.errors():
                loc = ".".join(str(p) for p in err["loc"])
                issues.append(CatalogIssue(path, f"{name} : {loc} : {err['msg']}"))
    return persons, issues
