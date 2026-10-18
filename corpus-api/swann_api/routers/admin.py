"""Administration : supervision, tâches, qualité, cohérence, index, historique,
flux temps réel (docs/console.md §8–9). Rôle corpus:admin."""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime
from typing import Annotated, Any
from uuid import UUID

import anyio.to_thread
import psycopg
from fastapi import APIRouter, Query, Request
from fastapi.responses import StreamingResponse
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
from qdrant_client import models

from swann_api.admin_schemas import (
    AckRequest,
    AdminIndex,
    Consistency,
    Event,
    JobCreate,
    JobDetail,
    JobSummary,
    Overview,
    QualityRow,
    SignalInfo,
)
from swann_api.auth import Admin
from swann_api.deps import Conn, Cursor, Limit, decode_cursor, encode_cursor
from swann_api.errors import Problem, not_found
from swann_api.schemas import Page

router = APIRouter(prefix="/admin", tags=["administration"])

WORKER_ALIVE_SECONDS = 60
BUCKET_SCAN_LIMIT = 10_000


def jsonb(value: Any) -> Jsonb:
    return Jsonb(value, dumps=lambda obj: json.dumps(obj, default=str, ensure_ascii=False))


async def create_job(
    conn,
    kind: str,
    title: str,
    params: dict,
    actor: str,
    *,
    work_id: UUID | None = None,
    edition_id: UUID | None = None,
    status: str = "queued",
    result: dict | None = None,
) -> UUID:
    """Crée une tâche pour le worker (notifiée sur le canal `jobs`)."""
    row = await (
        await conn.execute(
            """
            INSERT INTO jobs (kind, title, params, created_by_sub, work_id, edition_id, status, result)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s) RETURNING id
            """,
            (
                kind,
                title,
                jsonb(params),
                actor,
                work_id,
                edition_id,
                status,
                jsonb(result) if result else None,
            ),
        )
    ).fetchone()
    return row["id"]


# --------------------------------------------------------------- vue d'ensemble
JOB_SUMMARY = """
    j.id, j.kind::text AS kind, j.status::text AS status, j.title, j.progress, j.error,
    j.work_id, w.title AS work_title, j.edition_id, e.title AS edition_title,
    j.created_by_sub, j.created_at, j.started_at, j.finished_at, j.heartbeat_at, j.attempts,
    j.cancel_requested
"""
JOB_FROM = "FROM jobs j LEFT JOIN works w ON w.id = j.work_id LEFT JOIN editions e ON e.id = j.edition_id"


async def _postgres_info(conn) -> dict:
    try:
        row = await (
            await conn.execute(
                """
                SELECT current_setting('server_version') AS version,
                       pg_database_size(current_database()) AS size_bytes,
                       (SELECT count(*) FROM pg_stat_activity WHERE datname = current_database())
                           AS connections,
                       (SELECT count(*) FROM schema_migrations) AS migrations_applied,
                       (SELECT max(version) FROM schema_migrations) AS last_migration
                """
            )
        ).fetchone()
    except psycopg.Error as e:
        return {"status": "error", "detail": f"{type(e).__name__}: {e}"}
    return {"status": "ok", **row}


async def _qdrant_info(request: Request) -> dict:
    state = request.app.state
    alias = state.settings.qdrant_collection
    try:
        qc = state.qdrant
        aliases = (await qc.get_aliases()).aliases
        target = next((a.collection_name for a in aliases if a.alias_name == alias), None)
        names = [c.name for c in (await qc.get_collections()).collections]
        infos = await asyncio.gather(*(qc.get_collection(n) for n in names), return_exceptions=True)
        collections = []
        for name, info in zip(names, infos, strict=True):
            if isinstance(info, BaseException):
                collections.append({"name": name, "status": f"erreur : {type(info).__name__}"})
                continue
            collections.append(
                {
                    "name": name,
                    "status": str(info.status.value if hasattr(info.status, "value") else info.status),
                    "points": info.points_count,
                    "indexed_vectors": info.indexed_vectors_count,
                    "segments": info.segments_count,
                    "is_active": name == target,
                }
            )
    except Exception as e:  # noqa: BLE001 - rapporté tel quel
        return {"status": "error", "detail": f"{type(e).__name__}: {e}", "alias": alias}
    return {"status": "ok", "alias": alias, "alias_target": target, "collections": collections}


def _bucket_info(mc, name: str) -> dict:
    if not mc.bucket_exists(name):
        return {"name": name, "exists": False}
    n = size = 0
    truncated = False
    for obj in mc.list_objects(name, recursive=True):
        n += 1
        size += obj.size or 0
        if n >= BUCKET_SCAN_LIMIT:
            truncated = True
            break
    return {"name": name, "exists": True, "objects": n, "size_bytes": size, "truncated": truncated}


async def _storage_info(request: Request) -> dict:
    state = request.app.state
    if state.s3 is None:
        return {"status": "disabled"}
    try:
        buckets = [
            await anyio.to_thread.run_sync(_bucket_info, state.s3, name)
            for name in (state.settings.minio_books_bucket, state.settings.minio_inbox_bucket)
        ]
    except Exception as e:  # noqa: BLE001
        return {"status": "error", "detail": f"{type(e).__name__}: {e}"}
    return {"status": "ok", "buckets": buckets}


@router.get("/overview", response_model=Overview, summary="Santé, volumes, entonnoir, tâches, qualité")
async def overview(request: Request, conn: Conn, _: Admin) -> dict:
    postgres, qdrant, storage = await asyncio.gather(
        _postgres_info(conn), _qdrant_info(request), _storage_info(request)
    )
    workers = await (
        await conn.execute(
            f"""
            SELECT wk.*, j.title AS current_job_title,
                   (wk.stopped_at IS NULL AND wk.seen_at > now() - interval '{WORKER_ALIVE_SECONDS} seconds')
                       AS alive
            FROM workers wk LEFT JOIN jobs j ON j.id = wk.current_job_id
            WHERE wk.stopped_at IS NULL OR wk.stopped_at > now() - interval '1 day'
            ORDER BY alive DESC, wk.seen_at DESC LIMIT 10
            """
        )
    ).fetchall()
    stats = """
        WITH ed AS (
            SELECT e.id, e.language, e.deleted_at,
                   EXISTS (SELECT 1 FROM segments s WHERE s.edition_id = e.id) AS extracted,
                   EXISTS (SELECT 1 FROM edition_indexings ei JOIN vector_indexes v ON v.id = ei.index_id
                           WHERE ei.edition_id = e.id AND v.status = 'active') AS indexed,
                   (SELECT ea.status::text FROM edition_alignments ea WHERE ea.edition_id = e.id) AS alignment
            FROM editions e
        )
    """
    funnel = await (
        await conn.execute(
            stats
            + """
            SELECT (SELECT count(*) FROM works WHERE deleted_at IS NULL) AS works,
                   count(*) FILTER (WHERE deleted_at IS NULL) AS editions,
                   count(*) FILTER (WHERE deleted_at IS NULL AND extracted) AS extracted,
                   count(*) FILTER (WHERE deleted_at IS NULL AND indexed) AS indexed,
                   count(*) FILTER (WHERE deleted_at IS NULL AND alignment = 'reliable') AS aligned_reliable,
                   count(*) FILTER (WHERE deleted_at IS NULL AND alignment IS NOT NULL
                                    AND alignment <> 'reliable') AS aligned_other,
                   count(*) FILTER (WHERE deleted_at IS NOT NULL) AS trashed
            FROM ed
            """
        )
    ).fetchone()
    languages = await (
        await conn.execute(
            stats
            + """
            SELECT language, count(*) AS editions, count(*) FILTER (WHERE extracted) AS extracted,
                   count(*) FILTER (WHERE indexed) AS indexed,
                   count(*) FILTER (WHERE alignment = 'reliable') AS aligned_reliable
            FROM ed WHERE deleted_at IS NULL GROUP BY language ORDER BY count(*) DESC, language
            """
        )
    ).fetchall()
    jobs_by_status = await (
        await conn.execute(
            "SELECT status::text AS key, count(*) AS count FROM jobs "
            "WHERE created_at > now() - interval '7 days' OR status IN ('queued', 'running', 'needs_review') "
            "GROUP BY 1 ORDER BY 1"
        )
    ).fetchall()
    signals = await (
        await conn.execute(
            """
            SELECT s AS key, count(*) AS count
            FROM edition_quality q JOIN editions e ON e.id = q.edition_id, unnest(q.signals) s
            WHERE e.deleted_at IS NULL
              AND NOT EXISTS (SELECT 1 FROM quality_acks a WHERE a.edition_id = q.edition_id AND a.signal = s)
            GROUP BY s ORDER BY count(*) DESC
            """
        )
    ).fetchall()
    mean = await (
        await conn.execute(
            "SELECT avg(q.score)::float AS mean FROM edition_quality q "
            "JOIN editions e ON e.id = q.edition_id "
            "WHERE e.deleted_at IS NULL"
        )
    ).fetchone()
    failures = await (
        await conn.execute(
            f"SELECT {JOB_SUMMARY} {JOB_FROM} WHERE j.status = 'failed' ORDER BY j.finished_at DESC LIMIT 5"
        )
    ).fetchall()
    running = await (
        await conn.execute(
            f"SELECT {JOB_SUMMARY} {JOB_FROM} WHERE j.status IN ('running', 'queued', 'needs_review') "
            "ORDER BY j.status = 'running' DESC, j.created_at LIMIT 20"
        )
    ).fetchall()
    return {
        "generated_at": datetime.now(UTC),
        "postgres": postgres,
        "qdrant": qdrant,
        "storage": storage,
        "workers": workers,
        "funnel": funnel,
        "languages": languages,
        "jobs_by_status": jobs_by_status,
        "quality_signals": signals,
        "quality_mean_score": mean["mean"],
        "recent_failures": failures,
        "running": running,
    }


# --------------------------------------------------------------------- tâches
@router.get("/jobs", response_model=Page[JobSummary], summary="Tâches, les plus récentes d'abord")
async def list_jobs(
    conn: Conn,
    _: Admin,
    limit: Limit = 50,
    cursor: Cursor = None,
    status: Annotated[list[str] | None, Query()] = None,
    kind: Annotated[list[str] | None, Query()] = None,
    work_id: UUID | None = None,
    edition_id: UUID | None = None,
) -> dict:
    after = decode_cursor(cursor)
    rows = await (
        await conn.execute(
            f"""
            SELECT {JOB_SUMMARY} {JOB_FROM}
            WHERE (%(status)s::text[] IS NULL OR j.status::text = ANY(%(status)s))
              AND (%(kind)s::text[] IS NULL OR j.kind::text = ANY(%(kind)s))
              AND (%(work)s::uuid IS NULL OR j.work_id = %(work)s)
              AND (%(edition)s::uuid IS NULL OR j.edition_id = %(edition)s)
              AND (%(at)s::timestamptz IS NULL OR (j.created_at, j.id) < (%(at)s, %(id)s::uuid))
            ORDER BY j.created_at DESC, j.id DESC LIMIT %(limit)s
            """,
            {
                "status": status,
                "kind": kind,
                "work": work_id,
                "edition": edition_id,
                "at": after and after.get("at"),
                "id": after and after.get("id"),
                "limit": limit + 1,
            },
        )
    ).fetchall()
    next_cursor = None
    if len(rows) > limit:
        rows = rows[:limit]
        next_cursor = encode_cursor({"at": rows[-1]["created_at"].isoformat(), "id": str(rows[-1]["id"])})
    return {"items": rows, "next_cursor": next_cursor}


async def load_job(conn, job_id: UUID) -> dict:
    row = await (
        await conn.execute(
            f"SELECT {JOB_SUMMARY}, j.params, j.result, j.parent_id {JOB_FROM} WHERE j.id = %s", (job_id,)
        )
    ).fetchone()
    if row is None:
        raise not_found("Tâche")
    row["ingestions"] = await (
        await conn.execute(
            "SELECT id, stage, status::text AS status, source_file, edition_id, n_segments, n_chunks, error, "
            "started_at, finished_at FROM ingestions WHERE job_id = %s ORDER BY started_at",
            (job_id,),
        )
    ).fetchall()
    return row


@router.get("/jobs/{job_id}", response_model=JobDetail, summary="Détail d'une tâche")
async def get_job(job_id: UUID, conn: Conn, _: Admin) -> dict:
    return await load_job(conn, job_id)


@router.post("/jobs", response_model=JobDetail, status_code=201, summary="Lancer une tâche de maintenance")
async def post_job(body: JobCreate, conn: Conn, principal: Admin) -> dict:
    titles = {
        "import_books": "Import de books/",
        "quality": "Recalcul de la qualité",
        "align": "Réalignement",
        "purge": "Purge de la corbeille",
        "sync_payload": "Fiches → Qdrant",
        "reprocess": "Retraitement",
    }
    params = dict(body.params)
    work_id = UUID(params["work_id"]) if params.get("work_id") else None
    edition_id = UUID(params["edition_id"]) if params.get("edition_id") else None
    if body.kind == "align" and work_id is None:
        raise Problem(400, "params", "`params.work_id` requis pour un réalignement.")
    if body.kind == "reprocess" and edition_id is None:
        raise Problem(400, "params", "`params.edition_id` requis pour un retraitement.")
    params["actor"] = principal.sub
    job_id = await create_job(
        conn,
        body.kind,
        body.title or titles[body.kind],
        params,
        principal.sub,
        work_id=work_id,
        edition_id=edition_id,
    )
    return await load_job(conn, job_id)


@router.post("/jobs/{job_id}/cancel", response_model=JobDetail, summary="Annuler une tâche")
async def cancel_job(job_id: UUID, conn: Conn, _: Admin) -> dict:
    await conn.execute(
        """
        UPDATE jobs SET
            cancel_requested = true,
            status = CASE WHEN status IN ('queued', 'needs_review') THEN 'cancelled' ELSE status END,
            finished_at = CASE WHEN status IN ('queued', 'needs_review') THEN now() ELSE finished_at END
        WHERE id = %s AND status IN ('queued', 'running', 'needs_review')
        """,
        (job_id,),
    )
    return await load_job(conn, job_id)


@router.post("/jobs/{job_id}/retry", response_model=JobDetail, summary="Relancer une tâche échouée")
async def retry_job(job_id: UUID, conn: Conn, _: Admin) -> dict:
    row = await (
        await conn.execute(
            """
            UPDATE jobs SET status = 'queued', error = NULL, finished_at = NULL, attempts = 0,
                   cancel_requested = false, progress = '{}'
            WHERE id = %s AND status IN ('failed', 'cancelled') AND kind <> 'cli'
            RETURNING id
            """,
            (job_id,),
        )
    ).fetchone()
    if row is None:
        raise Problem(409, "not-retryable", "Seule une tâche échouée ou annulée (hors CLI) se relance.")
    return await load_job(conn, job_id)


# -------------------------------------------------------------------- qualité
QUALITY_SORTS = {
    "score": "q.score ASC NULLS FIRST, e.source_file",
    "-score": "q.score DESC NULLS LAST, e.source_file",
    "title": "e.title, e.id",
    "source_file": "e.source_file",
    "computed_at": "q.computed_at DESC NULLS LAST, e.source_file",
}


@router.get("/quality", response_model=Page[QualityRow], summary="Éditions et signaux de qualité")
async def list_quality(
    conn: Conn,
    _: Admin,
    limit: Annotated[int, Query(ge=1, le=500)] = 100,
    cursor: Cursor = None,
    signal: Annotated[list[str] | None, Query(description="Éditions qui ont l'un de ces signaux.")] = None,
    language: str | None = None,
    max_score: Annotated[int | None, Query(ge=0, le=100)] = None,
    q: Annotated[str | None, Query(description="Titre, œuvre ou fichier.")] = None,
    include_acked: bool = False,
    trashed: bool = False,
    sort: Annotated[str, Query(pattern="^-?(score|title|source_file|computed_at)$")] = "score",
) -> dict:
    offset = int((decode_cursor(cursor) or {}).get("offset", 0))
    rows = await (
        await conn.execute(
            f"""
            SELECT e.id AS edition_id, e.title, e.language, e.is_original, e.access::text AS access,
                   e.source_file, e.deleted_at, w.id AS work_id, w.title AS work_title,
                   coalesce(array(SELECT p.display_name FROM work_authors wa
                                  JOIN persons p ON p.id = wa.person_id
                                  WHERE wa.work_id = w.id ORDER BY wa.position), '{{}}') AS authors,
                   q.score, coalesce(q.signals, '{{}}') AS signals,
                   coalesce(array(SELECT a.signal FROM quality_acks a WHERE a.edition_id = e.id), '{{}}')
                       AS acked,
                   coalesce(q.metrics -> 'signal_details', '{{}}') AS signal_details,
                   coalesce(q.metrics - 'signal_details', '{{}}') AS metrics,
                   q.structure_method, q.detected_language,
                   coalesce(q.parse_warnings, '[]') AS parse_warnings,
                   q.computed_at
            FROM editions e JOIN works w ON w.id = e.work_id
            LEFT JOIN edition_quality q ON q.edition_id = e.id
            WHERE (e.deleted_at IS NOT NULL) = %(trashed)s
              AND (%(language)s::text IS NULL OR e.language = %(language)s)
              AND (%(max_score)s::int IS NULL OR q.score <= %(max_score)s)
              AND (%(q)s::text IS NULL OR e.title ILIKE %(like)s OR w.title ILIKE %(like)s
                   OR e.source_file ILIKE %(like)s)
              AND (%(signal)s::text[] IS NULL OR EXISTS (
                    SELECT 1 FROM unnest(q.signals) s WHERE s = ANY(%(signal)s)
                      AND (%(include_acked)s OR NOT EXISTS (
                            SELECT 1 FROM quality_acks a WHERE a.edition_id = e.id AND a.signal = s))))
            ORDER BY {QUALITY_SORTS[sort]}
            LIMIT %(limit)s OFFSET %(offset)s
            """,
            {
                "trashed": trashed,
                "language": language,
                "max_score": max_score,
                "q": q,
                "like": f"%{q}%" if q else None,
                "signal": signal,
                "include_acked": include_acked,
                "limit": limit + 1,
                "offset": offset,
            },
        )
    ).fetchall()
    next_cursor = encode_cursor({"offset": offset + limit}) if len(rows) > limit else None
    return {"items": rows[:limit], "next_cursor": next_cursor}


@router.get("/quality/signals", response_model=list[SignalInfo], summary="Nombre d'éditions par signal")
async def quality_signals(conn: Conn, _: Admin) -> list:
    return await (
        await conn.execute(
            """
            SELECT s AS code, count(*) AS count,
                   count(*) FILTER (WHERE EXISTS (
                       SELECT 1 FROM quality_acks a WHERE a.edition_id = q.edition_id AND a.signal = s
                   )) AS acked
            FROM edition_quality q JOIN editions e ON e.id = q.edition_id, unnest(q.signals) s
            WHERE e.deleted_at IS NULL GROUP BY s ORDER BY count(*) DESC
            """
        )
    ).fetchall()


async def _requeue_quality(conn, edition_id: UUID, actor: str) -> None:
    await create_job(
        conn,
        "quality",
        "Qualité (signal accepté)",
        {"edition_id": str(edition_id)},
        actor,
        edition_id=edition_id,
    )


@router.post("/quality/{edition_id}/acks", status_code=204, summary="Accepter un signal (ne remonte plus)")
async def ack_signal(edition_id: UUID, body: AckRequest, conn: Conn, principal: Admin) -> None:
    exists = await (await conn.execute("SELECT 1 FROM editions WHERE id = %s", (edition_id,))).fetchone()
    if exists is None:
        raise not_found("Édition")
    await conn.execute(
        """
        INSERT INTO quality_acks (edition_id, signal, acked_by_sub, comment) VALUES (%s, %s, %s, %s)
        ON CONFLICT (edition_id, signal) DO UPDATE SET comment = EXCLUDED.comment,
            acked_by_sub = EXCLUDED.acked_by_sub, acked_at = now()
        """,
        (edition_id, body.signal, principal.sub, body.comment),
    )
    await _requeue_quality(conn, edition_id, principal.sub)


@router.delete("/quality/{edition_id}/acks/{signal}", status_code=204, summary="Retirer l'acceptation")
async def unack_signal(edition_id: UUID, signal: str, conn: Conn, principal: Admin) -> None:
    await conn.execute("DELETE FROM quality_acks WHERE edition_id = %s AND signal = %s", (edition_id, signal))
    await _requeue_quality(conn, edition_id, principal.sub)


# ------------------------------------------------------------------ cohérence
@router.get("/consistency", response_model=Consistency, summary="Écarts Postgres / Qdrant / MinIO")
async def consistency(
    request: Request,
    conn: Conn,
    _: Admin,
    only_problems: bool = True,
    limit: Annotated[int, Query(ge=1, le=2000)] = 500,
) -> dict:
    state = request.app.state
    active = await (
        await conn.execute(
            "SELECT id, collection, chunker_version FROM vector_indexes WHERE status = 'active' LIMIT 1"
        )
    ).fetchone()
    rows = await (
        await conn.execute(
            """
            SELECT e.id AS edition_id, e.title, e.language, e.source_file, e.epub_object_key AS epub_key,
                   (SELECT count(*) FROM chunks c WHERE c.edition_id = e.id
                      AND c.chunker_version = %(chunker)s) AS chunks,
                   (SELECT ei.n_points FROM edition_indexings ei
                     WHERE ei.edition_id = e.id AND ei.index_id = %(index)s) AS indexed_points
            FROM editions e
            WHERE e.deleted_at IS NULL AND EXISTS (SELECT 1 FROM segments s WHERE s.edition_id = e.id)
            ORDER BY e.source_file LIMIT %(limit)s
            """,
            {
                "chunker": active and active["chunker_version"],
                "index": active and active["id"],
                "limit": limit,
            },
        )
    ).fetchall()
    sem = asyncio.Semaphore(8)

    async def qdrant_count(edition_id: UUID) -> int | None:
        if active is None:
            return None
        async with sem:
            try:
                res = await state.qdrant.count(
                    active["collection"],
                    count_filter=models.Filter(
                        must=[
                            models.FieldCondition(
                                key="edition_id", match=models.MatchValue(value=str(edition_id))
                            )
                        ]
                    ),
                    exact=True,
                )
            except Exception:  # noqa: BLE001
                return None
            return res.count

    def epub_present(key: str | None) -> bool | None:
        if state.s3 is None or key is None:
            return None
        from swann_core import s3

        return s3.exists(state.s3, state.settings.minio_books_bucket, key)

    counts = await asyncio.gather(*(qdrant_count(r["edition_id"]) for r in rows))
    out = []
    for r, count in zip(rows, counts, strict=True):
        r["qdrant_points"] = count
        r["epub_present"] = await anyio.to_thread.run_sync(epub_present, r["epub_key"])
        problems = []
        if active is not None:
            if r["indexed_points"] is None:
                problems.append("absente de l'index actif")
            elif r["qdrant_points"] is not None and r["qdrant_points"] != r["indexed_points"]:
                problems.append(f"Qdrant : {r['qdrant_points']} points, {r['indexed_points']} attendus")
            if r["indexed_points"] is not None and r["chunks"] != r["indexed_points"]:
                problems.append(f"{r['chunks']} chunks pour {r['indexed_points']} points indexés")
        if r["epub_key"] is None:
            problems.append("EPUB jamais stocké")
        elif r["epub_present"] is False:
            problems.append("EPUB absent de MinIO")
        r["problems"] = problems
        if problems or not only_problems:
            out.append(r)
    return {
        "active_collection": active and active["collection"],
        "checked": len(rows),
        "with_problems": sum(1 for r in rows if r["problems"]),
        "rows": out,
    }


# ----------------------------------------------------------------------- index
@router.get("/indexes", response_model=list[AdminIndex], summary="Index vectoriels et avancement")
async def list_indexes(conn: Conn, _: Admin) -> list:
    return await (
        await conn.execute(
            """
            SELECT v.id, v.collection, v.dense_model, v.dense_dim, v.sparse_model, v.chunker_version,
                   v.status::text AS status, v.created_at,
                   (SELECT count(*) FROM edition_indexings ei WHERE ei.index_id = v.id) AS editions,
                   (SELECT coalesce(sum(n_points), 0) FROM edition_indexings ei WHERE ei.index_id = v.id)
                       AS points,
                   (SELECT count(*) FROM editions e WHERE e.deleted_at IS NULL
                      AND EXISTS (SELECT 1 FROM segments s WHERE s.edition_id = e.id)
                      AND NOT EXISTS (SELECT 1 FROM edition_indexings ei
                                      WHERE ei.edition_id = e.id AND ei.index_id = v.id)) AS pending
            FROM vector_indexes v ORDER BY v.created_at
            """
        )
    ).fetchall()


@router.post("/indexes/{collection}/activate", response_model=list[AdminIndex], summary="Activer un index")
async def activate_index(collection: str, request: Request, conn: Conn, principal: Admin) -> list:
    index = await (
        await conn.execute(
            "SELECT id, dense_model, chunker_version FROM vector_indexes WHERE collection = %s", (collection,)
        )
    ).fetchone()
    if index is None:
        raise not_found("Index")
    state = request.app.state
    alias = state.settings.qdrant_collection
    aliases = (await state.qdrant.get_aliases()).aliases
    ops: list = []
    if any(a.alias_name == alias for a in aliases):
        ops.append(models.DeleteAliasOperation(delete_alias=models.DeleteAlias(alias_name=alias)))
    ops.append(
        models.CreateAliasOperation(
            create_alias=models.CreateAlias(collection_name=collection, alias_name=alias)
        )
    )
    await state.qdrant.update_collection_aliases(change_aliases_operations=ops)
    async with conn.transaction():
        await conn.execute(
            "UPDATE vector_indexes SET status = 'retired' WHERE status = 'active' AND id <> %s",
            (index["id"],),
        )
        await conn.execute("UPDATE vector_indexes SET status = 'active' WHERE id = %s", (index["id"],))
        await conn.execute(
            "SELECT corpus_emit('index.activated', NULL, NULL, %s, %s)",
            (
                jsonb(
                    {
                        "collection": collection,
                        "dense_model": index["dense_model"],
                        "chunker_version": index["chunker_version"],
                    }
                ),
                principal.sub,
            ),
        )
    await state.engine.refresh(force=True)
    return await list_indexes(conn, principal)


# ------------------------------------------------------------------ historique
@router.get("/events", response_model=Page[Event], summary="Historique des modifications du corpus")
async def list_events(
    conn: Conn,
    _: Admin,
    limit: Limit = 50,
    cursor: Cursor = None,
    work_id: UUID | None = None,
    edition_id: UUID | None = None,
    actor: str | None = None,
) -> dict:
    before = (decode_cursor(cursor) or {}).get("before")
    rows = await (
        await conn.execute(
            """
            SELECT id, at, type, work_id, edition_id, actor, data FROM corpus_events
            WHERE (%(work)s::uuid IS NULL OR work_id = %(work)s)
              AND (%(edition)s::uuid IS NULL OR edition_id = %(edition)s)
              AND (%(actor)s::text IS NULL OR actor = %(actor)s)
              AND (%(before)s::bigint IS NULL OR id < %(before)s)
            ORDER BY id DESC LIMIT %(limit)s
            """,
            {"work": work_id, "edition": edition_id, "actor": actor, "before": before, "limit": limit + 1},
        )
    ).fetchall()
    next_cursor = encode_cursor({"before": rows[limit - 1]["id"]}) if len(rows) > limit else None
    return {"items": rows[:limit], "next_cursor": next_cursor}


# ------------------------------------------------------------------ temps réel
@router.get(
    "/stream",
    summary="Flux SSE : tâches (`job`) et modifications du corpus (`corpus`)",
    response_class=StreamingResponse,
)
async def stream(request: Request, _: Admin) -> StreamingResponse:
    settings = request.app.state.settings

    async def events():
        conn = await psycopg.AsyncConnection.connect(
            settings.database_url, autocommit=True, row_factory=dict_row
        )
        try:
            await conn.execute("LISTEN jobs")
            await conn.execute("LISTEN corpus_events")
            yield "retry: 3000\n\n"
            while not await request.is_disconnected():
                got = False
                async for n in conn.notifies(timeout=15, stop_after=50):
                    got = True
                    if n.channel == "jobs":
                        yield f"event: job\ndata: {n.payload}\n\n"
                    else:
                        yield f'event: corpus\ndata: {{"id": {int(n.payload)}}}\n\n'
                if not got:
                    yield ": ping\n\n"
        finally:
            await conn.close()

    return StreamingResponse(
        events(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
