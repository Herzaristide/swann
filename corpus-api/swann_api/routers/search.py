"""Recherche dans tout le corpus : thème (sens), citation (sens + mots exacts),
mots (BM25), et passages proches d'un passage donné."""

from __future__ import annotations

import time

from fastapi import APIRouter, Request
from qdrant_client import models

from swann_api import sql
from swann_api.auth import EXCERPT_MAX_CHARS, Principal, Reader
from swann_api.deps import Conn, Lang
from swann_api.engine import ActiveIndex, SearchEngine
from swann_api.errors import not_found
from swann_api.schemas import SearchFilters, SearchRequest, SearchResponse, SimilarRequest
from swann_api.text import excerpt, find_all
from swann_core import qdrant as qstore
from swann_core.embed import sparse

router = APIRouter(tags=["recherche"])

CANDIDATES = 100  # candidats de chaque côté avant la fusion RRF (mode quote)
GROUP_KEYS = {"work": "work_id", "edition": "edition_id"}


# -------------------------------------------------------------------- filtres
async def corpus_languages(conn: Conn) -> list[str]:
    rows = await (await conn.execute("SELECT DISTINCT language FROM editions")).fetchall()
    return [r["language"] for r in rows]


def expand_languages(requested: list[str], available: list[str]) -> list[str]:
    """« fr » couvre aussi « fr-CA » : le payload Qdrant ne connaît que l'égalité."""
    out = set()
    for lang in requested:
        base = lang.split("-")[0]
        out.add(lang)
        out.update(a for a in available if a == lang or a.split("-")[0] == base and "-" not in lang)
    return sorted(out)


def qdrant_filter(
    f: SearchFilters, principal: Principal, languages: list[str], extra_must_not: list | None = None
) -> models.Filter:
    def any_of(key: str, values) -> models.FieldCondition:
        return models.FieldCondition(key=key, match=models.MatchAny(any=[str(v) for v in values]))

    must: list = [any_of("access", principal.visible_access)]
    if languages:
        must.append(any_of("language", languages))
    if f.original_only:
        must.append(models.FieldCondition(key="is_original", match=models.MatchValue(value=True)))
    if f.year and (f.year.min is not None or f.year.max is not None):
        must.append(
            models.FieldCondition(
                key="first_published_year", range=models.Range(gte=f.year.min, lte=f.year.max)
            )
        )
    for key, values in (
        ("author_ids", f.author_ids),
        ("movement_ids", f.movement_ids),
        ("work_id", f.work_ids),
        ("edition_id", f.edition_ids),
    ):
        if values:
            must.append(any_of(key, values))
    return models.Filter(must=must, must_not=extra_must_not or None)


# ---------------------------------------------------------------- surlignage
def term_spans(text: str, query: str, language: str | None) -> list[tuple[int, int]]:
    """Mots du passage dont la racine est celle d'un mot de la requête."""
    wanted = set(sparse.terms(query, language))
    if not wanted:
        return []
    return [
        m.span()
        for m in sparse.WORD_RE.finditer(text)
        if (stems := sparse.terms(m.group(), language)) and stems[0] in wanted
    ]


# ------------------------------------------------------------------ Qdrant
async def run_query(
    engine: SearchEngine,
    index: ActiveIndex,
    *,
    query,
    using: str | None,
    prefetch: list | None,
    flt: models.Filter,
    group_by: str,
    limit: int,
    group_size: int = 1,
) -> list[list[models.ScoredPoint]]:
    """Liste de groupes (chaque groupe = meilleurs points d'une œuvre / édition)."""
    common = {"collection_name": index.collection, "with_payload": True}
    if group_by == "none":
        response = await engine.qdrant.query_points(
            query=query,
            using=using,
            prefetch=prefetch,
            query_filter=flt if not prefetch else None,
            limit=limit,
            **common,
        )
        return [[p] for p in response.points]
    response = await engine.qdrant.query_points_groups(
        query=query,
        using=using,
        prefetch=prefetch,
        query_filter=flt if not prefetch else None,
        group_by=GROUP_KEYS[group_by],
        group_size=group_size,
        limit=limit,
        **common,
    )
    return [g.hits for g in response.groups]


# ------------------------------------------------------- construction des hits
async def passages(conn: Conn, points: list[models.ScoredPoint]) -> list[dict]:
    """Texte, chemin de section et page de chaque point, en une requête."""
    payloads = [p.payload or {} for p in points]
    rows = await (
        await conn.execute(
            """
            SELECT r.i, string_agg(s.text, E'\\n\\n' ORDER BY s.seq) AS text,
                   (SELECT sp.path FROM section_paths sp WHERE sp.section_id = r.section_id) AS path,
                   (SELECT pb.label FROM page_breaks pb WHERE pb.edition_id = r.edition_id
                    AND pb.char_offset <= min(s.char_start) ORDER BY pb.char_offset DESC LIMIT 1) AS page
            FROM unnest(%(e)s::uuid[], %(a)s::int[], %(b)s::int[], %(sec)s::uuid[])
                 WITH ORDINALITY AS r(edition_id, a, b, section_id, i)
            JOIN segments s ON s.edition_id = r.edition_id AND s.seq BETWEEN r.a AND r.b
            GROUP BY r.i, r.edition_id, r.section_id
            """,
            {
                "e": [p["edition_id"] for p in payloads],
                "a": [p["segment_start_seq"] for p in payloads],
                "b": [p["segment_end_seq"] for p in payloads],
                "sec": [p.get("section_id") for p in payloads],
            },
        )
    ).fetchall()
    by_i = {r["i"]: r for r in rows}
    return [by_i.get(i + 1, {"text": "", "path": None, "page": None}) for i in range(len(points))]


async def editions_meta(conn: Conn, edition_ids: list[str], principal: Principal, lang: str | None) -> dict:
    rows = await (
        await conn.execute(
            f"""
            SELECT e.id, e.title, e.language, e.is_original, e.access::text AS access, e.revision,
                   json_build_object('id', w.id, 'title', coalesce(wt.title, w.title),
                                     'first_published_year', w.first_published_year,
                                     'authors', {sql.authors_json("w")}) AS work
            FROM editions e JOIN works w ON w.id = e.work_id
            LEFT JOIN LATERAL ({sql.WORK_TITLE}) wt ON true
            WHERE e.id = ANY(%(ids)s::uuid[])
            """,
            {"ids": edition_ids, "lang": lang, "access": list(principal.visible_access)},
        )
    ).fetchall()
    return {str(r["id"]): r for r in rows}


async def translations(
    conn: Conn, hits: list[dict], show_languages: list[str], principal: Principal
) -> dict[tuple[int, str], dict | None]:
    """Passage correspondant dans chaque langue demandée, via les unités
    d'alignement (édition originale de préférence, sinon la mieux alignée)."""
    todo = [
        (i, h, lang)
        for i, h in enumerate(hits)
        for lang in show_languages
        if lang != h["edition"]["language"]
    ]
    if not todo:
        return {}
    rows = await (
        await conn.execute(
            f"""
            WITH r AS (
                SELECT * FROM unnest(%(i)s::int[], %(w)s::uuid[], %(e)s::uuid[], %(a)s::int[], %(b)s::int[],
                                     %(lang)s::text[]) AS r(i, work_id, edition_id, a, b, lang)
            ), t AS (
                SELECT r.*, (
                    SELECT e.id FROM editions e LEFT JOIN edition_alignments ea ON ea.edition_id = e.id
                    WHERE e.work_id = r.work_id AND e.id <> r.edition_id AND {sql.visible("e")}
                      AND (e.language = r.lang OR split_part(e.language, '-', 1) = split_part(r.lang, '-', 1))
                      AND coalesce(ea.status::text, 'pending') <> 'rejected'
                    ORDER BY (e.language = r.lang) DESC, e.is_original DESC,
                             ea.mean_score DESC NULLS LAST, e.id
                    LIMIT 1) AS target
                FROM r
            )
            SELECT t.i, t.lang, t.target AS edition_id, te.language, te.access::text AS access,
                   min(x.seq) AS seq_start, max(x.seq) AS seq_end,
                   string_agg(x.text, E'\\n\\n' ORDER BY x.seq) AS text
            FROM t JOIN editions te ON te.id = t.target
            LEFT JOIN LATERAL (
                SELECT DISTINCT ts.seq, ts.text
                FROM segments s
                JOIN segment_alignments a1 ON a1.segment_id = s.id
                JOIN segment_alignments a2 ON a2.unit_id = a1.unit_id
                JOIN segments ts ON ts.id = a2.segment_id
                WHERE s.edition_id = t.edition_id AND s.seq BETWEEN t.a AND t.b AND ts.edition_id = t.target
            ) x ON true
            GROUP BY t.i, t.lang, t.target, te.language, te.access
            """,
            {
                "i": [i for i, _, _ in todo],
                "w": [h["work"]["id"] for _, h, _ in todo],
                "e": [h["edition"]["id"] for _, h, _ in todo],
                "a": [h["passage"]["seq_start"] for _, h, _ in todo],
                "b": [h["passage"]["seq_end"] for _, h, _ in todo],
                "lang": [lang for _, _, lang in todo],
                "access": list(principal.visible_access),
            },
        )
    ).fetchall()
    out: dict[tuple[int, str], dict | None] = {(i, lang): None for i, _, lang in todo}
    for r in rows:
        if r["seq_start"] is None:
            continue
        text, truncated = r["text"], False
        if not principal.can_read_text(r["access"]):
            text, _ = excerpt(text, EXCERPT_MAX_CHARS)
            truncated = len(text) < len(r["text"])
        out[(r["i"], r["lang"])] = {
            "edition_id": r["edition_id"],
            "language": r["language"],
            "seq_start": r["seq_start"],
            "seq_end": r["seq_end"],
            "text": text,
            "truncated": truncated,
        }
    return out


async def build_hits(
    conn: Conn,
    groups: list[list[models.ScoredPoint]],
    principal: Principal,
    lang: str | None,
    *,
    query: str | None = None,
    mode: str = "theme",
    show_languages: list[str] | None = None,
) -> list[dict]:
    points = [p for g in groups for p in g]
    if not points:
        return []
    texts = await passages(conn, points)
    meta = await editions_meta(conn, list({p.payload["edition_id"] for p in points}), principal, lang)

    hits = []
    cursor = 0
    for group in groups:
        candidates = []
        for point in group:
            row = texts[cursor]
            cursor += 1
            edition = meta.get(point.payload["edition_id"])
            if edition is None or not principal.can_see(edition["access"]):
                continue  # payload Qdrant en retard sur la base
            text = row["text"]
            exact_spans = find_all(text, query) if query and mode == "quote" else []
            if exact_spans:
                highlights = exact_spans
            elif query and mode in ("quote", "words"):
                highlights = term_spans(text, query, edition["language"])
            else:
                highlights = []
            candidates.append((bool(exact_spans), point, edition, row, text, highlights))
        if not candidates:
            continue
        # Dans un groupe, une correspondance exacte passe devant un score meilleur.
        exact, point, edition, row, text, highlights = max(candidates, key=lambda c: (c[0], c[1].score))
        truncated = False
        if not principal.can_read_text(edition["access"]):
            short, shift = excerpt(text, EXCERPT_MAX_CHARS, highlights[0] if highlights else None)
            truncated = len(short) < len(text)
            highlights = [
                (a - shift, b - shift) for a, b in highlights if a >= shift and b <= shift + len(short)
            ]
            text = short
        work = edition["work"]
        hits.append(
            {
                "score": point.score,
                "exact": exact,
                "work": {
                    "id": work["id"],
                    "title": work["title"],
                    "authors": [{"id": a["id"], "name": a["name"]} for a in work["authors"]],
                    "first_published_year": work["first_published_year"],
                },
                "edition": {
                    k: edition[k] for k in ("id", "title", "language", "is_original", "access", "revision")
                },
                "passage": {
                    "seq_start": point.payload["segment_start_seq"],
                    "seq_end": point.payload["segment_end_seq"],
                    "section_path": row["path"] or [],
                    "page_label": row["page"],
                    "text": text,
                    "truncated": truncated,
                    "highlights": highlights,
                },
                "translations": {},
            }
        )
    if mode == "quote":
        hits.sort(key=lambda h: (not h["exact"], -h["score"]))
    if show_languages:
        found = await translations(conn, hits, show_languages, principal)
        for (i, language), passage in found.items():
            hits[i]["translations"][language] = passage
    return hits


# ------------------------------------------------------------------ endpoints
@router.post("/search", response_model=SearchResponse, summary="Recherche : thème, citation ou mots")
async def search(body: SearchRequest, request: Request, conn: Conn, principal: Reader, lang: Lang) -> dict:
    started = time.perf_counter()
    engine: SearchEngine = request.app.state.engine
    index = await engine.active()
    available = await corpus_languages(conn)
    languages = expand_languages(body.filters.languages, available)
    flt = qdrant_filter(body.filters, principal, languages)

    dense = sparse_vec = None
    if body.mode in ("theme", "quote"):
        dense = await engine.encode_query(body.q, body.mode)
    if body.mode in ("words", "quote"):
        # La langue de la requête est inconnue : racines dans chaque langue visée.
        ids: set[int] = set()
        for language in languages or available or [None]:
            ids.update(sparse.encode_query(body.q, language)[0])
        sparse_vec = models.SparseVector(indices=sorted(ids), values=[1.0] * len(ids))

    if body.mode == "theme":
        kwargs = {"query": dense, "using": qstore.DENSE, "prefetch": None}
    elif body.mode == "words":
        kwargs = {"query": sparse_vec, "using": qstore.SPARSE, "prefetch": None}
    else:
        kwargs = {
            "query": models.FusionQuery(fusion=models.Fusion.RRF),
            "using": None,
            "prefetch": [
                models.Prefetch(query=dense, using=qstore.DENSE, limit=CANDIDATES, filter=flt),
                models.Prefetch(query=sparse_vec, using=qstore.SPARSE, limit=CANDIDATES, filter=flt),
            ],
        }
    groups = await run_query(
        engine,
        index,
        flt=flt,
        group_by=body.group_by,
        limit=body.limit,
        group_size=3 if body.mode == "quote" else 1,
        **kwargs,
    )
    hits = await build_hits(
        conn, groups, principal, lang, query=body.q, mode=body.mode, show_languages=body.show_languages
    )
    return {
        "index": {
            "collection": index.collection,
            "dense_model": index.dense_model,
            "chunker_version": index.chunker_version,
        },
        "took_ms": round((time.perf_counter() - started) * 1000),
        "hits": hits,
    }


@router.post(
    "/similar", response_model=SearchResponse, summary="Passages proches par le sens d'un passage donné"
)
async def similar(body: SimilarRequest, request: Request, conn: Conn, principal: Reader, lang: Lang) -> dict:
    started = time.perf_counter()
    engine: SearchEngine = request.app.state.engine
    index = await engine.active()
    source = await (
        await conn.execute(
            "SELECT e.work_id, e.access::text AS access FROM editions e "
            "WHERE e.id = %s AND e.deleted_at IS NULL",
            (body.edition_id,),
        )
    ).fetchone()
    if source is None or not principal.can_see(source["access"]):
        raise not_found("Édition")
    # Chunk qui recouvre le plus le passage : son vecteur est déjà dans Qdrant.
    chunk = await (
        await conn.execute(
            """
            SELECT id FROM chunks
            WHERE edition_id = %(e)s AND chunker_version = %(v)s
              AND segment_start_seq <= %(b)s AND segment_end_seq >= %(a)s
            ORDER BY least(segment_end_seq, %(b)s) - greatest(segment_start_seq, %(a)s) DESC, chunk_index
            LIMIT 1
            """,
            {"e": body.edition_id, "v": index.chunker_version, "a": body.seq_start, "b": body.seq_end},
        )
    ).fetchone()
    if chunk is None:
        raise not_found("Passage indexé")
    exclude_key, exclude_value = (
        ("work_id", source["work_id"]) if body.exclude_same_work else ("edition_id", body.edition_id)
    )
    flt = qdrant_filter(
        body.filters,
        principal,
        expand_languages(body.filters.languages, await corpus_languages(conn)),
        extra_must_not=[
            models.FieldCondition(key=exclude_key, match=models.MatchValue(value=str(exclude_value)))
        ],
    )
    groups = await run_query(
        engine,
        index,
        query=str(chunk["id"]),
        using=qstore.DENSE,
        prefetch=None,
        flt=flt,
        group_by=body.group_by,
        limit=body.limit,
    )
    hits = await build_hits(conn, groups, principal, lang)
    return {
        "index": {
            "collection": index.collection,
            "dense_model": index.dense_model,
            "chunker_version": index.chunker_version,
        },
        "took_ms": round((time.perf_counter() - started) * 1000),
        "hits": hits,
    }
