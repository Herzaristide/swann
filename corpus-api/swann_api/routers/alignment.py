"""Alignement des traductions : qualité, passage correspondant, lecture
parallèle, relecture humaine (verdicts, liens manuels)."""

from __future__ import annotations

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Query, Response
from psycopg.types.json import Jsonb

from swann_api import sql
from swann_api.auth import Principal, Reader, Reviewer
from swann_api.deps import Conn
from swann_api.errors import Problem, forbidden, not_found
from swann_api.routers.editions import load_edition
from swann_api.schemas import (
    AlignmentLink,
    Counterpart,
    LinkRequest,
    Parallel,
    ReviewCreated,
    ReviewRequest,
    WorkAlignment,
)

router = APIRouter(tags=["alignement"])

MAX_PARALLEL = 500


# ------------------------------------------------------------------- qualité
@router.get(
    "/works/{work_id}/alignment", response_model=WorkAlignment, summary="Qualité de l'alignement par édition"
)
async def work_alignment(work_id: UUID, conn: Conn, principal: Reader) -> dict:
    p = {"w": work_id, "access": list(principal.visible_access)}
    editions = await (
        await conn.execute(
            f"""
            SELECT e.id AS edition_id, e.title, e.language, e.is_original,
                   (ea.edition_id IS NOT NULL AND ea.reference_edition_id IS NULL) AS is_reference,
                   ea.status::text AS status, ea.method, ea.n_segments, ea.aligned_ratio, ea.mean_score,
                   ea.low_score_ratio, ea.aligned_at
            FROM editions e LEFT JOIN edition_alignments ea ON ea.edition_id = e.id
            WHERE e.work_id = %(w)s AND {sql.visible("e")}
            ORDER BY is_reference DESC, e.is_original DESC, e.language
            """,
            p,
        )
    ).fetchall()
    if not editions:
        raise not_found("Œuvre")
    n_units = (
        await (
            await conn.execute("SELECT count(*) AS n FROM work_units WHERE work_id = %s", (work_id,))
        ).fetchone()
    )["n"]
    reference = next((e["edition_id"] for e in editions if e["is_reference"]), None)
    return {"work_id": work_id, "reference_edition_id": reference, "n_units": n_units, "editions": editions}


# ------------------------------------------------------ édition cible commune
async def pick_target(
    conn: Conn, source: dict, principal: Principal, target: UUID | None, lang: str | None
) -> dict:
    if (target is None) == (lang is None):
        raise Problem(400, "target", "Préciser soit `target` (édition), soit `lang`.")
    row = await (
        await conn.execute(
            f"""
            SELECT e.id, e.language, e.revision, e.access::text AS access, ea.status::text AS status,
                   (ea.edition_id IS NOT NULL AND ea.reference_edition_id IS NULL) AS is_reference
            FROM editions e LEFT JOIN edition_alignments ea ON ea.edition_id = e.id
            WHERE e.work_id = %(w)s AND e.id <> %(s)s AND {sql.visible("e")}
              AND (%(t)s::uuid IS NULL OR e.id = %(t)s)
              AND (%(l)s::text IS NULL OR e.language = %(l)s
                   OR split_part(e.language, '-', 1) = split_part(%(l)s::text, '-', 1))
            ORDER BY (e.language = %(l)s) DESC, e.is_original DESC, ea.mean_score DESC NULLS LAST, e.id
            LIMIT 1
            """,
            {
                "w": source["work_id"],
                "s": source["id"],
                "t": target,
                "l": lang,
                "access": list(principal.visible_access),
            },
        )
    ).fetchone()
    if row is None:
        raise not_found("Édition cible (même œuvre, autre édition)")
    return row


# ----------------------------------------------------- passage correspondant
@router.get(
    "/editions/{edition_id}/counterpart",
    response_model=Counterpart,
    summary="Position correspondante dans une autre édition de l'œuvre",
)
async def counterpart(
    edition_id: UUID,
    conn: Conn,
    principal: Reader,
    seq: Annotated[int, Query(ge=0)],
    target: Annotated[UUID | None, Query(description="Édition cible.")] = None,
    lang: Annotated[str | None, Query(description="Ou : langue cible (meilleure édition).")] = None,
) -> dict:
    source = await load_edition(conn, edition_id, principal)
    tgt = await pick_target(conn, source, principal, target, lang)
    out = {"edition_id": tgt["id"], "language": tgt["language"], "revision": tgt["revision"]}
    if tgt["status"] != "rejected":
        row = await (
            await conn.execute(
                """
                SELECT min(ts.seq) AS a, max(ts.seq) AS b, avg(coalesce(a2.score, a1.score)) AS score
                FROM segments s
                JOIN segment_alignments a1 ON a1.segment_id = s.id
                JOIN segment_alignments a2 ON a2.unit_id = a1.unit_id
                JOIN segments ts ON ts.id = a2.segment_id
                WHERE s.edition_id = %s AND s.seq = %s AND ts.edition_id = %s
                """,
                (edition_id, seq, tgt["id"]),
            )
        ).fetchone()
        if row["a"] is not None:
            return {
                **out,
                "seq_start": row["a"],
                "seq_end": row["b"],
                "via": "segment",
                "score": row["score"],
            }

    # Repli : début du chapitre correspondant (sections appariées à l'alignement).
    row = await (
        await conn.execute(
            """
            WITH RECURSIVE up AS (
                SELECT sec.id, sec.parent_id, sec.reference_section_id, 0 AS depth
                FROM segments s JOIN sections sec ON sec.id = s.section_id
                WHERE s.edition_id = %(src)s AND s.seq = %(seq)s
                UNION ALL
                SELECT p.id, p.parent_id, p.reference_section_id, up.depth + 1
                FROM up JOIN sections p ON p.id = up.parent_id
            ), ref AS (
                -- section de l'édition de référence : celle de la source si la source EST la référence
                SELECT coalesce(up.reference_section_id,
                                CASE WHEN EXISTS (SELECT 1 FROM edition_alignments ea
                                                  WHERE ea.edition_id = %(src)s
                                                    AND ea.reference_edition_id IS NULL)
                                     THEN up.id END) AS ref_id,
                       up.depth
                FROM up
            )
            SELECT r.seq_start FROM ref
            JOIN sections ts ON ts.edition_id = %(tgt)s
                            AND (ts.reference_section_id = ref.ref_id OR ts.id = ref.ref_id)
            JOIN section_ranges(%(tgt)s) r ON r.section_id = ts.id
            WHERE ref.ref_id IS NOT NULL AND r.seq_start IS NOT NULL
            ORDER BY ref.depth, ts.seq LIMIT 1
            """,
            {"src": edition_id, "seq": seq, "tgt": tgt["id"]},
        )
    ).fetchone()
    if row is None:
        raise not_found("Passage correspondant (édition non alignée)")
    return {
        **out,
        "seq_start": row["seq_start"],
        "seq_end": row["seq_start"],
        "via": "section",
        "score": None,
    }


# ------------------------------------------------------------ lecture parallèle
@router.get("/editions/{edition_id}/parallel", response_model=Parallel, summary="Lecture côte à côte")
async def parallel(
    edition_id: UUID,
    conn: Conn,
    principal: Reader,
    target: UUID,
    from_seq: Annotated[int, Query(ge=0)],
    to_seq: Annotated[int, Query(ge=0)],
) -> dict:
    if to_seq < from_seq or to_seq - from_seq >= MAX_PARALLEL:
        raise Problem(400, "range", f"Plage invalide (au plus {MAX_PARALLEL} segments).")
    source = await load_edition(conn, edition_id, principal)
    tgt = await pick_target(conn, source, principal, target, None)
    links = await (
        await conn.execute(
            """
            SELECT s.seq AS s_seq, ts.seq AS t_seq, a2.score, a2.method, a1.score AS s_score
            FROM segments s
            LEFT JOIN segment_alignments a1 ON a1.segment_id = s.id
            LEFT JOIN segment_alignments a2 ON a2.unit_id = a1.unit_id
                 AND a2.segment_id IN (SELECT id FROM segments WHERE edition_id = %(t)s)
            LEFT JOIN segments ts ON ts.id = a2.segment_id
            WHERE s.edition_id = %(s)s AND s.seq BETWEEN %(a)s AND %(b)s
            ORDER BY s.seq, ts.seq
            """,
            {"s": edition_id, "t": tgt["id"], "a": from_seq, "b": to_seq},
        )
    ).fetchall()
    pairs = group_pairs(links)
    # Segments de la cible sans correspondant, glissés entre les paires.
    matched = sorted({t for p in pairs for t in p["target_seqs"]})
    if matched:
        rows = await (
            await conn.execute(
                "SELECT seq FROM segments WHERE edition_id = %s AND seq BETWEEN %s AND %s ORDER BY seq",
                (tgt["id"], matched[0], matched[-1]),
            )
        ).fetchall()
        orphans = [r["seq"] for r in rows if r["seq"] not in set(matched)]
        pairs = insert_orphans(pairs, orphans)
    quality = tgt["status"]
    if tgt["is_reference"]:
        own = await (
            await conn.execute(
                "SELECT status::text AS s FROM edition_alignments WHERE edition_id = %s", (edition_id,)
            )
        ).fetchone()
        quality = own["s"] if own else None
    return {
        "source": {"edition_id": edition_id, "revision": source["revision"]},
        "target": {"edition_id": tgt["id"], "revision": tgt["revision"]},
        "quality": quality,
        "pairs": pairs,
    }


def group_pairs(links: list[dict]) -> list[dict]:
    """Composantes connexes du graphe source ↔ cible (relations n-n), dans
    l'ordre de la source. Une source sans lien forme une paire à cible vide."""
    parent: dict = {}

    def find(x):
        while parent.setdefault(x, x) != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    for link in links:
        find(("s", link["s_seq"]))
        if link["t_seq"] is not None:
            parent[find(("s", link["s_seq"]))] = find(("t", link["t_seq"]))
    groups: dict = {}
    for link in links:
        g = groups.setdefault(
            find(("s", link["s_seq"])), {"s": set(), "t": set(), "scores": [], "methods": set()}
        )
        g["s"].add(link["s_seq"])
        if link["t_seq"] is not None:
            g["t"].add(link["t_seq"])
            score = link["score"] if link["score"] is not None else link["s_score"]
            if score is not None:
                g["scores"].append(score)
            if link["method"] and link["method"] != "reference":
                g["methods"].add(link["method"])
    pairs = [
        {
            "source_seqs": sorted(g["s"]),
            "target_seqs": sorted(g["t"]),
            "score": round(sum(g["scores"]) / len(g["scores"]), 4) if g["scores"] else None,
            "method": ",".join(sorted(g["methods"])) or ("reference" if g["t"] else None),
        }
        for g in groups.values()
    ]
    return sorted(pairs, key=lambda p: p["source_seqs"][0])


def insert_orphans(pairs: list[dict], orphans: list[int]) -> list[dict]:
    out: list[dict] = []
    queue = sorted(orphans)
    for pair in pairs:
        first = pair["target_seqs"][0] if pair["target_seqs"] else None
        while queue and first is not None and queue[0] < first:
            out.append({"source_seqs": [], "target_seqs": [queue.pop(0)], "score": None, "method": None})
        out.append(pair)
    out.extend({"source_seqs": [], "target_seqs": [t], "score": None, "method": None} for t in queue)
    return out


# --------------------------------------------------------------- relecture
async def segment_of(conn: Conn, edition_id: UUID, seq: int, principal: Principal) -> dict:
    row = await (
        await conn.execute(
            """
            SELECT s.id, s.seq, s.text, e.id AS edition_id, e.work_id, e.revision, e.access::text AS access
            FROM segments s JOIN editions e ON e.id = s.edition_id
            WHERE s.edition_id = %s AND s.seq = %s
            """,
            (edition_id, seq),
        )
    ).fetchone()
    if row is None or not principal.can_see(row["access"]):
        raise not_found("Segment")
    if not principal.can_read_text(row["access"]):
        raise forbidden("Texte de l'édition non disponible pour la relecture.")
    return row


@router.get("/alignment/sample", response_model=AlignmentLink, summary="Un lien tiré au hasard, à vérifier")
async def sample(
    conn: Conn,
    principal: Reviewer,
    method: str | None = None,
    min_score: float | None = None,
    max_score: float | None = None,
    work_id: UUID | None = None,
) -> dict:
    readable = ["open"] if not principal.is_admin else ["open", "excerpt", "restricted"]
    link = await (
        await conn.execute(
            """
            SELECT sa.unit_id, sa.method, sa.score, s.seq, s.text, e.id AS edition_id, e.revision, u.work_id
            FROM segment_alignments sa
            JOIN segments s ON s.id = sa.segment_id JOIN editions e ON e.id = s.edition_id
            JOIN work_units u ON u.id = sa.unit_id
            WHERE sa.method <> 'reference' AND e.access::text = ANY(%(readable)s)
              AND (%(m)s::text IS NULL OR sa.method = %(m)s)
              AND (%(lo)s::real IS NULL OR sa.score >= %(lo)s)
              AND (%(hi)s::real IS NULL OR sa.score <= %(hi)s)
              AND (%(w)s::uuid IS NULL OR u.work_id = %(w)s)
            ORDER BY random() LIMIT 1
            """,
            {"readable": readable, "m": method, "lo": min_score, "hi": max_score, "w": work_id},
        )
    ).fetchone()
    if link is None:
        raise not_found("Lien d'alignement")
    reference = await (
        await conn.execute(
            """
            SELECT s.seq, s.text, e.id AS edition_id, e.revision
            FROM segment_alignments sa
            JOIN segments s ON s.id = sa.segment_id JOIN editions e ON e.id = s.edition_id
            WHERE sa.unit_id = %s AND sa.method = 'reference' ORDER BY s.seq
            """,
            (link["unit_id"],),
        )
    ).fetchall()
    return {
        "unit_id": link["unit_id"],
        "method": link["method"],
        "score": link["score"],
        "source": {k: link[k] for k in ("edition_id", "revision", "seq", "text")},
        "reference": reference,
    }


@router.post(
    "/alignment/reviews",
    response_model=ReviewCreated,
    status_code=201,
    summary="Verdict sur un lien d'alignement",
)
async def create_review(body: ReviewRequest, conn: Conn, principal: Reviewer) -> dict:
    segment = await segment_of(conn, body.source.edition_id, body.source.seq, principal)
    link = await (
        await conn.execute(
            "SELECT method, score FROM segment_alignments WHERE segment_id = %s AND unit_id = %s",
            (segment["id"], body.unit_id),
        )
    ).fetchone()
    if link is None:
        raise not_found("Lien d'alignement")
    return await (
        await conn.execute(
            """
            INSERT INTO alignment_reviews (segment_id, unit_id, reviewer_sub, verdict, is_sample,
                                           alignment_method, alignment_score, comment)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s) RETURNING id, created_at
            """,
            (
                segment["id"],
                body.unit_id,
                principal.sub,
                body.verdict,
                body.is_sample,
                link["method"],
                link["score"],
                body.comment,
            ),
        )
    ).fetchone()


async def checked_unit(conn: Conn, unit_id: UUID, work_id: UUID) -> None:
    row = await (await conn.execute("SELECT work_id FROM work_units WHERE id = %s", (unit_id,))).fetchone()
    if row is None:
        raise not_found("Unité d'alignement")
    if row["work_id"] != work_id:
        raise Problem(400, "unit-work", "L'unité appartient à une autre œuvre que le segment.")


@router.post("/alignment/links", status_code=204, summary="Poser un lien manuel (correction)")
async def create_link(body: LinkRequest, conn: Conn, principal: Reviewer) -> Response:
    segment = await segment_of(conn, body.source.edition_id, body.source.seq, principal)
    await checked_unit(conn, body.unit_id, segment["work_id"])
    async with conn.transaction():
        await conn.execute(
            """
            INSERT INTO segment_alignments (segment_id, unit_id, score, method, created_by_sub)
            VALUES (%s, %s, NULL, 'manual', %s)
            ON CONFLICT (segment_id, unit_id) DO UPDATE
                SET score = NULL, method = 'manual', created_by_sub = EXCLUDED.created_by_sub,
                    created_at = now()
            """,
            (segment["id"], body.unit_id, principal.sub),
        )
        await conn.execute(
            "SELECT corpus_emit('work.alignment_changed', %s, %s, %s)",
            (
                segment["work_id"],
                segment["edition_id"],
                Jsonb({"reason": "manual_link", "seq": segment["seq"], "unit_id": str(body.unit_id)}),
            ),
        )
    return Response(status_code=204)


@router.delete("/alignment/links", status_code=204, summary="Supprimer un lien jugé faux")
async def delete_link(
    conn: Conn,
    principal: Reviewer,
    edition_id: UUID,
    seq: Annotated[int, Query(ge=0)],
    unit_id: UUID,
) -> Response:
    segment = await segment_of(conn, edition_id, seq, principal)
    async with conn.transaction():
        deleted = await (
            await conn.execute(
                "DELETE FROM segment_alignments WHERE segment_id = %s AND unit_id = %s RETURNING method",
                (segment["id"], unit_id),
            )
        ).fetchone()
        if deleted is None:
            raise not_found("Lien d'alignement")
        if deleted["method"] == "reference":
            raise Problem(
                400, "reference-link", "Un lien de l'édition de référence définit l'unité : non supprimable."
            )
        await conn.execute(
            "SELECT corpus_emit('work.alignment_changed', %s, %s, %s)",
            (
                segment["work_id"],
                edition_id,
                Jsonb({"reason": "link_deleted", "seq": seq, "unit_id": str(unit_id)}),
            ),
        )
    return Response(status_code=204)
