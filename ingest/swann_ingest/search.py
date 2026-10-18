"""Recherche hybride (sens + mots exacts) regroupée par œuvre — sert à tester
l'index de bout en bout depuis la ligne de commande."""

from __future__ import annotations

import uuid
from dataclasses import dataclass

import psycopg
from qdrant_client import QdrantClient, models

from swann_core import qdrant as qstore
from swann_core.embed import sparse
from swann_core.embed.dense import DenseEncoder

CANDIDATES = 100
MODES = ("theme", "quote", "words")


@dataclass
class Hit:
    work_title: str
    authors: list[str]
    edition_title: str
    language: str
    section_path: str | None
    score: float
    text: str
    translation: str | None = None
    translation_language: str | None = None


def corpus_languages(conn: psycopg.Connection) -> list[str]:
    return [r["language"] for r in conn.execute("SELECT DISTINCT language FROM editions").fetchall()]


def _sparse_query(text: str, languages: list[str]) -> models.SparseVector:
    """Le mot est ramené à sa racine dans chaque langue du corpus : on ne sait
    pas dans quelle langue est la requête."""
    ids: set[int] = set()
    for lang in languages or [None]:
        ids.update(sparse.encode_query(text, lang)[0])
    ids_sorted = sorted(ids)
    return models.SparseVector(indices=ids_sorted, values=[1.0] * len(ids_sorted))


def _filter(
    languages: list[str] | None, year_min: int | None, year_max: int | None, original_only: bool
) -> models.Filter | None:
    must: list = []
    if languages:
        must.append(models.FieldCondition(key="language", match=models.MatchAny(any=languages)))
    if year_min is not None or year_max is not None:
        must.append(
            models.FieldCondition(key="first_published_year", range=models.Range(gte=year_min, lte=year_max))
        )
    if original_only:
        must.append(models.FieldCondition(key="is_original", match=models.MatchValue(value=True)))
    return models.Filter(must=must) if must else None


def _chunk_text(conn: psycopg.Connection, edition_id: str, a: int, b: int) -> str:
    rows = conn.execute(
        "SELECT text FROM segments WHERE edition_id = %s AND seq BETWEEN %s AND %s ORDER BY seq",
        (edition_id, a, b),
    ).fetchall()
    return "\n\n".join(r["text"] for r in rows)


def _translation(
    conn: psycopg.Connection, work_id: str, edition_id: str, a: int, b: int, language: str
) -> str | None:
    """Passage correspondant dans une édition de l'œuvre dans `language`, via
    les unités d'alignement. S'il y a plusieurs éditions dans cette langue :
    l'originale, sinon la mieux alignée."""
    target = conn.execute(
        """
        SELECT e.id FROM editions e
        LEFT JOIN edition_alignments ea ON ea.edition_id = e.id
        WHERE e.work_id = %s AND e.language = %s AND e.id <> %s
        ORDER BY e.is_original DESC, ea.mean_score DESC NULLS LAST, e.source_file
        LIMIT 1
        """,
        (work_id, language, edition_id),
    ).fetchone()
    if target is None:
        return None
    rows = conn.execute(
        """
        SELECT DISTINCT t.seq, t.text
        FROM segments s
        JOIN segment_alignments a1 ON a1.segment_id = s.id
        JOIN segment_alignments a2 ON a2.unit_id = a1.unit_id
        JOIN segments t ON t.id = a2.segment_id
        WHERE s.edition_id = %s AND s.seq BETWEEN %s AND %s AND t.edition_id = %s
        ORDER BY t.seq
        """,
        (edition_id, a, b, target["id"]),
    ).fetchall()
    return "\n\n".join(r["text"] for r in rows) or None


def search(
    conn: psycopg.Connection,
    qc: QdrantClient,
    encoder: DenseEncoder,
    collection: str,
    query: str,
    *,
    mode: str = "theme",
    languages: list[str] | None = None,
    year_min: int | None = None,
    year_max: int | None = None,
    original_only: bool = False,
    limit: int = 10,
    show_language: str | None = None,
) -> list[Hit]:
    """mode : "theme" (sens seul), "quote" (sens + mots, fusion RRF) ou
    "words" (mots exacts seuls, BM25)."""
    if mode not in MODES:
        raise ValueError(f"mode inconnu : {mode} (attendu : {', '.join(MODES)})")
    flt = _filter(languages, year_min, year_max, original_only)
    common = {
        "collection_name": collection,
        "group_by": "work_id",
        "group_size": 1,
        "limit": limit,
        "with_payload": True,
    }
    if mode == "words":
        sparse_vec = _sparse_query(query, languages or corpus_languages(conn))
        response = qc.query_points_groups(query=sparse_vec, using=qstore.SPARSE, query_filter=flt, **common)
    elif mode == "theme":
        dense = encoder.encode_query(query, "theme").tolist()
        response = qc.query_points_groups(query=dense, using=qstore.DENSE, query_filter=flt, **common)
    else:
        dense = encoder.encode_query(query, "quote").tolist()
        sparse_vec = _sparse_query(query, languages or corpus_languages(conn))
        response = qc.query_points_groups(
            prefetch=[
                models.Prefetch(query=dense, using=qstore.DENSE, limit=CANDIDATES, filter=flt),
                models.Prefetch(query=sparse_vec, using=qstore.SPARSE, limit=CANDIDATES, filter=flt),
            ],
            query=models.FusionQuery(fusion=models.Fusion.RRF),
            **common,
        )

    hits: list[Hit] = []
    for group in response.groups:
        point = group.hits[0]
        p = point.payload
        meta = conn.execute(
            """
            SELECT w.title AS work_title, e.title AS edition_title,
                   array(SELECT pe.display_name FROM work_authors wa
                         JOIN persons pe ON pe.id = wa.person_id
                         WHERE wa.work_id = w.id ORDER BY wa.position) AS authors
            FROM editions e JOIN works w ON w.id = e.work_id WHERE e.id = %s
            """,
            (uuid.UUID(p["edition_id"]),),
        ).fetchone()
        hit = Hit(
            work_title=meta["work_title"],
            authors=meta["authors"],
            edition_title=meta["edition_title"],
            language=p["language"],
            section_path=p.get("section_path"),
            score=point.score,
            text=_chunk_text(conn, p["edition_id"], p["segment_start_seq"], p["segment_end_seq"]),
        )
        if show_language and show_language != p["language"]:
            hit.translation = _translation(
                conn,
                p["work_id"],
                p["edition_id"],
                p["segment_start_seq"],
                p["segment_end_seq"],
                show_language,
            )
            hit.translation_language = show_language
        hits.append(hit)
    return hits
