"""Atelier d'alignement (docs/console.md §6).

Complète les endpoints publics (`/alignment/sample`, verdicts, liens) :
liste des éditions à traiter, carte des scores par section, vue de travail
côte à côte (segments cibles + segments de référence et leurs unités),
remplacement des liens d'un segment, statut fixé à la main, précision
mesurée par échantillons. Rôle corpus:review (jeton utilisateur).
"""

from __future__ import annotations

import math
from datetime import datetime
from typing import Annotated, Literal
from uuid import UUID

from fastapi import APIRouter, Query
from pydantic import BaseModel, Field

from swann_api.auth import Reviewer
from swann_api.deps import Conn
from swann_api.errors import Problem, not_found
from swann_api.routers.admin import jsonb

router = APIRouter(prefix="/admin/alignment", tags=["administration"])

LOW_SCORE = 0.50  # même seuil que l'aligneur (swann_ingest.pipeline.align)
MAX_WINDOW = 200


class AlignedEdition(BaseModel):
    edition_id: UUID
    title: str
    language: str
    work_id: UUID
    work_title: str
    reference_edition_id: UUID | None
    reference_language: str | None
    status: str
    method: str
    n_segments: int
    n_aligned_segments: int
    aligned_ratio: float | None
    mean_score: float | None
    low_score_ratio: float | None
    manual_links: int
    reviews: int
    aligned_at: datetime
    updated_at: datetime


class SectionScore(BaseModel):
    section_id: UUID
    seq: int
    label: str | None
    title: str | None
    kind: str
    seq_start: int | None
    seq_end: int | None
    n_segments: int
    n_aligned: int
    mean_score: float | None
    n_low: int
    reference_section_id: UUID | None
    reference_title: str | None


class AlignmentMap(BaseModel):
    edition_id: UUID
    reference_edition_id: UUID | None
    status: str | None
    sections: list[SectionScore]


class Link(BaseModel):
    unit_id: UUID
    reference_seq: int | None
    score: float | None
    method: str
    created_by_sub: str | None


class WorkbenchTarget(BaseModel):
    seq: int
    kind: str
    text: str
    section_id: UUID | None
    links: list[Link]
    best_score: float | None
    manual: bool


class WorkbenchReference(BaseModel):
    seq: int
    kind: str
    text: str
    unit_id: UUID | None


class Workbench(BaseModel):
    edition_id: UUID
    language: str
    reference_edition_id: UUID
    reference_language: str
    status: str | None
    target: list[WorkbenchTarget]
    reference: list[WorkbenchReference]
    has_more: bool


class NextLow(BaseModel):
    seq: int | None
    score: float | None
    reason: Literal["low_score", "unaligned"] | None


class ReplaceLinks(BaseModel):
    edition_id: UUID
    seq: int
    reference_seqs: list[int] = Field(
        description="Segments de référence liés ; vide = segment sans correspondant."
    )


class StatusRequest(BaseModel):
    status: Literal["pending", "reliable", "doubtful", "rejected"]
    comment: str | None = None


class Precision(BaseModel):
    method: str
    reviews: int
    correct: int
    partial: int
    incorrect: int
    precision: float | None = Field(description="(corrects + ½ partiels) / vérifiés")
    wilson_low: float | None = Field(description="Borne basse à 95 % de la part de liens corrects")
    wilson_high: float | None


# ----------------------------------------------------------------- statistiques
async def recount(conn, edition_id: UUID) -> dict | None:
    """Recalcule la qualité d'une édition alignée d'après ses liens actuels
    (après des corrections à la main)."""
    return await (
        await conn.execute(
            """
            WITH tgt AS (
                SELECT s.id FROM segments s JOIN sections sec ON sec.id = s.section_id
                WHERE s.edition_id = %(e)s AND sec.matter = 'body' AND s.kind <> 'note'
            ), best AS (
                SELECT sa.segment_id, max(sa.score) AS score, bool_or(sa.method = 'manual') AS manual
                FROM segment_alignments sa JOIN tgt ON tgt.id = sa.segment_id GROUP BY sa.segment_id
            )
            UPDATE edition_alignments ea SET
                n_segments = (SELECT count(*) FROM tgt),
                n_aligned_segments = (SELECT count(*) FROM best),
                mean_score = (SELECT avg(score) FROM best WHERE score IS NOT NULL),
                low_score_ratio = coalesce(
                    (SELECT avg((score < %(low)s)::int) FROM best WHERE score IS NOT NULL AND NOT manual), 0
                )
            WHERE ea.edition_id = %(e)s AND ea.reference_edition_id IS NOT NULL
            RETURNING ea.n_segments, ea.n_aligned_segments, ea.aligned_ratio, ea.mean_score,
                ea.low_score_ratio
            """,
            {"e": edition_id, "low": LOW_SCORE},
        )
    ).fetchone()


def wilson(successes: int, n: int, z: float = 1.96) -> tuple[float | None, float | None]:
    if n == 0:
        return None, None
    p = successes / n
    denom = 1 + z * z / n
    centre = (p + z * z / (2 * n)) / denom
    margin = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / denom
    return round(max(0.0, centre - margin), 4), round(min(1.0, centre + margin), 4)


# --------------------------------------------------------------------- listes
@router.get("/editions", response_model=list[AlignedEdition], summary="Éditions alignées, à traiter d'abord")
async def aligned_editions(
    conn: Conn,
    _: Reviewer,
    status: Annotated[list[str] | None, Query()] = None,
    work_id: UUID | None = None,
    sort: Annotated[str, Query(pattern="^(todo|ratio|score|recent|title)$")] = "todo",
) -> list:
    order = {
        "todo": "(ea.status = 'doubtful') DESC, (ea.status = 'pending') DESC, ea.aligned_ratio NULLS FIRST",
        "ratio": "ea.aligned_ratio NULLS FIRST",
        "score": "ea.mean_score NULLS FIRST",
        "recent": "ea.updated_at DESC",
        "title": "w.title, e.language",
    }[sort]
    return await (
        await conn.execute(
            f"""
            SELECT e.id AS edition_id, e.title, e.language, w.id AS work_id, w.title AS work_title,
                   ea.reference_edition_id, r.language AS reference_language, ea.status::text AS status,
                   ea.method, ea.n_segments, ea.n_aligned_segments, ea.aligned_ratio, ea.mean_score,
                   ea.low_score_ratio, ea.aligned_at, ea.updated_at,
                   (SELECT count(*) FROM segment_alignments sa JOIN segments s ON s.id = sa.segment_id
                     WHERE s.edition_id = e.id AND sa.method = 'manual') AS manual_links,
                   (SELECT count(*) FROM alignment_reviews ar JOIN segments s ON s.id = ar.segment_id
                     WHERE s.edition_id = e.id) AS reviews
            FROM edition_alignments ea
            JOIN editions e ON e.id = ea.edition_id JOIN works w ON w.id = e.work_id
            LEFT JOIN editions r ON r.id = ea.reference_edition_id
            WHERE ea.reference_edition_id IS NOT NULL AND e.deleted_at IS NULL
              AND (%(status)s::text[] IS NULL OR ea.status::text = ANY(%(status)s))
              AND (%(work)s::uuid IS NULL OR e.work_id = %(work)s)
            ORDER BY {order}, e.id
            """,
            {"status": status, "work": work_id},
        )
    ).fetchall()


@router.get("/editions/{edition_id}/map", response_model=AlignmentMap, summary="Scores par section")
async def alignment_map(edition_id: UUID, conn: Conn, _: Reviewer) -> dict:
    ea = await (
        await conn.execute(
            "SELECT reference_edition_id, status::text AS status FROM edition_alignments "
            "WHERE edition_id = %s",
            (edition_id,),
        )
    ).fetchone()
    sections = await (
        await conn.execute(
            """
            WITH seg AS (
                SELECT s.section_id, s.id, (SELECT max(sa.score) FROM segment_alignments sa
                                            WHERE sa.segment_id = s.id) AS score,
                       EXISTS (SELECT 1 FROM segment_alignments sa WHERE sa.segment_id = s.id) AS aligned
                FROM segments s WHERE s.edition_id = %(e)s AND s.kind <> 'note'
            )
            SELECT sec.id AS section_id, sec.seq, sec.label, sec.title, sec.kind::text AS kind,
                   r.seq_start, r.seq_end, count(seg.id) AS n_segments,
                   count(seg.id) FILTER (WHERE seg.aligned) AS n_aligned,
                   avg(seg.score) AS mean_score,
                   count(seg.id) FILTER (WHERE seg.score < %(low)s) AS n_low,
                   sec.reference_section_id,
                   coalesce(rs.title, rs.label) AS reference_title
            FROM sections sec
            JOIN seg ON seg.section_id = sec.id
            LEFT JOIN section_ranges(%(e)s) r ON r.section_id = sec.id
            LEFT JOIN sections rs ON rs.id = sec.reference_section_id
            WHERE sec.edition_id = %(e)s AND sec.matter = 'body'
            GROUP BY sec.id, r.seq_start, r.seq_end, rs.title, rs.label
            ORDER BY sec.seq
            """,
            {"e": edition_id, "low": LOW_SCORE},
        )
    ).fetchall()
    return {
        "edition_id": edition_id,
        "reference_edition_id": ea and ea["reference_edition_id"],
        "status": ea and ea["status"],
        "sections": sections,
    }


# ---------------------------------------------------------------- vue de travail
@router.get("/editions/{edition_id}/workbench", response_model=Workbench, summary="Vue côte à côte éditable")
async def workbench(
    edition_id: UUID,
    conn: Conn,
    _: Reviewer,
    from_seq: Annotated[int, Query(ge=0)] = 0,
    limit: Annotated[int, Query(ge=1, le=MAX_WINDOW)] = 40,
) -> dict:
    head = await (
        await conn.execute(
            """
            SELECT e.language, ea.reference_edition_id, ea.status::text AS status, r.language AS ref_language
            FROM editions e
            LEFT JOIN edition_alignments ea ON ea.edition_id = e.id
            LEFT JOIN editions r ON r.id = ea.reference_edition_id
            WHERE e.id = %s
            """,
            (edition_id,),
        )
    ).fetchone()
    if head is None:
        raise not_found("Édition")
    if head["reference_edition_id"] is None:
        raise Problem(409, "not-aligned", "Édition non alignée, ou édition de référence de son œuvre.")
    ref_id = head["reference_edition_id"]
    rows = await (
        await conn.execute(
            """
            SELECT s.seq, s.kind::text AS kind, s.text, s.section_id,
                   coalesce((SELECT json_agg(json_build_object(
                                 'unit_id', sa.unit_id, 'score', sa.score, 'method', sa.method,
                                 'created_by_sub', sa.created_by_sub,
                                 'reference_seq', (SELECT rs.seq FROM segment_alignments ra
                                                   JOIN segments rs ON rs.id = ra.segment_id
                                                   WHERE ra.unit_id = sa.unit_id AND ra.method = 'reference'
                                                   ORDER BY rs.seq LIMIT 1))
                             ORDER BY sa.unit_id)
                             FROM segment_alignments sa WHERE sa.segment_id = s.id), '[]') AS links
            FROM segments s
            WHERE s.edition_id = %s AND s.seq >= %s AND s.kind <> 'note'
            ORDER BY s.seq LIMIT %s
            """,
            (edition_id, from_seq, limit + 1),
        )
    ).fetchall()
    has_more = len(rows) > limit
    rows = rows[:limit]
    for r in rows:
        r["links"].sort(key=lambda link: (link["reference_seq"] is None, link["reference_seq"]))
        scores = [link["score"] for link in r["links"] if link["score"] is not None]
        r["best_score"] = max(scores) if scores else None
        r["manual"] = any(link["method"] == "manual" for link in r["links"])
    ref_seqs = [
        link["reference_seq"] for r in rows for link in r["links"] if link["reference_seq"] is not None
    ]
    if ref_seqs:
        lo, hi = min(ref_seqs), max(ref_seqs)
    else:
        # Rien d'aligné dans la fenêtre : même position relative dans la référence.
        pos = await (
            await conn.execute(
                "SELECT (SELECT max(seq) FROM segments WHERE edition_id = %s) AS n_t, "
                "(SELECT max(seq) FROM segments WHERE edition_id = %s) AS n_r",
                (edition_id, ref_id),
            )
        ).fetchone()
        lo = int(from_seq * (pos["n_r"] or 0) / max(pos["n_t"] or 1, 1))
        hi = lo + limit
    margin = 8
    reference = await (
        await conn.execute(
            """
            SELECT s.seq, s.kind::text AS kind, s.text,
                   (SELECT sa.unit_id FROM segment_alignments sa
                    WHERE sa.segment_id = s.id AND sa.method = 'reference' LIMIT 1) AS unit_id
            FROM segments s
            WHERE s.edition_id = %s AND s.seq BETWEEN %s AND %s AND s.kind <> 'note'
            ORDER BY s.seq LIMIT %s
            """,
            (ref_id, max(0, lo - margin), hi + margin, MAX_WINDOW * 2),
        )
    ).fetchall()
    return {
        "edition_id": edition_id,
        "language": head["language"],
        "reference_edition_id": ref_id,
        "reference_language": head["ref_language"],
        "status": head["status"],
        "target": rows,
        "reference": reference,
        "has_more": has_more,
    }


@router.get(
    "/editions/{edition_id}/next", response_model=NextLow, summary="Prochain lien douteux ou manquant"
)
async def next_low(
    edition_id: UUID,
    conn: Conn,
    _: Reviewer,
    after_seq: Annotated[int, Query(ge=-1)] = -1,
    threshold: Annotated[float, Query(ge=0, le=1)] = LOW_SCORE,
) -> dict:
    row = await (
        await conn.execute(
            """
            SELECT s.seq, b.score, b.n IS NULL AS unaligned
            FROM segments s JOIN sections sec ON sec.id = s.section_id
            LEFT JOIN LATERAL (
                SELECT count(*) AS n, max(sa.score) AS score, bool_or(sa.method = 'manual') AS manual
                FROM segment_alignments sa WHERE sa.segment_id = s.id HAVING count(*) > 0
            ) b ON true
            WHERE s.edition_id = %(e)s AND s.seq > %(after)s AND sec.matter = 'body'
              AND s.kind NOT IN ('note', 'heading')
              AND (b.n IS NULL OR (NOT b.manual AND b.score < %(t)s))
              AND NOT EXISTS (SELECT 1 FROM alignment_reviews ar WHERE ar.segment_id = s.id
                              AND ar.verdict = 'correct')
            ORDER BY s.seq LIMIT 1
            """,
            {"e": edition_id, "after": after_seq, "t": threshold},
        )
    ).fetchone()
    if row is None:
        return {"seq": None, "score": None, "reason": None}
    return {
        "seq": row["seq"],
        "score": row["score"],
        "reason": "unaligned" if row["unaligned"] else "low_score",
    }


# --------------------------------------------------------------- corrections
@router.post("/links/replace", response_model=WorkbenchTarget, summary="Remplacer les liens d'un segment")
async def replace_links(body: ReplaceLinks, conn: Conn, principal: Reviewer) -> dict:
    """Liens manuels du segment cible vers les segments de référence donnés
    (les liens automatiques du segment sont remplacés)."""
    async with conn.transaction():
        segment = await (
            await conn.execute(
                """
                SELECT s.id, e.work_id, ea.reference_edition_id
                FROM segments s JOIN editions e ON e.id = s.edition_id
                JOIN edition_alignments ea ON ea.edition_id = e.id
                WHERE s.edition_id = %s AND s.seq = %s
                """,
                (body.edition_id, body.seq),
            )
        ).fetchone()
        if segment is None or segment["reference_edition_id"] is None:
            raise not_found("Segment d'une édition alignée")
        units = await (
            await conn.execute(
                """
                SELECT s.seq, sa.unit_id FROM segments s
                JOIN segment_alignments sa ON sa.segment_id = s.id AND sa.method = 'reference'
                WHERE s.edition_id = %s AND s.seq = ANY(%s)
                """,
                (segment["reference_edition_id"], body.reference_seqs),
            )
        ).fetchall()
        missing = set(body.reference_seqs) - {u["seq"] for u in units}
        if missing:
            raise Problem(400, "reference-seq", f"Segments de référence sans unité : {sorted(missing)}")
        await conn.execute("DELETE FROM segment_alignments WHERE segment_id = %s", (segment["id"],))
        for u in units:
            await conn.execute(
                "INSERT INTO segment_alignments (segment_id, unit_id, score, method, created_by_sub) "
                "VALUES (%s, %s, NULL, 'manual', %s)",
                (segment["id"], u["unit_id"], principal.sub),
            )
        await recount(conn, body.edition_id)
        await conn.execute(
            "SELECT corpus_emit('work.alignment_changed', %s, %s, %s, %s)",
            (
                segment["work_id"],
                body.edition_id,
                jsonb({"reason": "manual_links", "seq": body.seq, "reference_seqs": body.reference_seqs}),
                principal.sub,
            ),
        )
    bench = await workbench(body.edition_id, conn, principal, from_seq=body.seq, limit=1)
    return bench["target"][0]


@router.post(
    "/editions/{edition_id}/recount", response_model=list[AlignedEdition], summary="Recalculer la qualité"
)
async def post_recount(edition_id: UUID, conn: Conn, principal: Reviewer) -> list:
    if await recount(conn, edition_id) is None:
        raise not_found("Édition alignée")
    work = await (await conn.execute("SELECT work_id FROM editions WHERE id = %s", (edition_id,))).fetchone()
    return await aligned_editions(conn, principal, work_id=work["work_id"])


@router.post("/editions/{edition_id}/status", response_model=list[AlignedEdition], summary="Fixer le statut")
async def set_status(edition_id: UUID, body: StatusRequest, conn: Conn, principal: Reviewer) -> list:
    async with conn.transaction():
        row = await (
            await conn.execute(
                "UPDATE edition_alignments SET status = %s WHERE edition_id = %s "
                "AND reference_edition_id IS NOT NULL RETURNING edition_id",
                (body.status, edition_id),
            )
        ).fetchone()
        if row is None:
            raise not_found("Édition alignée")
        work = await (
            await conn.execute("SELECT work_id FROM editions WHERE id = %s", (edition_id,))
        ).fetchone()
        await conn.execute(
            "SELECT corpus_emit('work.alignment_changed', %s, %s, %s, %s)",
            (
                work["work_id"],
                edition_id,
                jsonb({"reason": "status", "status": body.status, "comment": body.comment}),
                principal.sub,
            ),
        )
    return await aligned_editions(conn, principal, work_id=work["work_id"])


@router.get("/precision", response_model=list[Precision], summary="Précision mesurée par méthode")
async def precision(conn: Conn, _: Reviewer, sample_only: bool = True) -> list:
    rows = await (
        await conn.execute(
            """
            SELECT alignment_method AS method, count(*) AS reviews,
                   count(*) FILTER (WHERE verdict = 'correct') AS correct,
                   count(*) FILTER (WHERE verdict = 'partial') AS partial,
                   count(*) FILTER (WHERE verdict = 'incorrect') AS incorrect
            FROM alignment_reviews WHERE (NOT %s OR is_sample)
            GROUP BY alignment_method ORDER BY alignment_method
            """,
            (sample_only,),
        )
    ).fetchall()
    for r in rows:
        n = r["reviews"]
        r["precision"] = round((r["correct"] + 0.5 * r["partial"]) / n, 4) if n else None
        r["wilson_low"], r["wilson_high"] = wilson(r["correct"], n)
    return rows
