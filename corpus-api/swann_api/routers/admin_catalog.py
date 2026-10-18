"""Modification des fiches et de la structure (docs/console.md §5).

Chaque modification est journalisée dans `corpus_events` (auteur = `sub`
Keycloak) et lance si besoin une tâche du worker :
- `sync_payload` quand un champ copié dans Qdrant change (droits, langue,
  auteurs, courants, année, œuvre) ;
- `reprocess` après une modification de structure (découpage, index et
  alignement dépendent des sections) ;
- `trash_edition` / `restore_edition` / `purge` pour la corbeille ;
- `align` après une fusion ou un déplacement.

Concurrence optimiste : les fiches renvoient un ETag (`updated_at`) ; un
`If-Match` périmé donne 412.

Une modification de structure incrémente la révision de l'édition et émet
`edition.text_replaced` (reason = structure) : les applications invalident
leurs caches et relocalisent leurs ancres (le texte et les `seq` ne changent
pas, la relocalisation est immédiate).
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Any, Literal
from uuid import UUID

from fastapi import APIRouter, Header, Query, Response
from pydantic import BaseModel, Field

from swann_api.auth import Admin, Principal
from swann_api.deps import Conn, Cursor, decode_cursor, encode_cursor
from swann_api.errors import Problem, not_found
from swann_api.routers.admin import create_job, jsonb
from swann_api.schemas import Access, Page

router = APIRouter(prefix="/admin", tags=["administration"])

LANGUAGE = r"^[a-z]{2,3}(-[A-Za-z0-9]{2,8})*$"
QID = r"^Q\d+$"
SectionKind = Literal[
    "volume", "part", "book", "chapter", "section", "act", "scene", "poem", "canto", "story", "letter",
    "essay", "dedication", "epigraph", "preface", "foreword", "introduction", "prologue", "epilogue",
    "afterword", "appendix", "glossary", "notes", "note", "other",
]  # fmt: skip
Matter = Literal["front", "body", "back"]
NoteOrigin = Literal["author", "translator", "editor", "unknown"]


# --------------------------------------------------------------------- modèles
class TitleIn(BaseModel):
    language: str = Field(pattern=LANGUAGE)
    title: str = Field(min_length=1)


class PersonLink(BaseModel):
    id: UUID
    name: str
    wikidata_id: str | None = None


class MovementLink(BaseModel):
    id: UUID
    slug: str
    label: str | None = None


class AdminEditionRow(BaseModel):
    id: UUID
    title: str
    language: str
    is_original: bool
    access: Access
    publisher: str | None
    year: int | None
    revision: int
    source_file: str
    translators: list[PersonLink]
    n_segments: int
    quality_score: int | None
    signals: list[str]
    alignment_status: str | None
    aligned_ratio: float | None
    is_reference: bool
    deleted_at: datetime | None
    updated_at: datetime


class AdminWork(BaseModel):
    id: UUID
    slug: str | None
    title: str
    original_language: str | None
    first_published_year: int | None
    wikidata_id: str | None
    titles: list[TitleIn]
    authors: list[PersonLink]
    movements: list[MovementLink]
    editions: list[AdminEditionRow]
    deleted_at: datetime | None
    created_at: datetime
    updated_at: datetime


class AdminWorkRow(BaseModel):
    id: UUID
    slug: str | None
    title: str
    original_language: str | None
    first_published_year: int | None
    wikidata_id: str | None
    authors: list[str]
    languages: list[str]
    n_editions: int
    min_score: int | None
    deleted_at: datetime | None
    updated_at: datetime


class WorkPatch(BaseModel):
    title: str | None = Field(None, min_length=1)
    original_language: str | None = Field(None, pattern=LANGUAGE)
    first_published_year: int | None = None
    wikidata_id: str | None = Field(None, pattern=QID)
    titles: list[TitleIn] | None = None
    author_ids: list[UUID] | None = None
    movement_ids: list[UUID] | None = None


class EditionPatch(BaseModel):
    title: str | None = Field(None, min_length=1)
    language: str | None = Field(None, pattern=LANGUAGE)
    is_original: bool | None = None
    access: Access | None = None
    publisher: str | None = None
    year: int | None = None
    translator_ids: list[UUID] | None = None


class MergeRequest(BaseModel):
    into: UUID = Field(description="Œuvre (ou personne) qui reste.")


class MoveRequest(BaseModel):
    work_id: UUID


class AdminPerson(BaseModel):
    id: UUID
    display_name: str
    sort_name: str | None
    birth_year: int | None
    death_year: int | None
    wikidata_id: str | None
    names: list[dict[str, str]]
    works: list[dict[str, Any]]
    translations: list[dict[str, Any]]
    updated_at: datetime


class AdminPersonRow(BaseModel):
    id: UUID
    display_name: str
    birth_year: int | None
    death_year: int | None
    wikidata_id: str | None
    n_works: int
    n_translations: int


class PersonIn(BaseModel):
    display_name: str = Field(min_length=1)
    sort_name: str | None = None
    birth_year: int | None = None
    death_year: int | None = None
    wikidata_id: str | None = Field(None, pattern=QID)
    names: list[dict[str, str]] | None = None  # [{language, name}]


class PersonPatch(BaseModel):
    display_name: str | None = Field(None, min_length=1)
    sort_name: str | None = None
    birth_year: int | None = None
    death_year: int | None = None
    wikidata_id: str | None = Field(None, pattern=QID)
    names: list[dict[str, str]] | None = None


class AdminMovement(BaseModel):
    id: UUID
    slug: str
    parent_id: UUID | None
    start_year: int | None
    end_year: int | None
    wikidata_id: str | None
    labels: dict[str, str]
    n_works: int


class MovementIn(BaseModel):
    slug: str = Field(pattern=r"^[a-z0-9]+(-[a-z0-9]+)*$")
    parent_id: UUID | None = None
    start_year: int | None = None
    end_year: int | None = None
    wikidata_id: str | None = Field(None, pattern=QID)
    labels: dict[str, str] = {}


class MovementPatch(BaseModel):
    slug: str | None = Field(None, pattern=r"^[a-z0-9]+(-[a-z0-9]+)*$")
    parent_id: UUID | None = None
    start_year: int | None = None
    end_year: int | None = None
    wikidata_id: str | None = Field(None, pattern=QID)
    labels: dict[str, str] | None = None


class StructureSection(BaseModel):
    id: UUID
    parent_id: UUID | None
    seq: int
    depth: int
    kind: SectionKind
    matter: Matter
    label: str | None
    title: str | None
    number: int | None
    seq_start: int | None = Field(description="Premier segment (descendants compris).")
    seq_end: int | None
    n_segments: int
    own_segments: int = Field(description="Segments rattachés directement à la section.")
    preview: str | None = Field(description="Début du premier paragraphe.")
    reference_section_id: UUID | None


class Structure(BaseModel):
    edition_id: UUID
    revision: int
    title: str
    language: str
    sections: list[StructureSection]
    reprocess_pending: bool


class SectionPatch(BaseModel):
    kind: SectionKind | None = None
    matter: Matter | None = None
    label: str | None = None
    title: str | None = None
    number: int | None = None
    parent_id: UUID | None = Field(None, description="Nouveau parent ; utiliser `to_root` pour la racine.")
    to_root: bool = False
    apply_to_descendants: bool = Field(False, description="`matter` appliqué aussi aux sous-sections.")


class SplitRequest(BaseModel):
    at_seq: int = Field(description="Premier segment de la nouvelle section.")
    title: str | None = None
    label: str | None = None
    kind: SectionKind | None = None


class SegmentPreview(BaseModel):
    seq: int
    kind: str
    text: str


# ---------------------------------------------------------------------- outils
def etag(updated_at: datetime) -> str:
    return f'"{updated_at.isoformat()}"'


def check_if_match(if_match: str | None, updated_at: datetime) -> None:
    """`If-Match` : l'ETag reçu, ou la date `updated_at` telle que sérialisée
    en JSON (« …Z » ou « …+00:00 ») ; comparée en tant que date."""
    if not if_match or if_match.strip() == "*":
        return
    try:
        sent = datetime.fromisoformat(if_match.strip().strip('"'))
    except ValueError:
        sent = None
    if sent != updated_at:
        raise Problem(
            412,
            "precondition-failed",
            "La fiche a été modifiée entre-temps : recharger avant d'enregistrer.",
            title="Modification concurrente",
            current=etag(updated_at),
        )


async def emit(conn, type_: str, work_id, edition_id, data: dict, principal: Principal) -> None:
    await conn.execute(
        "SELECT corpus_emit(%s, %s, %s, %s, %s)", (type_, work_id, edition_id, jsonb(data), principal.sub)
    )


async def queue_sync(conn, principal: Principal, *, work_id=None, edition_ids=None, title="Fiches → Qdrant"):
    """Recopie la fiche dans le payload Qdrant (sans doublon en file)."""
    params: dict = {"actor": principal.sub}
    if work_id:
        params["work_id"] = str(work_id)
    if edition_ids:
        params["edition_ids"] = [str(e) for e in edition_ids]
    await create_job(conn, "sync_payload", title, params, principal.sub, work_id=work_id)


async def queue_once(conn, kind: str, title: str, params: dict, principal: Principal, **links) -> None:
    """Crée la tâche sauf si la même attend déjà en file."""
    key = "edition_id" if "edition_id" in params else "work_id"
    exists = await (
        await conn.execute(
            f"SELECT 1 FROM jobs WHERE kind = %s AND status = 'queued' AND params->>'{key}' = %s",
            (kind, str(params[key])),
        )
    ).fetchone()
    if not exists:
        await create_job(conn, kind, title, {**params, "actor": principal.sub}, principal.sub, **links)


async def fetch_one(conn, sql: str, params) -> dict:
    return await (await conn.execute(sql, params)).fetchone()


# ---------------------------------------------------------------------- œuvres
@router.get("/works", response_model=Page[AdminWorkRow], summary="Œuvres (corbeille comprise sur demande)")
async def list_works(
    conn: Conn,
    _: Admin,
    q: str | None = None,
    trashed: bool = False,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    cursor: Cursor = None,
) -> dict:
    offset = int((decode_cursor(cursor) or {}).get("offset", 0))
    rows = await (
        await conn.execute(
            """
            SELECT w.id, w.slug, w.title, w.original_language, w.first_published_year, w.wikidata_id,
                   w.deleted_at, w.updated_at,
                   coalesce(array(SELECT p.display_name FROM work_authors wa JOIN persons p ON p.id =
                       wa.person_id
                                  WHERE wa.work_id = w.id ORDER BY wa.position), '{}') AS authors,
                   coalesce(array(SELECT DISTINCT e.language FROM editions e
                                  WHERE e.work_id = w.id AND e.deleted_at IS NULL ORDER BY 1), '{}') AS
                                      languages,
                   (SELECT count(*) FROM editions e WHERE e.work_id = w.id AND e.deleted_at IS NULL) AS
                       n_editions,
                   (SELECT min(q.score) FROM editions e JOIN edition_quality q ON q.edition_id = e.id
                     WHERE e.work_id = w.id AND e.deleted_at IS NULL) AS min_score
            FROM works w
            WHERE (w.deleted_at IS NOT NULL) = %(trashed)s
              AND (%(q)s::text IS NULL OR w.title ILIKE %(like)s OR w.slug ILIKE %(like)s
                   OR EXISTS (SELECT 1 FROM work_titles t WHERE t.work_id = w.id AND t.title ILIKE %(like)s)
                   OR EXISTS (SELECT 1 FROM work_authors wa JOIN persons p ON p.id = wa.person_id
                              WHERE wa.work_id = w.id AND p.display_name ILIKE %(like)s))
            ORDER BY w.title, w.id LIMIT %(limit)s OFFSET %(offset)s
            """,
            {"trashed": trashed, "q": q, "like": f"%{q}%", "limit": limit + 1, "offset": offset},
        )
    ).fetchall()
    next_cursor = encode_cursor({"offset": offset + limit}) if len(rows) > limit else None
    return {"items": rows[:limit], "next_cursor": next_cursor}


async def load_work(conn, work_id: UUID) -> dict:
    work = await fetch_one(
        conn,
        """
        SELECT w.id, w.slug, w.title, w.original_language, w.first_published_year, w.wikidata_id,
               w.deleted_at, w.created_at, w.updated_at,
               coalesce((SELECT json_agg(json_build_object('language', t.language, 'title', t.title)
                                         ORDER BY t.language, t.title)
                         FROM work_titles t WHERE t.work_id = w.id), '[]') AS titles,
               coalesce((SELECT json_agg(json_build_object('id', p.id, 'name', p.display_name,
                                                           'wikidata_id', p.wikidata_id) ORDER BY wa.position)
                         FROM work_authors wa JOIN persons p ON p.id = wa.person_id
                         WHERE wa.work_id = w.id), '[]') AS authors,
               coalesce((SELECT json_agg(json_build_object(
                             'id', m.id, 'slug', m.slug,
                             'label', (SELECT ml.label FROM movement_labels ml WHERE ml.movement_id = m.id
                                       ORDER BY ml.language = 'fr' DESC, ml.language LIMIT 1)) ORDER BY
                                           m.slug)
                         FROM work_movements wm JOIN movements m ON m.id = wm.movement_id
                         WHERE wm.work_id = w.id), '[]') AS movements
        FROM works w WHERE w.id = %s
        """,
        (work_id,),
    )
    if work is None:
        raise not_found("Œuvre")
    work["editions"] = await (
        await conn.execute(
            """
            SELECT e.id, e.title, e.language, e.is_original, e.access::text AS access, e.publisher, e.year,
                   e.revision, e.source_file, e.deleted_at, e.updated_at,
                   coalesce((SELECT json_agg(json_build_object('id', p.id, 'name', p.display_name,
                                                               'wikidata_id', p.wikidata_id) ORDER BY
                                                                   c.position)
                             FROM edition_contributors c JOIN persons p ON p.id = c.person_id
                             WHERE c.edition_id = e.id AND c.role = 'translator'), '[]') AS translators,
                   (SELECT count(*) FROM segments s WHERE s.edition_id = e.id) AS n_segments,
                   q.score AS quality_score, coalesce(q.signals, '{}') AS signals,
                   ea.status::text AS alignment_status, ea.aligned_ratio,
                   coalesce(ea.edition_id IS NOT NULL AND ea.reference_edition_id IS NULL, false) AS
                       is_reference
            FROM editions e
            LEFT JOIN edition_quality q ON q.edition_id = e.id
            LEFT JOIN edition_alignments ea ON ea.edition_id = e.id
            WHERE e.work_id = %s
            ORDER BY e.deleted_at NULLS FIRST, e.is_original DESC, e.language, e.title
            """,
            (work_id,),
        )
    ).fetchall()
    return work


@router.get("/works/{work_id}", response_model=AdminWork, summary="Fiche complète d'une œuvre")
async def get_work(work_id: UUID, conn: Conn, _: Admin, response: Response) -> dict:
    work = await load_work(conn, work_id)
    response.headers["ETag"] = etag(work["updated_at"])
    return work


PAYLOAD_WORK_FIELDS = {"first_published_year", "author_ids", "movement_ids"}


@router.patch("/works/{work_id}", response_model=AdminWork, summary="Modifier une œuvre")
async def patch_work(
    work_id: UUID,
    body: WorkPatch,
    conn: Conn,
    principal: Admin,
    response: Response,
    if_match: Annotated[str | None, Header()] = None,
) -> dict:
    fields = body.model_dump(exclude_unset=True)
    async with conn.transaction():
        current = await fetch_one(conn, "SELECT updated_at FROM works WHERE id = %s FOR UPDATE", (work_id,))
        if current is None:
            raise not_found("Œuvre")
        check_if_match(if_match, current["updated_at"])
        simple = {
            k: fields[k]
            for k in ("title", "original_language", "first_published_year", "wikidata_id")
            if k in fields
        }
        if simple:
            if simple.get("wikidata_id") and await fetch_one(
                conn,
                "SELECT 1 FROM works WHERE wikidata_id = %s AND id <> %s",
                (simple["wikidata_id"], work_id),
            ):
                raise Problem(409, "wikidata-taken", "Une autre œuvre porte déjà cet identifiant Wikidata.")
            sets = ", ".join(f"{k} = %({k})s" for k in simple)
            await conn.execute(f"UPDATE works SET {sets} WHERE id = %(id)s", {**simple, "id": work_id})
        if body.titles is not None:
            await conn.execute("DELETE FROM work_titles WHERE work_id = %s", (work_id,))
            for t in body.titles:
                await conn.execute(
                    "INSERT INTO work_titles (work_id, language, title) VALUES (%s, %s, %s) ON CONFLICT DO "
                    "NOTHING",
                    (work_id, t.language, t.title),
                )
        if body.author_ids is not None:
            await conn.execute("DELETE FROM work_authors WHERE work_id = %s", (work_id,))
            for pos, pid in enumerate(dict.fromkeys(body.author_ids)):
                await conn.execute(
                    "INSERT INTO work_authors (work_id, person_id, position) VALUES (%s, %s, %s)",
                    (work_id, pid, pos),
                )
        if body.movement_ids is not None:
            await conn.execute("DELETE FROM work_movements WHERE work_id = %s", (work_id,))
            for mid in set(body.movement_ids):
                await conn.execute(
                    "INSERT INTO work_movements (work_id, movement_id) VALUES (%s, %s)", (work_id, mid)
                )
        if fields:
            await conn.execute("UPDATE works SET updated_at = now() WHERE id = %s", (work_id,))
            await emit(conn, "work.updated", work_id, None, {"fields": sorted(fields)}, principal)
        if PAYLOAD_WORK_FIELDS & fields.keys():
            await queue_sync(conn, principal, work_id=work_id)
    work = await load_work(conn, work_id)
    response.headers["ETag"] = etag(work["updated_at"])
    return work


async def _trash_editions(conn, edition_ids: list, principal: Principal) -> None:
    for eid in edition_ids:
        await create_job(
            conn,
            "trash_edition",
            "Mise à la corbeille",
            {"edition_id": str(eid), "actor": principal.sub},
            principal.sub,
            edition_id=eid,
        )


@router.delete("/works/{work_id}", response_model=AdminWork, summary="Mettre une œuvre à la corbeille")
async def trash_work(work_id: UUID, conn: Conn, principal: Admin) -> dict:
    async with conn.transaction():
        row = await fetch_one(
            conn,
            "UPDATE works SET deleted_at = now(), deleted_by_sub = %s WHERE id = %s AND deleted_at IS NULL "
            "RETURNING id",
            (principal.sub, work_id),
        )
        if row is None:
            raise Problem(409, "already-trashed", "Œuvre introuvable ou déjà à la corbeille.")
        editions = await (
            await conn.execute(
                "UPDATE editions SET deleted_at = now(), deleted_by_sub = %s "
                "WHERE work_id = %s AND deleted_at IS NULL RETURNING id",
                (principal.sub, work_id),
            )
        ).fetchall()
        await _trash_editions(conn, [e["id"] for e in editions], principal)
        await emit(
            conn, "work.trashed", work_id, None, {"editions": [str(e["id"]) for e in editions]}, principal
        )
    return await load_work(conn, work_id)


@router.post("/works/{work_id}/restore", response_model=AdminWork, summary="Sortir une œuvre de la corbeille")
async def restore_work(work_id: UUID, conn: Conn, principal: Admin) -> dict:
    async with conn.transaction():
        row = await fetch_one(conn, "SELECT deleted_at FROM works WHERE id = %s", (work_id,))
        if row is None or row["deleted_at"] is None:
            raise Problem(409, "not-trashed", "Œuvre introuvable ou pas à la corbeille.")
        # Les éditions mises à la corbeille avec l'œuvre (même instant ou après) reviennent.
        editions = await (
            await conn.execute(
                "UPDATE editions SET deleted_at = NULL, deleted_by_sub = NULL "
                "WHERE work_id = %s AND deleted_at >= %s RETURNING id",
                (work_id, row["deleted_at"]),
            )
        ).fetchall()
        await conn.execute(
            "UPDATE works SET deleted_at = NULL, deleted_by_sub = NULL WHERE id = %s", (work_id,)
        )
        for e in editions:
            await create_job(
                conn,
                "restore_edition",
                "Sortie de corbeille",
                {"edition_id": str(e["id"]), "actor": principal.sub},
                principal.sub,
                edition_id=e["id"],
            )
        await emit(
            conn, "work.restored", work_id, None, {"editions": [str(e["id"]) for e in editions]}, principal
        )
    return await load_work(conn, work_id)


@router.post("/works/{work_id}/merge", response_model=AdminWork, summary="Fusionner dans une autre œuvre")
async def merge_work(work_id: UUID, body: MergeRequest, conn: Conn, principal: Admin) -> dict:
    """Les éditions, titres, auteurs et courants de l'œuvre rejoignent `into` ;
    l'œuvre fusionnée va à la corbeille. L'alignement de `into` est refait."""
    if body.into == work_id:
        raise Problem(400, "merge-self", "Impossible de fusionner une œuvre avec elle-même.")
    async with conn.transaction():
        for wid in (work_id, body.into):
            if not await fetch_one(conn, "SELECT 1 FROM works WHERE id = %s AND deleted_at IS NULL", (wid,)):
                raise not_found("Œuvre")
        # Les liens d'alignement de l'œuvre fusionnée pointent vers ses unités : à refaire.
        await conn.execute("DELETE FROM work_units WHERE work_id IN (%s, %s)", (work_id, body.into))
        await conn.execute(
            "DELETE FROM edition_alignments WHERE edition_id IN (SELECT id FROM editions WHERE work_id IN "
            "(%s, %s))",
            (work_id, body.into),
        )
        moved = await (
            await conn.execute(
                "UPDATE editions SET work_id = %s WHERE work_id = %s RETURNING id", (body.into, work_id)
            )
        ).fetchall()
        await conn.execute(
            "INSERT INTO work_titles (work_id, language, title) SELECT %s, language, title FROM work_titles "
            "WHERE work_id = %s ON CONFLICT DO NOTHING",
            (body.into, work_id),
        )
        await conn.execute(
            "INSERT INTO work_authors (work_id, person_id, position) "
            "SELECT %s, person_id, 100 + position FROM work_authors WHERE work_id = %s ON CONFLICT DO "
            "NOTHING",
            (body.into, work_id),
        )
        await conn.execute(
            "INSERT INTO work_movements (work_id, movement_id) SELECT %s, movement_id FROM work_movements "
            "WHERE work_id = %s ON CONFLICT DO NOTHING",
            (body.into, work_id),
        )
        await conn.execute(
            "UPDATE works SET deleted_at = now(), deleted_by_sub = %s, wikidata_id = NULL WHERE id = %s",
            (principal.sub, work_id),
        )
        await emit(
            conn,
            "work.merged",
            body.into,
            None,
            {"merged_work_id": str(work_id), "editions": [str(e["id"]) for e in moved]},
            principal,
        )
        await emit(conn, "work.trashed", work_id, None, {"merged_into": str(body.into)}, principal)
        await queue_sync(conn, principal, work_id=body.into)
        await create_job(
            conn,
            "align",
            "Réalignement après fusion",
            {"work_id": str(body.into), "force": True, "actor": principal.sub},
            principal.sub,
            work_id=body.into,
        )
    return await load_work(conn, body.into)


# --------------------------------------------------------------------- éditions
async def load_edition_row(conn, edition_id: UUID) -> dict:
    row = await fetch_one(
        conn,
        "SELECT id, work_id, updated_at, deleted_at, language, is_original, access::text AS access "
        "FROM editions WHERE id = %s",
        (edition_id,),
    )
    if row is None:
        raise not_found("Édition")
    return row


@router.patch("/editions/{edition_id}", response_model=AdminWork, summary="Modifier une édition")
async def patch_edition(
    edition_id: UUID,
    body: EditionPatch,
    conn: Conn,
    principal: Admin,
    if_match: Annotated[str | None, Header()] = None,
) -> dict:
    fields = body.model_dump(exclude_unset=True)
    async with conn.transaction():
        current = await load_edition_row(conn, edition_id)
        check_if_match(if_match, current["updated_at"])
        simple = {
            k: fields[k]
            for k in ("title", "language", "is_original", "access", "publisher", "year")
            if k in fields
        }
        if simple:
            sets = ", ".join(f"{k} = %({k})s" for k in simple)
            await conn.execute(f"UPDATE editions SET {sets} WHERE id = %(id)s", {**simple, "id": edition_id})
        if body.translator_ids is not None:
            await conn.execute(
                "DELETE FROM edition_contributors WHERE edition_id = %s AND role = 'translator'",
                (edition_id,),
            )
            for pos, pid in enumerate(dict.fromkeys(body.translator_ids)):
                await conn.execute(
                    "INSERT INTO edition_contributors (edition_id, person_id, role, position) "
                    "VALUES (%s, %s, 'translator', %s)",
                    (edition_id, pid, pos),
                )
        if fields:
            await emit(
                conn, "edition.updated", current["work_id"], edition_id, {"fields": sorted(fields)}, principal
            )
        if {"language", "is_original", "access"} & fields.keys():
            await queue_sync(conn, principal, edition_ids=[edition_id])
    return await load_work(conn, current["work_id"])


@router.post("/editions/{edition_id}/move", response_model=AdminWork, summary="Déplacer vers une autre œuvre")
async def move_edition(edition_id: UUID, body: MoveRequest, conn: Conn, principal: Admin) -> dict:
    async with conn.transaction():
        current = await load_edition_row(conn, edition_id)
        old_work = current["work_id"]
        if old_work == body.work_id:
            raise Problem(400, "same-work", "L'édition appartient déjà à cette œuvre.")
        if not await fetch_one(
            conn, "SELECT 1 FROM works WHERE id = %s AND deleted_at IS NULL", (body.work_id,)
        ):
            raise not_found("Œuvre")
        was_reference = await fetch_one(
            conn,
            "SELECT 1 FROM edition_alignments WHERE edition_id = %s AND reference_edition_id IS NULL",
            (edition_id,),
        )
        if was_reference:  # les autres éditions s'alignaient sur celle-ci
            await conn.execute("DELETE FROM work_units WHERE work_id = %s", (old_work,))
            await conn.execute(
                "DELETE FROM edition_alignments WHERE edition_id IN (SELECT id FROM editions WHERE work_id "
                "= %s)",
                (old_work,),
            )
        else:
            await conn.execute(
                "DELETE FROM segment_alignments sa USING segments s "
                "WHERE s.id = sa.segment_id AND s.edition_id = %s",
                (edition_id,),
            )
            await conn.execute("DELETE FROM edition_alignments WHERE edition_id = %s", (edition_id,))
        await conn.execute(
            "UPDATE sections SET reference_section_id = NULL WHERE edition_id = %s", (edition_id,)
        )
        await conn.execute("UPDATE editions SET work_id = %s WHERE id = %s", (body.work_id, edition_id))
        await emit(
            conn, "edition.moved", body.work_id, edition_id, {"from_work_id": str(old_work)}, principal
        )
        await queue_sync(conn, principal, edition_ids=[edition_id])
        for wid, incremental in ((body.work_id, True), (old_work, False)):
            await create_job(
                conn,
                "align",
                "Réalignement après déplacement",
                {"work_id": str(wid), "incremental": incremental, "actor": principal.sub},
                principal.sub,
                work_id=wid,
            )
    return await load_work(conn, body.work_id)


@router.delete(
    "/editions/{edition_id}", response_model=AdminWork, summary="Mettre une édition à la corbeille"
)
async def trash_edition(edition_id: UUID, conn: Conn, principal: Admin) -> dict:
    async with conn.transaction():
        row = await fetch_one(
            conn,
            "UPDATE editions SET deleted_at = now(), deleted_by_sub = %s WHERE id = %s AND deleted_at IS "
            "NULL "
            "RETURNING work_id",
            (principal.sub, edition_id),
        )
        if row is None:
            raise Problem(409, "already-trashed", "Édition introuvable ou déjà à la corbeille.")
        await _trash_editions(conn, [edition_id], principal)
        await emit(conn, "edition.trashed", row["work_id"], edition_id, {}, principal)
    return await load_work(conn, row["work_id"])


@router.post("/editions/{edition_id}/restore", response_model=AdminWork, summary="Sortir de la corbeille")
async def restore_edition(edition_id: UUID, conn: Conn, principal: Admin) -> dict:
    async with conn.transaction():
        row = await fetch_one(
            conn,
            "UPDATE editions SET deleted_at = NULL, deleted_by_sub = NULL WHERE id = %s AND deleted_at IS "
            "NOT NULL "
            "RETURNING work_id",
            (edition_id,),
        )
        if row is None:
            raise Problem(409, "not-trashed", "Édition introuvable ou pas à la corbeille.")
        await conn.execute(
            "UPDATE works SET deleted_at = NULL, deleted_by_sub = NULL WHERE id = %s", (row["work_id"],)
        )
        await create_job(
            conn,
            "restore_edition",
            "Sortie de corbeille",
            {"edition_id": str(edition_id), "actor": principal.sub},
            principal.sub,
            edition_id=edition_id,
        )
        await emit(conn, "edition.restored", row["work_id"], edition_id, {}, principal)
    return await load_work(conn, row["work_id"])


@router.post("/editions/{edition_id}/purge", status_code=202, summary="Supprimer définitivement (corbeille)")
async def purge_edition(edition_id: UUID, conn: Conn, principal: Admin) -> dict:
    row = await fetch_one(conn, "SELECT deleted_at FROM editions WHERE id = %s", (edition_id,))
    if row is None:
        raise not_found("Édition")
    if row["deleted_at"] is None:
        raise Problem(409, "not-trashed", "Seule une édition à la corbeille se supprime définitivement.")
    job_id = await create_job(
        conn,
        "purge",
        "Suppression définitive",
        {"edition_ids": [str(edition_id)], "actor": principal.sub},
        principal.sub,
        edition_id=edition_id,
    )
    return {"job_id": job_id}


# -------------------------------------------------------------------- structure
@router.get("/editions/{edition_id}/structure", response_model=Structure, summary="Arbre des sections")
async def get_structure(edition_id: UUID, conn: Conn, _: Admin) -> dict:
    edition = await fetch_one(
        conn, "SELECT id, revision, title, language FROM editions WHERE id = %s", (edition_id,)
    )
    if edition is None:
        raise not_found("Édition")
    sections = await (
        await conn.execute(
            """
            SELECT s.id, s.parent_id, s.seq, p.depth, s.kind::text AS kind, s.matter::text AS matter, s.label,
                   s.title, s.number, s.reference_section_id, r.seq_start, r.seq_end,
                   coalesce(r.n_segments, 0) AS n_segments,
                   (SELECT count(*) FROM segments g WHERE g.section_id = s.id) AS own_segments,
                   (SELECT left(g.text, 160) FROM segments g WHERE g.section_id = s.id AND g.kind <> 'heading'
                    ORDER BY g.seq LIMIT 1) AS preview
            FROM sections s
            JOIN section_paths p ON p.section_id = s.id
            LEFT JOIN section_ranges(%(e)s) r ON r.section_id = s.id
            WHERE s.edition_id = %(e)s
            ORDER BY s.seq
            """,
            {"e": edition_id},
        )
    ).fetchall()
    pending = await fetch_one(
        conn,
        "SELECT 1 FROM jobs WHERE kind = 'reprocess' AND status IN ('queued', 'running') "
        "AND params->>'edition_id' = %s",
        (str(edition_id),),
    )
    return {**edition, "edition_id": edition["id"], "sections": sections, "reprocess_pending": bool(pending)}


@router.get(
    "/editions/{edition_id}/sections/{section_id}/segments",
    response_model=list[SegmentPreview],
    summary="Segments d'une section (pour choisir où la scinder)",
)
async def section_segments(
    edition_id: UUID, section_id: UUID, conn: Conn, _: Admin, limit: Annotated[int, Query(le=2000)] = 500
) -> list:
    return await (
        await conn.execute(
            "SELECT seq, kind::text AS kind, left(text, 300) AS text FROM segments "
            "WHERE edition_id = %s AND section_id = %s ORDER BY seq LIMIT %s",
            (edition_id, section_id, limit),
        )
    ).fetchall()


async def structure_changed(conn, edition_id: UUID, principal: Principal, change: dict) -> None:
    """Nouvelle révision (caches des applications), événement, retraitement."""
    row = await fetch_one(
        conn,
        "UPDATE editions SET revision = revision + 1 WHERE id = %s RETURNING work_id, revision",
        (edition_id,),
    )
    await emit(
        conn,
        "edition.text_replaced",
        row["work_id"],
        edition_id,
        {
            "old_revision": row["revision"] - 1,
            "new_revision": row["revision"],
            "reason": "structure",
            **change,
        },
        principal,
    )
    await queue_once(
        conn,
        "reprocess",
        "Retraitement (structure modifiée)",
        {"edition_id": str(edition_id)},
        principal,
        edition_id=edition_id,
        work_id=row["work_id"],
    )


async def load_section(conn, edition_id: UUID, section_id: UUID) -> dict:
    row = await fetch_one(
        conn, "SELECT * FROM sections WHERE id = %s AND edition_id = %s FOR UPDATE", (section_id, edition_id)
    )
    if row is None:
        raise not_found("Section")
    return row


async def shift_sections(conn, edition_id: UUID, after_seq: int, delta: int) -> None:
    """Décale le `seq` des sections après `after_seq` (contrainte unique : en
    deux temps, par des valeurs négatives)."""
    await conn.execute(
        "UPDATE sections SET seq = -(seq + %s) - 1 WHERE edition_id = %s AND seq > %s",
        (delta, edition_id, after_seq),
    )
    await conn.execute("UPDATE sections SET seq = -seq - 1 WHERE edition_id = %s AND seq < 0", (edition_id,))


@router.patch(
    "/editions/{edition_id}/sections/{section_id}", response_model=Structure, summary="Modifier une section"
)
async def patch_section(
    edition_id: UUID, section_id: UUID, body: SectionPatch, conn: Conn, principal: Admin
) -> dict:
    fields = body.model_dump(exclude_unset=True, exclude={"to_root", "apply_to_descendants", "parent_id"})
    async with conn.transaction():
        section = await load_section(conn, edition_id, section_id)
        if fields:
            sets = ", ".join(f"{k} = %({k})s" for k in fields)
            await conn.execute(f"UPDATE sections SET {sets} WHERE id = %(id)s", {**fields, "id": section_id})
        if body.matter and body.apply_to_descendants:
            await conn.execute(
                """
                WITH RECURSIVE sub AS (
                    SELECT id FROM sections WHERE parent_id = %(s)s
                    UNION ALL SELECT c.id FROM sections c JOIN sub ON c.parent_id = sub.id
                )
                UPDATE sections SET matter = %(m)s WHERE id IN (SELECT id FROM sub)
                """,
                {"s": section_id, "m": body.matter},
            )
        if body.to_root or body.parent_id is not None:
            new_parent = None if body.to_root else body.parent_id
            if new_parent is not None:
                cycle = await fetch_one(
                    conn,
                    """
                    WITH RECURSIVE up AS (
                        SELECT id, parent_id FROM sections WHERE id = %(p)s AND edition_id = %(e)s
                        UNION ALL SELECT s.id, s.parent_id FROM sections s JOIN up ON s.id = up.parent_id
                    )
                    SELECT count(*) FILTER (WHERE id = %(s)s) AS loops, count(*) AS n FROM up
                    """,
                    {"p": new_parent, "e": edition_id, "s": section_id},
                )
                if cycle["n"] == 0:
                    raise not_found("Section parente")
                if cycle["loops"]:
                    raise Problem(
                        400, "cycle", "Une section ne peut pas être rangée sous l'une de ses sous-sections."
                    )
            await conn.execute("UPDATE sections SET parent_id = %s WHERE id = %s", (new_parent, section_id))
        changed = sorted(body.model_dump(exclude_unset=True))
        if changed:
            await structure_changed(
                conn, edition_id, principal, {"section_id": str(section["id"]), "fields": changed}
            )
    return await get_structure(edition_id, conn, principal)


@router.post(
    "/editions/{edition_id}/sections/{section_id}/split",
    response_model=Structure,
    summary="Scinder une section à un segment",
)
async def split_section(
    edition_id: UUID, section_id: UUID, body: SplitRequest, conn: Conn, principal: Admin
) -> dict:
    """Les segments de la section à partir de `at_seq` forment une nouvelle
    section sœur, placée juste après (mêmes parent, type et matière)."""
    async with conn.transaction():
        section = await load_section(conn, edition_id, section_id)
        own = await fetch_one(
            conn,
            "SELECT min(seq) AS first, max(seq) AS last, count(*) FILTER (WHERE seq >= %s) AS moved "
            "FROM segments WHERE section_id = %s",
            (body.at_seq, section_id),
        )
        if not own["moved"] or body.at_seq <= own["first"]:
            raise Problem(400, "split", "`at_seq` doit désigner un segment de la section, après le premier.")
        # Après la section et toutes ses sous-sections dans l'ordre des seq.
        last = await fetch_one(
            conn,
            """
            WITH RECURSIVE sub AS (
                SELECT id, seq FROM sections WHERE id = %s
                UNION ALL SELECT c.id, c.seq FROM sections c JOIN sub ON c.parent_id = sub.id
            ) SELECT max(seq) AS seq FROM sub
            """,
            (section_id,),
        )
        await shift_sections(conn, edition_id, last["seq"], 1)
        new = await fetch_one(
            conn,
            """
            INSERT INTO sections (edition_id, parent_id, seq, kind, matter, label, title, number)
            VALUES (%s, %s, %s, %s, %s, %s, %s, NULL) RETURNING id
            """,
            (
                edition_id,
                section["parent_id"],
                last["seq"] + 1,
                body.kind or section["kind"],
                section["matter"],
                body.label,
                body.title,
            ),
        )
        await conn.execute(
            "UPDATE segments SET section_id = %s WHERE section_id = %s AND seq >= %s",
            (new["id"], section_id, body.at_seq),
        )
        await structure_changed(
            conn,
            edition_id,
            principal,
            {"split": str(section_id), "new_section_id": str(new["id"]), "at_seq": body.at_seq},
        )
    return await get_structure(edition_id, conn, principal)


@router.post(
    "/editions/{edition_id}/sections/{section_id}/merge-next",
    response_model=Structure,
    summary="Fusionner avec la section sœur suivante",
)
async def merge_next(edition_id: UUID, section_id: UUID, conn: Conn, principal: Admin) -> dict:
    async with conn.transaction():
        section = await load_section(conn, edition_id, section_id)
        nxt = await fetch_one(
            conn,
            """
            SELECT id FROM sections WHERE edition_id = %s AND parent_id IS NOT DISTINCT FROM %s AND seq > %s
            ORDER BY seq LIMIT 1
            """,
            (edition_id, section["parent_id"], section["seq"]),
        )
        if nxt is None:
            raise Problem(400, "merge", "Pas de section sœur après celle-ci.")
        await conn.execute(
            "UPDATE segments SET section_id = %s WHERE section_id = %s", (section_id, nxt["id"])
        )
        await conn.execute("UPDATE sections SET parent_id = %s WHERE parent_id = %s", (section_id, nxt["id"]))
        await conn.execute(
            "UPDATE note_refs SET note_section_id = %s WHERE note_section_id = %s", (section_id, nxt["id"])
        )
        await conn.execute(
            "UPDATE sections SET reference_section_id = NULL WHERE reference_section_id = %s", (nxt["id"],)
        )
        await conn.execute("DELETE FROM sections WHERE id = %s", (nxt["id"],))
        await structure_changed(
            conn, edition_id, principal, {"merged": str(nxt["id"]), "into": str(section_id)}
        )
    return await get_structure(edition_id, conn, principal)


@router.patch(
    "/editions/{edition_id}/notes/{note_section_id}",
    response_model=Structure,
    summary="Origine d'une note (auteur, traducteur, éditeur)",
)
async def patch_note(
    edition_id: UUID,
    note_section_id: UUID,
    conn: Conn,
    principal: Admin,
    origin: Annotated[NoteOrigin, Query()],
) -> dict:
    async with conn.transaction():
        await load_section(conn, edition_id, note_section_id)
        await conn.execute(
            "UPDATE note_refs SET origin = %s WHERE note_section_id = %s", (origin, note_section_id)
        )
        await structure_changed(
            conn, edition_id, principal, {"note_section_id": str(note_section_id), "origin": origin}
        )
    return await get_structure(edition_id, conn, principal)


# -------------------------------------------------------------------- personnes
@router.get("/persons", response_model=Page[AdminPersonRow], summary="Personnes (auteurs, traducteurs)")
async def list_persons(
    conn: Conn,
    _: Admin,
    q: str | None = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    cursor: Cursor = None,
) -> dict:
    offset = int((decode_cursor(cursor) or {}).get("offset", 0))
    rows = await (
        await conn.execute(
            """
            SELECT p.id, p.display_name, p.birth_year, p.death_year, p.wikidata_id,
                   (SELECT count(*) FROM work_authors wa WHERE wa.person_id = p.id) AS n_works,
                   (SELECT count(*) FROM edition_contributors c WHERE c.person_id = p.id) AS n_translations
            FROM persons p
            WHERE %(q)s::text IS NULL OR p.display_name ILIKE %(like)s OR p.wikidata_id = %(q)s
               OR EXISTS (SELECT 1 FROM person_names pn WHERE pn.person_id = p.id AND pn.name ILIKE %(like)s)
            ORDER BY coalesce(p.sort_name, p.display_name), p.id LIMIT %(limit)s OFFSET %(offset)s
            """,
            {"q": q, "like": f"%{q}%", "limit": limit + 1, "offset": offset},
        )
    ).fetchall()
    next_cursor = encode_cursor({"offset": offset + limit}) if len(rows) > limit else None
    return {"items": rows[:limit], "next_cursor": next_cursor}


async def load_person(conn, person_id: UUID) -> dict:
    row = await fetch_one(
        conn,
        """
        SELECT p.*,
               coalesce((SELECT json_agg(json_build_object('language', language, 'name', name)
                                         ORDER BY language, name)
                         FROM person_names WHERE person_id = p.id), '[]') AS names,
               coalesce((SELECT json_agg(json_build_object('id', w.id, 'title', w.title, 'slug', w.slug,
                                                           'deleted', w.deleted_at IS NOT NULL) ORDER BY
                                                               w.title)
                         FROM work_authors wa JOIN works w ON w.id = wa.work_id
                         WHERE wa.person_id = p.id), '[]') AS works,
               coalesce((SELECT json_agg(json_build_object('id', e.id, 'title', e.title, 'language',
                   e.language,
                                                           'work_id', e.work_id) ORDER BY e.title)
                         FROM edition_contributors c JOIN editions e ON e.id = c.edition_id
                         WHERE c.person_id = p.id), '[]') AS translations
        FROM persons p WHERE p.id = %s
        """,
        (person_id,),
    )
    if row is None:
        raise not_found("Personne")
    return row


@router.get("/persons/{person_id}", response_model=AdminPerson, summary="Fiche d'une personne")
async def get_person(person_id: UUID, conn: Conn, _: Admin) -> dict:
    return await load_person(conn, person_id)


async def _set_names(conn, person_id: UUID, names: list[dict[str, str]]) -> None:
    await conn.execute("DELETE FROM person_names WHERE person_id = %s", (person_id,))
    for n in names:
        if n.get("language") and n.get("name"):
            await conn.execute(
                "INSERT INTO person_names (person_id, language, name) VALUES (%s, %s, %s) ON CONFLICT DO "
                "NOTHING",
                (person_id, n["language"], n["name"]),
            )


@router.post("/persons", response_model=AdminPerson, status_code=201, summary="Créer une personne")
async def create_person(body: PersonIn, conn: Conn, _: Admin) -> dict:
    async with conn.transaction():
        if body.wikidata_id and await fetch_one(
            conn, "SELECT 1 FROM persons WHERE wikidata_id = %s", (body.wikidata_id,)
        ):
            raise Problem(409, "wikidata-taken", "Une personne porte déjà cet identifiant Wikidata.")
        row = await fetch_one(
            conn,
            "INSERT INTO persons (display_name, sort_name, birth_year, death_year, wikidata_id) "
            "VALUES (%s, %s, %s, %s, %s) RETURNING id",
            (body.display_name, body.sort_name, body.birth_year, body.death_year, body.wikidata_id),
        )
        if body.names:
            await _set_names(conn, row["id"], body.names)
    return await load_person(conn, row["id"])


@router.patch("/persons/{person_id}", response_model=AdminPerson, summary="Modifier une personne")
async def patch_person(
    person_id: UUID,
    body: PersonPatch,
    conn: Conn,
    principal: Admin,
    if_match: Annotated[str | None, Header()] = None,
) -> dict:
    fields = body.model_dump(exclude_unset=True, exclude={"names"})
    async with conn.transaction():
        current = await fetch_one(
            conn, "SELECT updated_at FROM persons WHERE id = %s FOR UPDATE", (person_id,)
        )
        if current is None:
            raise not_found("Personne")
        check_if_match(if_match, current["updated_at"])
        if fields.get("wikidata_id") and await fetch_one(
            conn,
            "SELECT 1 FROM persons WHERE wikidata_id = %s AND id <> %s",
            (fields["wikidata_id"], person_id),
        ):
            raise Problem(409, "wikidata-taken", "Une autre personne porte déjà cet identifiant Wikidata.")
        if fields:
            sets = ", ".join(f"{k} = %({k})s" for k in fields)
            await conn.execute(f"UPDATE persons SET {sets} WHERE id = %(id)s", {**fields, "id": person_id})
        if body.names is not None:
            await _set_names(conn, person_id, body.names)
        works = await (
            await conn.execute("SELECT work_id FROM work_authors WHERE person_id = %s", (person_id,))
        ).fetchall()
        for w in works:
            await emit(conn, "work.updated", w["work_id"], None, {"fields": ["authors"]}, principal)
    return await load_person(conn, person_id)


@router.post("/persons/{person_id}/merge", response_model=AdminPerson, summary="Fusionner deux personnes")
async def merge_person(person_id: UUID, body: MergeRequest, conn: Conn, principal: Admin) -> dict:
    """La personne disparaît au profit de `into` : œuvres, traductions et
    variantes de nom passent à `into`."""
    if body.into == person_id:
        raise Problem(400, "merge-self", "Impossible de fusionner une personne avec elle-même.")
    async with conn.transaction():
        source = await fetch_one(conn, "SELECT * FROM persons WHERE id = %s", (person_id,))
        if source is None or not await fetch_one(conn, "SELECT 1 FROM persons WHERE id = %s", (body.into,)):
            raise not_found("Personne")
        works = await (
            await conn.execute("SELECT work_id FROM work_authors WHERE person_id = %s", (person_id,))
        ).fetchall()
        await conn.execute(
            "INSERT INTO work_authors (work_id, person_id, position) SELECT work_id, %s, position "
            "FROM work_authors WHERE person_id = %s ON CONFLICT DO NOTHING",
            (body.into, person_id),
        )
        await conn.execute(
            "INSERT INTO edition_contributors (edition_id, person_id, role, position) "
            "SELECT edition_id, %s, role, position FROM edition_contributors WHERE person_id = %s "
            "ON CONFLICT DO NOTHING",
            (body.into, person_id),
        )
        await conn.execute(
            "INSERT INTO person_names (person_id, language, name) SELECT %s, language, name FROM "
            "person_names "
            "WHERE person_id = %s ON CONFLICT DO NOTHING",
            (body.into, person_id),
        )
        await conn.execute(
            "INSERT INTO person_names (person_id, language, name) VALUES (%s, 'und', %s) ON CONFLICT DO "
            "NOTHING",
            (body.into, source["display_name"]),
        )
        await conn.execute(
            "UPDATE persons SET wikidata_id = coalesce(wikidata_id, %s), birth_year = coalesce(birth_year, "
            "%s), "
            "death_year = coalesce(death_year, %s) WHERE id = %s",
            (None, source["birth_year"], source["death_year"], body.into),
        )
        await conn.execute("DELETE FROM persons WHERE id = %s", (person_id,))
        if source["wikidata_id"]:
            await conn.execute(
                "UPDATE persons SET wikidata_id = %s WHERE id = %s AND wikidata_id IS NULL",
                (source["wikidata_id"], body.into),
            )
        for w in works:
            await emit(conn, "work.updated", w["work_id"], None, {"fields": ["authors"]}, principal)
            await queue_sync(conn, principal, work_id=w["work_id"])
    return await load_person(conn, body.into)


# -------------------------------------------------------------------- courants
@router.get("/movements", response_model=list[AdminMovement], summary="Courants littéraires")
async def list_movements(conn: Conn, _: Admin) -> list:
    return await (
        await conn.execute(
            """
            SELECT m.id, m.slug, m.parent_id, m.start_year, m.end_year, m.wikidata_id,
                   coalesce((SELECT json_object_agg(language, label) FROM movement_labels
                             WHERE movement_id = m.id), '{}') AS labels,
                   (SELECT count(*) FROM work_movements wm WHERE wm.movement_id = m.id) AS n_works
            FROM movements m ORDER BY m.slug
            """
        )
    ).fetchall()


async def _set_labels(conn, movement_id: UUID, labels: dict[str, str]) -> None:
    await conn.execute("DELETE FROM movement_labels WHERE movement_id = %s", (movement_id,))
    for lang, label in labels.items():
        if label:
            await conn.execute(
                "INSERT INTO movement_labels (movement_id, language, label) VALUES (%s, %s, %s)",
                (movement_id, lang, label),
            )


@router.post("/movements", response_model=list[AdminMovement], status_code=201, summary="Créer un courant")
async def create_movement(body: MovementIn, conn: Conn, principal: Admin) -> list:
    async with conn.transaction():
        if await fetch_one(conn, "SELECT 1 FROM movements WHERE slug = %s", (body.slug,)):
            raise Problem(409, "slug-taken", "Ce slug existe déjà.")
        row = await fetch_one(
            conn,
            "INSERT INTO movements (slug, parent_id, start_year, end_year, wikidata_id) "
            "VALUES (%s, %s, %s, %s, %s) RETURNING id",
            (body.slug, body.parent_id, body.start_year, body.end_year, body.wikidata_id),
        )
        await _set_labels(conn, row["id"], body.labels)
    return await list_movements(conn, principal)


@router.patch("/movements/{movement_id}", response_model=list[AdminMovement], summary="Modifier un courant")
async def patch_movement(movement_id: UUID, body: MovementPatch, conn: Conn, principal: Admin) -> list:
    fields = body.model_dump(exclude_unset=True, exclude={"labels"})
    async with conn.transaction():
        if not await fetch_one(conn, "SELECT 1 FROM movements WHERE id = %s", (movement_id,)):
            raise not_found("Courant")
        if fields.get("parent_id") == movement_id:
            raise Problem(400, "cycle", "Un courant ne peut pas être son propre parent.")
        if fields:
            sets = ", ".join(f"{k} = %({k})s" for k in fields)
            await conn.execute(
                f"UPDATE movements SET {sets} WHERE id = %(id)s", {**fields, "id": movement_id}
            )
        if body.labels is not None:
            await _set_labels(conn, movement_id, body.labels)
    return await list_movements(conn, principal)


@router.delete("/movements/{movement_id}", response_model=list[AdminMovement], summary="Supprimer un courant")
async def delete_movement(movement_id: UUID, conn: Conn, principal: Admin) -> list:
    async with conn.transaction():
        works = await (
            await conn.execute("SELECT work_id FROM work_movements WHERE movement_id = %s", (movement_id,))
        ).fetchall()
        await conn.execute("DELETE FROM movements WHERE id = %s", (movement_id,))
        for w in works:
            await emit(conn, "work.updated", w["work_id"], None, {"fields": ["movements"]}, principal)
            await queue_sync(conn, principal, work_id=w["work_id"])
    return await list_movements(conn, principal)


# --------------------------------------------------------------------- corbeille
class TrashItem(BaseModel):
    edition_id: UUID
    title: str
    language: str
    work_id: UUID
    work_title: str
    deleted_at: datetime
    deleted_by_sub: str | None
    purge_after: datetime


@router.get("/trash", response_model=list[TrashItem], summary="Éditions à la corbeille")
async def list_trash(conn: Conn, _: Admin, days: Annotated[int, Query(ge=0)] = 30) -> list:
    return await (
        await conn.execute(
            """
            SELECT e.id AS edition_id, e.title, e.language, w.id AS work_id, w.title AS work_title,
                   e.deleted_at, e.deleted_by_sub, e.deleted_at + make_interval(days => %s) AS purge_after
            FROM editions e JOIN works w ON w.id = e.work_id
            WHERE e.deleted_at IS NOT NULL ORDER BY e.deleted_at DESC
            """,
            (days,),
        )
    ).fetchall()
