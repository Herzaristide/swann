"""Modèles des requêtes et réponses : source du contrat OpenAPI (/v1/openapi.json)."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Generic, Literal, TypeVar
from uuid import UUID

from pydantic import BaseModel, Field

T = TypeVar("T")

Access = Literal["open", "excerpt", "restricted"]
AlignmentStatus = Literal["pending", "reliable", "doubtful", "rejected"]


class Page(BaseModel, Generic[T]):  # noqa: UP046 - syntaxe class Page[T] mal gérée par OpenAPI
    items: list[T]
    next_cursor: str | None = Field(
        None, description="À passer en `cursor` pour la page suivante ; null = fin."
    )


# ------------------------------------------------------------------ catalogue
class PersonRef(BaseModel):
    id: UUID
    name: str


class Author(PersonRef):
    birth_year: int | None = None
    death_year: int | None = None


class MovementRef(BaseModel):
    id: UUID
    slug: str
    label: str


class AlignmentSummary(BaseModel):
    status: AlignmentStatus
    aligned_ratio: float | None


class EditionSummary(BaseModel):
    id: UUID
    title: str
    language: str
    is_original: bool
    translators: list[PersonRef]
    publisher: str | None
    year: int | None
    access: Access
    revision: int
    alignment: AlignmentSummary | None


class WorkSummary(BaseModel):
    id: UUID
    slug: str | None
    title: str = Field(description="Titre dans la langue d'affichage (repli : titre de référence).")
    title_language: str | None
    original_title: str
    original_language: str | None
    first_published_year: int | None
    authors: list[Author]
    movements: list[MovementRef]
    languages: list[str] = Field(description="Langues des éditions visibles.")


class WorkDetail(WorkSummary):
    wikidata_id: str | None
    titles: dict[str, str] = Field(description="Titres connus, par langue.")
    editions: list[EditionSummary]


class WorkRef(BaseModel):
    id: UUID
    title: str
    first_published_year: int | None = None


class PersonSummary(BaseModel):
    id: UUID
    name: str
    sort_name: str | None
    birth_year: int | None
    death_year: int | None
    wikidata_id: str | None
    n_works: int = Field(description="Œuvres écrites (visibles).")
    n_translations: int = Field(description="Éditions traduites (visibles).")


class PersonName(BaseModel):
    language: str
    name: str


class Translation(BaseModel):
    edition_id: UUID
    edition_title: str
    language: str
    work: WorkRef


class PersonDetail(PersonSummary):
    names: list[PersonName]
    works: list[WorkRef]
    translations: list[Translation]


class MovementNode(BaseModel):
    id: UUID
    slug: str
    label: str
    start_year: int | None
    end_year: int | None
    n_works: int = Field(description="Œuvres du courant et de ses sous-courants.")
    children: list[MovementNode] = []


class MovementDetail(MovementNode):
    wikidata_id: str | None
    parent: MovementRef | None


class LanguageCount(BaseModel):
    language: str
    n_editions: int
    n_works: int
    n_originals: int


class Suggestion(BaseModel):
    kind: Literal["work", "person"]
    id: UUID
    label: str
    detail: str | None = Field(None, description="Auteurs (œuvre) ou dates (personne).")
    score: float


# ------------------------------------------------------------- éditions/texte
class Contributor(PersonRef):
    role: Literal["translator", "editor", "preface", "illustrator"]


class EditionDetail(EditionSummary):
    work: WorkRef
    contributors: list[Contributor]
    n_pages: int | None
    n_segments: int
    char_length: int
    has_page_breaks: bool
    reference_edition_id: UUID | None = Field(
        description="Édition de référence de l'alignement (null = elle-même ou non alignée)."
    )
    epub_available: bool


class TocSection(BaseModel):
    id: UUID
    kind: str
    matter: Literal["front", "body", "back"]
    label: str | None
    title: str | None
    number: int | None
    seq_start: int | None
    seq_end: int | None
    children: list[TocSection] = []


class Toc(BaseModel):
    edition_id: UUID
    revision: int
    sections: list[TocSection]


class NoteRef(BaseModel):
    offset: int = Field(description="Position de l'appel dans `text` du segment.")
    label: str
    note_id: UUID
    origin: Literal["author", "translator", "editor", "unknown"]


class PageMark(BaseModel):
    offset: int = Field(description="Début de la page dans `text` du segment.")
    label: str


class Segment(BaseModel):
    seq: int
    section_id: UUID | None
    kind: str
    speaker: str | None
    char_start: int
    char_end: int
    text: str
    markup: str | None
    notes: list[NoteRef] = []
    pages: list[PageMark] = []


class Note(BaseModel):
    note_id: UUID
    label: str | None
    origin: str | None
    segments: list[Segment]


class SegmentRange(BaseModel):
    edition_id: UUID
    revision: int
    total_segments: int
    segments: list[Segment]
    next_from_seq: int | None
    notes: dict[UUID, Note] | None = Field(None, description="Contenu des notes appelées (include=notes).")


class Position(BaseModel):
    seq: int
    offset: int
    section_id: UUID | None
    path: list[str]
    page_label: str | None
    progress: float = Field(description="Part du texte avant cette position (0 à 1).")


class FindHit(BaseModel):
    seq: int
    offset: int
    length: int
    snippet: str
    snippet_offset: int = Field(description="Position de la correspondance dans `snippet`.")
    section_path: list[str]


class FindResult(BaseModel):
    edition_id: UUID
    revision: int
    hits: list[FindHit]
    truncated: bool = Field(description="Vrai si d'autres correspondances existent au-delà de `limit`.")


class Quote(BaseModel):
    exact: str = Field(min_length=1, max_length=500)
    prefix: str = Field("", max_length=200)
    suffix: str = Field("", max_length=200)


class Anchor(BaseModel):
    key: str = Field(max_length=200, description="Identifiant libre, renvoyé tel quel.")
    revision: int
    seq: int
    offset: int = 0
    quote: Quote | None = None


class AnchorsRequest(BaseModel):
    anchors: list[Anchor] = Field(max_length=500)


class AnchorResult(BaseModel):
    key: str
    status: Literal["unchanged", "relocated", "approximate", "lost"]
    seq: int | None
    offset: int | None


class AnchorsResponse(BaseModel):
    revision: int
    results: list[AnchorResult]


# ------------------------------------------------------------------ recherche
class YearRange(BaseModel):
    min: int | None = None
    max: int | None = None


class SearchFilters(BaseModel):
    languages: list[str] = []
    original_only: bool = False
    year: YearRange | None = None
    author_ids: list[UUID] = []
    movement_ids: list[UUID] = []
    work_ids: list[UUID] = []
    edition_ids: list[UUID] = []


class SearchRequest(BaseModel):
    q: str = Field(min_length=1, max_length=2000)
    mode: Literal["theme", "quote", "words"] = "theme"
    filters: SearchFilters = SearchFilters()
    group_by: Literal["work", "edition", "none"] = "work"
    show_languages: list[str] = Field([], max_length=5)
    limit: int = Field(10, ge=1, le=50)


class SimilarRequest(BaseModel):
    edition_id: UUID
    seq_start: int
    seq_end: int
    filters: SearchFilters = SearchFilters()
    group_by: Literal["work", "edition", "none"] = "work"
    exclude_same_work: bool = True
    limit: int = Field(10, ge=1, le=50)


class HitWork(BaseModel):
    id: UUID
    title: str
    authors: list[PersonRef]
    first_published_year: int | None


class HitEdition(BaseModel):
    id: UUID
    title: str
    language: str
    is_original: bool
    access: Access
    revision: int


class Passage(BaseModel):
    seq_start: int
    seq_end: int
    section_path: list[str]
    page_label: str | None
    text: str
    truncated: bool = Field(False, description="Extrait tronqué (édition `excerpt`).")
    highlights: list[tuple[int, int]] = Field([], description="Plages [début, fin[ dans `text`.")


class TranslatedPassage(BaseModel):
    edition_id: UUID
    language: str
    seq_start: int
    seq_end: int
    text: str
    truncated: bool = False


class SearchHit(BaseModel):
    score: float
    exact: bool = Field(False, description="La citation figure mot pour mot dans le passage.")
    work: HitWork
    edition: HitEdition
    passage: Passage
    translations: dict[str, TranslatedPassage | None] = {}


class IndexInfo(BaseModel):
    collection: str
    dense_model: str
    chunker_version: str


class SearchResponse(BaseModel):
    index: IndexInfo
    took_ms: int
    hits: list[SearchHit]


# ----------------------------------------------------------------- alignement
class EditionAlignment(BaseModel):
    edition_id: UUID
    title: str
    language: str
    is_original: bool
    is_reference: bool
    status: AlignmentStatus | None
    method: str | None
    n_segments: int | None
    aligned_ratio: float | None
    mean_score: float | None
    low_score_ratio: float | None
    aligned_at: datetime | None


class WorkAlignment(BaseModel):
    work_id: UUID
    reference_edition_id: UUID | None
    n_units: int
    editions: list[EditionAlignment]


class Counterpart(BaseModel):
    edition_id: UUID
    language: str
    revision: int
    seq_start: int
    seq_end: int
    via: Literal["segment", "section"]
    score: float | None


class EditionRev(BaseModel):
    edition_id: UUID
    revision: int


class ParallelPair(BaseModel):
    source_seqs: list[int]
    target_seqs: list[int]
    score: float | None
    method: str | None


class Parallel(BaseModel):
    source: EditionRev
    target: EditionRev
    quality: AlignmentStatus | None
    pairs: list[ParallelPair]


class LinkSide(BaseModel):
    edition_id: UUID
    revision: int
    seq: int
    text: str


class AlignmentLink(BaseModel):
    unit_id: UUID
    method: str
    score: float | None
    source: LinkSide
    reference: list[LinkSide] = Field(
        description="Segments de l'édition de référence rattachés à la même unité."
    )


class ReviewSource(BaseModel):
    edition_id: UUID
    seq: int


class ReviewRequest(BaseModel):
    source: ReviewSource
    unit_id: UUID
    verdict: Literal["correct", "incorrect", "partial"]
    is_sample: bool = False
    comment: str | None = Field(None, max_length=2000)


class ReviewCreated(BaseModel):
    id: UUID
    created_at: datetime


class LinkRequest(BaseModel):
    source: ReviewSource
    unit_id: UUID


# ----------------------------------------------------------------- changements
class Change(BaseModel):
    cursor: str
    at: datetime
    type: str
    work_id: UUID | None
    edition_id: UUID | None
    data: dict[str, Any]


class Changes(BaseModel):
    items: list[Change]
    next_cursor: str | None = Field(
        description="Dernier curseur lu (à repasser en `after`), inchangé si vide."
    )


# --------------------------------------------------------------------- service
class Meta(BaseModel):
    api_version: str
    index: dict[str, Any] | None
