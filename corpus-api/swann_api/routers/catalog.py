"""Catalogue : œuvres, personnes, courants, langues, autocomplétion."""

from __future__ import annotations

from typing import Annotated, Literal
from uuid import UUID

from fastapi import APIRouter, Query

from swann_api import sql
from swann_api.auth import Principal, Reader
from swann_api.deps import Conn, Cursor, Lang, Limit, decode_cursor, encode_cursor
from swann_api.errors import Problem, not_found
from swann_api.schemas import (
    LanguageCount,
    MovementDetail,
    MovementNode,
    Page,
    PersonDetail,
    PersonSummary,
    Suggestion,
    WorkDetail,
    WorkSummary,
)

router = APIRouter(tags=["catalogue"])


def params(principal: Principal, lang: str | None, **extra) -> dict:
    return {"lang": lang, "access": list(principal.visible_access), **extra}


def like_pattern(q: str) -> str:
    """Motif LIKE « contient », jokers de l'utilisateur neutralisés."""
    escaped = q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{escaped}%"


def keyset(cursor: dict | None, sort_key: str, descending: bool, id_col: str) -> str:
    if cursor is None:
        return "TRUE"
    op = "<" if descending else ">"
    return f"({sort_key}, {id_col}) {op} (%(cursor_key)s, %(cursor_id)s::uuid)"


def next_page(rows: list[dict], limit: int, sort: str) -> str | None:
    if len(rows) <= limit:
        return None
    last = rows[limit - 1]
    return encode_cursor({"s": sort, "k": last["sort_key"], "id": str(last["id"])})


def cursor_params(cursor: str | None, sort: str) -> tuple[dict | None, dict]:
    decoded = decode_cursor(cursor)
    if decoded is None:
        return None, {}
    if decoded.get("s") != sort or "k" not in decoded or "id" not in decoded:
        raise Problem(400, "invalid-cursor", "Curseur invalide pour ce tri.")
    return decoded, {"cursor_key": decoded["k"], "cursor_id": decoded["id"]}


# --------------------------------------------------------------------- œuvres
WORK_SORTS: dict[str, tuple[str, bool]] = {
    "title": ("search_key(b.title)", False),
    "-title": ("search_key(b.title)", True),
    "year": ("coalesce(b.first_published_year, 2147483647)", False),
    "-year": ("coalesce(b.first_published_year, -2147483648)", True),
    "author": ("b.author_key", False),
}

WORK_BASE = f"""
    SELECT w.id, w.slug, coalesce(wt.title, w.title) AS title,
           CASE WHEN wt.title IS NULL THEN w.original_language ELSE wt.language END AS title_language,
           w.title AS original_title, w.original_language, w.first_published_year, w.wikidata_id,
           coalesce((SELECT search_key(coalesce(p.sort_name, p.display_name))
                     FROM work_authors wa JOIN persons p ON p.id = wa.person_id
                     WHERE wa.work_id = w.id ORDER BY wa.position LIMIT 1), '') AS author_key
    FROM works w
    LEFT JOIN LATERAL ({sql.WORK_TITLE}) wt ON true
"""

WORK_FIELDS = f"""
    b.id, b.slug, b.title, b.title_language, b.original_title, b.original_language,
    b.first_published_year, b.wikidata_id,
    {sql.authors_json("b")} AS authors,
    {sql.movements_json("b")} AS movements,
    array(SELECT DISTINCT e.language FROM editions e
          WHERE e.work_id = b.id AND {sql.visible("e")} ORDER BY 1) AS languages
"""

MOVEMENT_DESCENDANTS = """
    WITH RECURSIVE d AS (
        SELECT id FROM movements WHERE id = %(movement_id)s
        UNION ALL SELECT m.id FROM movements m JOIN d ON m.parent_id = d.id
    ) SELECT id FROM d
"""


@router.get("/works", response_model=Page[WorkSummary], summary="Liste filtrée et triée des œuvres")
async def list_works(
    conn: Conn,
    principal: Reader,
    lang: Lang,
    q: Annotated[str | None, Query(description="Titre, dans toutes les langues.")] = None,
    author_id: UUID | None = None,
    movement_id: Annotated[UUID | None, Query(description="Courant, sous-courants compris.")] = None,
    language: Annotated[str | None, Query(description="Au moins une édition dans cette langue.")] = None,
    original_language: str | None = None,
    year_min: int | None = None,
    year_max: int | None = None,
    slug: str | None = None,
    sort: Literal["title", "-title", "year", "-year", "author"] = "title",
    cursor: Cursor = None,
    limit: Limit = 20,
) -> dict:
    decoded, cparams = cursor_params(cursor, sort)
    p = params(
        principal,
        lang,
        q=like_pattern(q) if q else None,
        author_id=author_id,
        movement_id=movement_id,
        language=language,
        original_language=original_language,
        year_min=year_min,
        year_max=year_max,
        slug=slug,
        n=limit + 1,
        **cparams,
    )
    filters = [sql.work_visible("w")]
    if q:
        filters.append(
            "(search_key(w.title) LIKE search_key(%(q)s) OR EXISTS (SELECT 1 FROM work_titles t "
            "WHERE t.work_id = w.id AND search_key(t.title) LIKE search_key(%(q)s)))"
        )
    if author_id:
        filters.append(
            "EXISTS (SELECT 1 FROM work_authors wa WHERE wa.work_id = w.id AND wa.person_id = %(author_id)s)"
        )
    if movement_id:
        filters.append(
            "EXISTS (SELECT 1 FROM work_movements wm WHERE wm.work_id = w.id "
            f"AND wm.movement_id IN ({MOVEMENT_DESCENDANTS}))"
        )
    if language:
        filters.append(
            "EXISTS (SELECT 1 FROM editions le WHERE le.work_id = w.id AND "
            f"(le.language = %(language)s OR split_part(le.language, '-', 1) = %(language)s) "
            f"AND {sql.visible('le')})"
        )
    if original_language:
        filters.append("split_part(w.original_language, '-', 1) = split_part(%(original_language)s, '-', 1)")
    if year_min is not None:
        filters.append("w.first_published_year >= %(year_min)s")
    if year_max is not None:
        filters.append("w.first_published_year <= %(year_max)s")
    if slug:
        filters.append("w.slug = %(slug)s")

    sort_key, desc = WORK_SORTS[sort]
    direction = "DESC" if desc else "ASC"
    query = f"""
        WITH base AS ({WORK_BASE} WHERE {" AND ".join(filters)})
        SELECT {WORK_FIELDS}, {sort_key} AS sort_key
        FROM base b
        WHERE {keyset(decoded, sort_key, desc, "b.id")}
        ORDER BY sort_key {direction}, b.id {direction}
        LIMIT %(n)s
    """
    rows = await (await conn.execute(query, p)).fetchall()
    return {"items": rows[:limit], "next_cursor": next_page(rows, limit, sort)}


@router.get("/works/{work_id}", response_model=WorkDetail, summary="Fiche d'une œuvre et de ses éditions")
async def get_work(work_id: UUID, conn: Conn, principal: Reader, lang: Lang) -> dict:
    p = params(principal, lang, work_id=work_id)
    row = await (
        await conn.execute(
            f"""
            WITH base AS ({WORK_BASE} WHERE w.id = %(work_id)s AND {sql.work_visible("w")})
            SELECT {WORK_FIELDS},
                   (SELECT coalesce(json_object_agg(language, title), '{{}}') FROM (
                        SELECT DISTINCT ON (language) language, title FROM work_titles
                        WHERE work_id = b.id ORDER BY language, title) t) AS titles
            FROM base b
            """,
            p,
        )
    ).fetchone()
    if row is None:
        raise not_found("Œuvre")
    row["editions"] = await (
        await conn.execute(
            f"""
            SELECT {sql.EDITION_SUMMARY} FROM editions e
            WHERE e.work_id = %(work_id)s AND {sql.visible("e")}
            ORDER BY e.is_original DESC, e.language, e.year NULLS LAST, e.title
            """,
            p,
        )
    ).fetchall()
    return row


# ------------------------------------------------------------------ personnes
PERSON_FIELDS = f"""
    p.id, {sql.person_name()} AS name, p.sort_name, p.birth_year, p.death_year, p.wikidata_id,
    (SELECT count(*) FROM work_authors wa JOIN works w ON w.id = wa.work_id
     WHERE wa.person_id = p.id AND {sql.work_visible("w")}) AS n_works,
    (SELECT count(*) FROM edition_contributors c JOIN editions e ON e.id = c.edition_id
     WHERE c.person_id = p.id AND c.role = 'translator' AND {sql.visible("e")}) AS n_translations
"""

AUTHOR_OF_VISIBLE = f"""EXISTS (SELECT 1 FROM work_authors wa JOIN works w ON w.id = wa.work_id
    WHERE wa.person_id = p.id AND {sql.work_visible("w")})"""
TRANSLATOR_OF_VISIBLE = f"""EXISTS (SELECT 1 FROM edition_contributors c
    JOIN editions e ON e.id = c.edition_id
    WHERE c.person_id = p.id AND c.role = 'translator' AND {sql.visible("e")})"""


@router.get("/persons", response_model=Page[PersonSummary], summary="Auteurs et traducteurs")
async def list_persons(
    conn: Conn,
    principal: Reader,
    lang: Lang,
    role: Literal["author", "translator"] | None = None,
    q: Annotated[str | None, Query(description="Nom, toutes graphies.")] = None,
    cursor: Cursor = None,
    limit: Limit = 20,
) -> dict:
    sort = "name"
    decoded, cparams = cursor_params(cursor, sort)
    p = params(principal, lang, q=like_pattern(q) if q else None, n=limit + 1, **cparams)
    if role == "author":
        filters = [AUTHOR_OF_VISIBLE]
    elif role == "translator":
        filters = [TRANSLATOR_OF_VISIBLE]
    else:
        filters = [f"({AUTHOR_OF_VISIBLE} OR {TRANSLATOR_OF_VISIBLE})"]
    if q:
        filters.append(
            "(search_key(p.display_name) LIKE search_key(%(q)s) OR EXISTS (SELECT 1 FROM person_names pn "
            "WHERE pn.person_id = p.id AND search_key(pn.name) LIKE search_key(%(q)s)))"
        )
    sort_key = "search_key(coalesce(p.sort_name, p.display_name))"
    rows = await (
        await conn.execute(
            f"""
            SELECT {PERSON_FIELDS}, {sort_key} AS sort_key FROM persons p
            WHERE {" AND ".join(filters)} AND {keyset(decoded, sort_key, False, "p.id")}
            ORDER BY sort_key, p.id LIMIT %(n)s
            """,
            p,
        )
    ).fetchall()
    return {"items": rows[:limit], "next_cursor": next_page(rows, limit, sort)}


@router.get("/persons/{person_id}", response_model=PersonDetail, summary="Fiche d'une personne")
async def get_person(person_id: UUID, conn: Conn, principal: Reader, lang: Lang) -> dict:
    p = params(principal, lang, person_id=person_id)
    row = await (
        await conn.execute(
            f"""
            SELECT {PERSON_FIELDS},
                   (SELECT coalesce(json_agg(json_build_object('language', language, 'name', name)
                                             ORDER BY language, name), '[]')
                    FROM person_names WHERE person_id = p.id) AS names
            FROM persons p
            WHERE p.id = %(person_id)s AND ({AUTHOR_OF_VISIBLE} OR {TRANSLATOR_OF_VISIBLE})
            """,
            p,
        )
    ).fetchone()
    if row is None:
        raise not_found("Personne")
    row["works"] = await (
        await conn.execute(
            f"""
            SELECT w.id, coalesce(wt.title, w.title) AS title, w.first_published_year
            FROM work_authors wa JOIN works w ON w.id = wa.work_id
            LEFT JOIN LATERAL ({sql.WORK_TITLE}) wt ON true
            WHERE wa.person_id = %(person_id)s AND {sql.work_visible("w")}
            ORDER BY w.first_published_year NULLS LAST, 2
            """,
            p,
        )
    ).fetchall()
    rows = await (
        await conn.execute(
            f"""
            SELECT e.id AS edition_id, e.title AS edition_title, e.language,
                   json_build_object('id', w.id, 'title', coalesce(wt.title, w.title),
                                     'first_published_year', w.first_published_year) AS work
            FROM edition_contributors c JOIN editions e ON e.id = c.edition_id
            JOIN works w ON w.id = e.work_id
            LEFT JOIN LATERAL ({sql.WORK_TITLE}) wt ON true
            WHERE c.person_id = %(person_id)s AND c.role = 'translator' AND {sql.visible("e")}
            ORDER BY e.year NULLS LAST, e.title
            """,
            p,
        )
    ).fetchall()
    row["translations"] = rows
    return row


# ------------------------------------------------------------------- courants
MOVEMENTS = f"""
    WITH RECURSIVE closure AS (
        SELECT id AS ancestor, id AS descendant FROM movements
        UNION ALL
        SELECT c.ancestor, m.id FROM closure c JOIN movements m ON m.parent_id = c.descendant
    )
    SELECT m.id, m.slug, m.parent_id, m.start_year, m.end_year, m.wikidata_id,
           {sql.movement_label()} AS label,
           (SELECT count(DISTINCT wm.work_id) FROM closure c
            JOIN work_movements wm ON wm.movement_id = c.descendant
            JOIN works w ON w.id = wm.work_id
            WHERE c.ancestor = m.id AND {sql.work_visible("w")}) AS n_works
    FROM movements m
"""


def build_tree(rows: list[dict]) -> tuple[list[dict], dict]:
    nodes = {r["id"]: {**r, "children": []} for r in rows}
    roots = []
    for node in sorted(nodes.values(), key=lambda n: (n["start_year"] or 0, n["label"])):
        parent = nodes.get(node["parent_id"])
        (parent["children"] if parent else roots).append(node)
    return roots, nodes


@router.get("/movements", response_model=list[MovementNode], summary="Arbre des courants littéraires")
async def list_movements(conn: Conn, principal: Reader, lang: Lang) -> list[dict]:
    rows = await (await conn.execute(MOVEMENTS, params(principal, lang))).fetchall()
    roots, _ = build_tree(rows)
    return roots


@router.get(
    "/movements/{movement_id}", response_model=MovementDetail, summary="Un courant et ses sous-courants"
)
async def get_movement(movement_id: UUID, conn: Conn, principal: Reader, lang: Lang) -> dict:
    rows = await (await conn.execute(MOVEMENTS, params(principal, lang))).fetchall()
    _, nodes = build_tree(rows)
    node = nodes.get(movement_id)
    if node is None:
        raise not_found("Courant")
    parent = nodes.get(node["parent_id"])
    node["parent"] = {k: parent[k] for k in ("id", "slug", "label")} if parent else None
    return node


# -------------------------------------------------------------------- langues
@router.get("/languages", response_model=list[LanguageCount], summary="Langues du corpus")
async def list_languages(conn: Conn, principal: Reader) -> list[dict]:
    return await (
        await conn.execute(
            f"""
            SELECT e.language, count(*) AS n_editions, count(DISTINCT e.work_id) AS n_works,
                   count(*) FILTER (WHERE e.is_original) AS n_originals
            FROM editions e WHERE {sql.visible("e")}
            GROUP BY e.language ORDER BY n_editions DESC, e.language
            """,
            params(principal, None),
        )
    ).fetchall()


# --------------------------------------------------------------- autocomplétion
@router.get("/suggest", response_model=list[Suggestion], summary="Autocomplétion : œuvres et personnes")
async def suggest(
    conn: Conn,
    principal: Reader,
    lang: Lang,
    q: Annotated[str, Query(min_length=2, max_length=200)],
    limit: Annotated[int, Query(ge=1, le=20)] = 8,
) -> list[dict]:
    # `<%` = similarité de mots (pg_trgm), servie par les index trigrammes sur search_key().
    return await (
        await conn.execute(
            f"""
            WITH k AS (SELECT search_key(%(q)s) AS k),
            work_hits AS (
                SELECT id, max(score) AS score FROM (
                    SELECT t.work_id AS id, word_similarity(k.k, search_key(t.title)) AS score
                    FROM work_titles t, k WHERE k.k <%% search_key(t.title)
                    UNION ALL
                    SELECT w.id, word_similarity(k.k, search_key(w.title))
                    FROM works w, k WHERE k.k <%% search_key(w.title)
                ) s GROUP BY id
            ),
            person_hits AS (
                SELECT id, max(score) AS score FROM (
                    SELECT p.id, word_similarity(k.k, search_key(p.display_name)) AS score
                    FROM persons p, k WHERE k.k <%% search_key(p.display_name)
                    UNION ALL
                    SELECT pn.person_id, word_similarity(k.k, search_key(pn.name))
                    FROM person_names pn, k WHERE k.k <%% search_key(pn.name)
                ) s GROUP BY id
            )
            SELECT * FROM (
                SELECT 'work' AS kind, w.id, coalesce(wt.title, w.title) AS label,
                       (SELECT string_agg({sql.person_name()}, ', ' ORDER BY wa.position)
                        FROM work_authors wa JOIN persons p ON p.id = wa.person_id
                        WHERE wa.work_id = w.id) AS detail,
                       h.score
                FROM work_hits h JOIN works w ON w.id = h.id
                LEFT JOIN LATERAL ({sql.WORK_TITLE}) wt ON true
                WHERE {sql.work_visible("w")}
                UNION ALL
                SELECT 'person', p.id, {sql.person_name()},
                       nullif(concat_ws('–', p.birth_year, p.death_year), ''), h.score
                FROM person_hits h JOIN persons p ON p.id = h.id
                WHERE {AUTHOR_OF_VISIBLE} OR {TRANSLATOR_OF_VISIBLE}
            ) s
            ORDER BY score DESC, label LIMIT %(limit)s
            """,
            params(principal, lang, q=q, limit=limit),
        )
    ).fetchall()
