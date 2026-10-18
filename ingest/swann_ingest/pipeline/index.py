"""Étape 2 : Postgres -> chunks -> vecteurs (dense + BM25) -> Qdrant.

Un index vectoriel = une collection Qdrant = un modèle dense + un découpage.
`run_index` traite les éditions extraites qui ne sont pas encore dans
`edition_indexings` pour cet index (reprise automatique après interruption).
"""

from __future__ import annotations

import re
import uuid
from dataclasses import dataclass

import psycopg
from qdrant_client import QdrantClient, models

from swann_core import qdrant as qstore
from swann_core.embed import sparse
from swann_core.embed.dense import DenseEncoder, count_tokens
from swann_ingest.chunking import v1 as chunker_v1
from swann_ingest.store.pg import emit

CHUNKERS = {chunker_v1.VERSION: chunker_v1}


@dataclass
class VectorIndex:
    id: uuid.UUID
    collection: str
    dense_model: str
    dense_dim: int
    sparse_model: str | None
    chunker_version: str
    status: str


def default_collection_name(dense_model: str, chunker_version: str) -> str:
    slug = re.sub(r"[^a-z0-9.]+", "-", dense_model.split("/")[-1].lower()).strip("-")
    return f"chunks__{slug}__{chunker_version}"


def get_index(conn: psycopg.Connection, collection: str) -> VectorIndex | None:
    row = conn.execute(
        "SELECT id, collection, dense_model, dense_dim, sparse_model, chunker_version, status::text "
        "FROM vector_indexes WHERE collection = %s",
        (collection,),
    ).fetchone()
    return VectorIndex(**row) if row else None


def list_indexes(conn: psycopg.Connection) -> list[dict]:
    return conn.execute(
        """
        SELECT v.collection, v.dense_model, v.chunker_version, v.status::text AS status,
               (SELECT count(*) FROM edition_indexings ei WHERE ei.index_id = v.id) AS editions,
               (SELECT coalesce(sum(n_points), 0) FROM edition_indexings ei
                 WHERE ei.index_id = v.id) AS points
        FROM vector_indexes v ORDER BY v.created_at
        """
    ).fetchall()


def create_index(
    conn: psycopg.Connection,
    qc: QdrantClient,
    dense_model: str,
    dense_dim: int,
    chunker_version: str,
    collection: str | None = None,
) -> VectorIndex:
    if chunker_version not in CHUNKERS:
        raise ValueError(f"découpage inconnu : {chunker_version} (connus : {', '.join(CHUNKERS)})")
    collection = collection or default_collection_name(dense_model, chunker_version)
    if get_index(conn, collection):
        raise ValueError(f"l'index {collection} existe déjà")
    qstore.ensure_collection(qc, collection, dense_dim)
    row = conn.execute(
        "INSERT INTO vector_indexes (collection, dense_model, dense_dim, sparse_model, "
        "chunker_version) VALUES (%s, %s, %s, %s, %s) "
        "RETURNING id, collection, dense_model, dense_dim, sparse_model, chunker_version, "
        "status::text",
        (collection, dense_model, dense_dim, sparse.NAME, chunker_version),
    ).fetchone()
    return VectorIndex(**row)


def activate_index(conn: psycopg.Connection, qc: QdrantClient, collection: str, alias: str) -> None:
    index = get_index(conn, collection)
    if index is None:
        raise ValueError(f"index inconnu : {collection}")
    qstore.set_alias(qc, alias, collection)
    with conn.transaction():
        conn.execute(
            "UPDATE vector_indexes SET status = 'retired' WHERE status = 'active' AND id <> %s",
            (index.id,),
        )
        conn.execute("UPDATE vector_indexes SET status = 'active' WHERE id = %s", (index.id,))
        emit(
            conn,
            "index.activated",
            data={
                "collection": index.collection,
                "dense_model": index.dense_model,
                "chunker_version": index.chunker_version,
            },
        )


def pending_editions(conn: psycopg.Connection, index: VectorIndex) -> list[dict]:
    return conn.execute(
        """
        SELECT e.id, e.source_file, e.language
        FROM editions e
        WHERE EXISTS (SELECT 1 FROM segments s WHERE s.edition_id = e.id)
          AND e.deleted_at IS NULL
          AND NOT EXISTS (SELECT 1 FROM edition_indexings ei
                          WHERE ei.edition_id = e.id AND ei.index_id = %s)
        ORDER BY e.source_file
        """,
        (index.id,),
    ).fetchall()


def _segments(conn: psycopg.Connection, edition_id: uuid.UUID) -> list[dict]:
    return conn.execute(
        """
        SELECT s.seq, s.section_id, s.kind::text AS kind, s.text, sec.matter::text AS matter
        FROM segments s LEFT JOIN sections sec ON sec.id = s.section_id
        WHERE s.edition_id = %s ORDER BY s.seq
        """,
        (edition_id,),
    ).fetchall()


def ensure_chunks(
    conn: psycopg.Connection, edition_id: uuid.UUID, chunker_version: str, tokenizer
) -> tuple[list[dict], dict[int, dict]]:
    """Crée les chunks de l'édition pour ce découpage s'ils n'existent pas.
    Retourne (chunks, segments par seq)."""
    segments = _segments(conn, edition_id)
    by_seq = {s["seq"]: s for s in segments}
    existing = conn.execute(
        "SELECT id, chunk_index, segment_start_seq, segment_end_seq, token_count FROM chunks "
        "WHERE edition_id = %s AND chunker_version = %s ORDER BY chunk_index",
        (edition_id, chunker_version),
    ).fetchall()
    if existing:
        return existing, by_seq

    body_sections = {s["section_id"] for s in segments if s["matter"] == "body"}
    tokens = count_tokens(tokenizer, [s["text"] for s in segments])
    plan = CHUNKERS[chunker_version].plan_chunks(segments, tokens, body_sections)
    rows = [
        {"id": uuid.uuid4(), "chunk_index": i, "segment_start_seq": a, "segment_end_seq": b, "token_count": n}
        for i, (a, b, n) in enumerate(plan)
    ]
    with (
        conn.transaction(),
        conn.cursor().copy(
            "COPY chunks (id, edition_id, chunker_version, chunk_index, segment_start_seq, "
            "segment_end_seq, token_count) FROM STDIN"
        ) as copy,
    ):
        for r in rows:
            copy.write_row(
                (
                    r["id"],
                    edition_id,
                    chunker_version,
                    r["chunk_index"],
                    r["segment_start_seq"],
                    r["segment_end_seq"],
                    r["token_count"],
                )
            )
    return rows, by_seq


def _edition_context(conn: psycopg.Connection, edition_id: uuid.UUID) -> dict:
    return conn.execute(
        """
        SELECT e.work_id, e.language, e.is_original, e.access::text AS access, w.first_published_year,
               coalesce(array(SELECT person_id::text FROM work_authors wa
                              WHERE wa.work_id = e.work_id ORDER BY position), '{}') AS author_ids,
               coalesce(array(SELECT movement_id::text FROM work_movements wm
                              WHERE wm.work_id = e.work_id), '{}') AS movement_ids
        FROM editions e JOIN works w ON w.id = e.work_id WHERE e.id = %s
        """,
        (edition_id,),
    ).fetchone()


def _section_info(conn: psycopg.Connection, edition_id: uuid.UUID) -> dict:
    rows = conn.execute(
        "SELECT p.section_id, p.path_text, s.kind::text AS kind FROM section_paths p "
        "JOIN sections s ON s.id = p.section_id WHERE p.edition_id = %s",
        (edition_id,),
    ).fetchall()
    return {r["section_id"]: r for r in rows}


def payload_fields(ctx: dict) -> dict:
    """Champs du payload Qdrant qui viennent de la fiche (pas du texte)."""
    return {
        "work_id": str(ctx["work_id"]),
        "language": ctx["language"],
        "is_original": ctx["is_original"],
        "access": ctx["access"],
        "author_ids": ctx["author_ids"],
        "movement_ids": ctx["movement_ids"],
        "first_published_year": ctx["first_published_year"],
    }


def active_index(conn: psycopg.Connection) -> VectorIndex | None:
    row = conn.execute(
        "SELECT id, collection, dense_model, dense_dim, sparse_model, chunker_version, status::text "
        "FROM vector_indexes WHERE status = 'active' LIMIT 1"
    ).fetchone()
    return VectorIndex(**row) if row else None


def drop_edition(conn: psycopg.Connection, qc: QdrantClient, edition_id: uuid.UUID) -> int:
    """Retire l'édition de tous les index (points Qdrant, edition_indexings) ;
    garde ses chunks sauf `drop_chunks`. Renvoie le nombre d'index touchés."""
    rows = conn.execute(
        "SELECT v.collection FROM edition_indexings ei JOIN vector_indexes v ON v.id = ei.index_id "
        "WHERE ei.edition_id = %s",
        (edition_id,),
    ).fetchall()
    for row in rows:
        qstore.delete_edition(qc, row["collection"], edition_id)
    conn.execute("DELETE FROM edition_indexings WHERE edition_id = %s", (edition_id,))
    return len(rows)


def sync_edition_payload(
    conn: psycopg.Connection, qc: QdrantClient, edition_id: uuid.UUID, collection: str | None = None
) -> None:
    """Recopie dans chaque collection qui indexe l'édition les champs de la
    fiche copiés dans le payload (droits, œuvre, langue, auteurs, courants,
    année) : modifiables dans la console sans réindexer."""
    ctx = _edition_context(conn, edition_id)
    collections = conn.execute(
        "SELECT v.collection FROM edition_indexings ei JOIN vector_indexes v ON v.id = ei.index_id "
        "WHERE ei.edition_id = %(e)s AND (%(c)s::text IS NULL OR v.collection = %(c)s)",
        {"e": edition_id, "c": collection},
    ).fetchall()
    for row in collections:
        qstore.ensure_payload_indexes(qc, row["collection"])
        qstore.set_edition_payload(qc, row["collection"], edition_id, payload_fields(ctx))


def index_edition(
    conn: psycopg.Connection, qc: QdrantClient, index: VectorIndex, encoder: DenseEncoder, edition: dict
) -> tuple[int, int]:
    """Retourne (nombre de chunks, nombre de tokens vectorisés)."""
    chunks, by_seq = ensure_chunks(conn, edition["id"], index.chunker_version, encoder.tokenizer)
    ctx = _edition_context(conn, edition["id"])
    sections = _section_info(conn, edition["id"])

    texts = [
        "\n\n".join(by_seq[q]["text"] for q in range(c["segment_start_seq"], c["segment_end_seq"] + 1))
        for c in chunks
    ]
    dense = encoder.encode_documents(texts) if texts else []

    def points():
        for chunk, text, vector in zip(chunks, texts, dense, strict=True):
            section_id = by_seq[chunk["segment_start_seq"]]["section_id"]
            section = sections.get(section_id, {})
            idx, val = sparse.encode_document(text, edition["language"])
            yield models.PointStruct(
                id=str(chunk["id"]),
                vector={
                    qstore.DENSE: vector.tolist(),
                    qstore.SPARSE: models.SparseVector(indices=idx, values=val),
                },
                payload={
                    **payload_fields(ctx),
                    "edition_id": str(edition["id"]),
                    "section_id": str(section_id),
                    "section_kind": section.get("kind"),
                    "section_path": section.get("path_text"),
                    "chunk_index": chunk["chunk_index"],
                    "segment_start_seq": chunk["segment_start_seq"],
                    "segment_end_seq": chunk["segment_end_seq"],
                    "chunker_version": index.chunker_version,
                },
            )

    # Une ré-extraction a pu laisser d'anciens points : on repart de zéro.
    qstore.delete_edition(qc, index.collection, edition["id"])
    qstore.upsert(qc, index.collection, points())
    conn.execute(
        "INSERT INTO edition_indexings (edition_id, index_id, n_points) VALUES (%s, %s, %s) "
        "ON CONFLICT (edition_id, index_id) DO UPDATE SET n_points = EXCLUDED.n_points, "
        "indexed_at = now()",
        (edition["id"], index.id, len(chunks)),
    )
    return len(chunks), sum(c["token_count"] or 0 for c in chunks)
