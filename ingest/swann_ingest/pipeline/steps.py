"""Étapes réutilisables par le worker (et la CLI) : indexer une édition dans
l'index actif, aligner une œuvre, retraiter une édition après une
modification de structure, corbeille et purge.

Les modèles (Qwen3 pour l'index, LaBSE pour l'alignement) sont chargés une
fois par `Models` et gardés en mémoire par le worker.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass, field

import psycopg
from qdrant_client import QdrantClient

from swann_ingest import quality
from swann_ingest.jobs import JobContext
from swann_ingest.pipeline.index import active_index, drop_edition, index_edition
from swann_ingest.store.pg import Store, emit

log = logging.getLogger("swann.worker")


@dataclass
class Models:
    """Encodeurs chargés à la demande et conservés."""

    device: str | None
    batch_size: int
    align_model: str
    _dense: dict = field(default_factory=dict)
    _align: object | None = None

    def dense(self, model_name: str):
        if model_name not in self._dense:
            from swann_core.embed.dense import DenseEncoder

            log.info("Chargement de %s…", model_name)
            self._dense[model_name] = DenseEncoder(model_name, self.device, self.batch_size)
        return self._dense[model_name]

    def align(self):
        if self._align is None:
            from swann_ingest.pipeline.align import AlignEncoder

            log.info("Chargement de %s…", self.align_model)
            self._align = AlignEncoder(self.align_model, self.device)
        return self._align

    def loaded(self) -> dict:
        out = {name: enc.device for name, enc in self._dense.items()}
        if self._align is not None:
            out[self.align_model] = self._align.device  # type: ignore[attr-defined]
        return out


def index_in_active(
    conn: psycopg.Connection, qc: QdrantClient, models: Models, edition_id: uuid.UUID, ctx: JobContext
) -> dict | None:
    """Indexe (ou réindexe) l'édition dans l'index actif. None s'il n'y en a pas."""
    index = active_index(conn)
    if index is None:
        return None
    edition = conn.execute(
        "SELECT id, source_file, language FROM editions WHERE id = %s", (edition_id,)
    ).fetchone()
    store = Store(conn)
    ctx.progress("index", message=f"vectorisation ({index.dense_model})", force=True)
    encoder = models.dense(index.dense_model)
    ingestion = store.start_ingestion(
        edition["source_file"], edition_id, index.id, stage="index", job_id=ctx.id
    )
    try:
        n_chunks, n_tokens = index_edition(conn, qc, index, encoder, edition)
    except Exception as e:
        store.finish_ingestion(ingestion, error=f"{type(e).__name__}: {e}")
        raise
    store.finish_ingestion(ingestion, n_chunks=n_chunks)
    return {"collection": index.collection, "chunks": n_chunks, "tokens": n_tokens}


def align_work_step(
    conn: psycopg.Connection,
    models: Models,
    work_id: uuid.UUID,
    ctx: JobContext,
    *,
    force: bool = False,
    incremental: bool = True,
) -> dict:
    """Aligne l'œuvre (seulement les éditions en attente si `incremental`).
    Une œuvre corrigée à la main n'est réalignée en entier qu'avec `force`."""
    from swann_ingest.pipeline.align import align_work, works_to_align

    works = works_to_align(conn, ids=[work_id])
    if not works:
        return {"skipped": "moins de deux éditions extraites"}
    work = works[0]
    ctx.progress("align", message=f"alignement de {work['title']}", force=True)
    store = Store(conn)
    ingestion = store.start_ingestion(work["slug"] or str(work_id), stage="align", job_id=ctx.id)
    try:
        reports = align_work(conn, models.align(), work, force, incremental=incremental)
    except RuntimeError as e:  # liens manuels : pas de réalignement complet sans force
        store.finish_ingestion(ingestion, error=str(e))
        return {"skipped": str(e)}
    except Exception as e:
        store.finish_ingestion(ingestion, error=f"{type(e).__name__}: {e}")
        raise
    store.finish_ingestion(ingestion)
    for r in reports:
        if r.get("edition_id"):
            quality.compute(conn, r["edition_id"])
    return {
        "editions": [
            {k: (str(v) if isinstance(v, uuid.UUID) else v) for k, v in r.items() if k != "source_file"}
            | {"source_file": r.get("source_file")}
            for r in reports
        ]
    }


def reprocess_edition(
    conn: psycopg.Connection, qc: QdrantClient, models: Models, edition_id: uuid.UUID, ctx: JobContext
) -> dict:
    """Après une modification de structure : le découpage dépend des sections
    (un chunk ne déborde pas d'une section, seul le corps est découpé) et le
    chemin de section est dans le payload → nouveaux chunks, réindexation,
    réalignement de l'œuvre."""
    row = conn.execute("SELECT work_id FROM editions WHERE id = %s", (edition_id,)).fetchone()
    ctx.link(work_id=row["work_id"], edition_id=edition_id)
    ctx.progress("reprocess", message="suppression des anciens chunks", force=True)
    with conn.transaction():
        drop_edition(conn, qc, edition_id)
        conn.execute("DELETE FROM chunks WHERE edition_id = %s", (edition_id,))
        # L'alignement de cette édition repose sur l'ancienne structure.
        conn.execute(
            "DELETE FROM edition_alignments WHERE edition_id = %s AND reference_edition_id IS NOT NULL",
            (edition_id,),
        )
    ctx.check_cancel()
    result: dict = {"index": index_in_active(conn, qc, models, edition_id, ctx)}
    ctx.check_cancel()
    result["align"] = align_work_step(conn, models, row["work_id"], ctx)
    result["quality"] = quality.compute(conn, edition_id)
    return result


def trash_edition(conn: psycopg.Connection, qc: QdrantClient, edition_id: uuid.UUID, ctx: JobContext) -> dict:
    """Édition mise à la corbeille par l'API : on la retire des index (la
    recherche ne la trouve plus). Chunks et texte restent pour la restauration."""
    ctx.progress("trash", message="retrait des index", force=True)
    n = drop_edition(conn, qc, edition_id)
    return {"indexes": n}


def restore_edition(
    conn: psycopg.Connection, qc: QdrantClient, models: Models, edition_id: uuid.UUID, ctx: JobContext
) -> dict:
    row = conn.execute("SELECT work_id FROM editions WHERE id = %s", (edition_id,)).fetchone()
    result: dict = {"index": index_in_active(conn, qc, models, edition_id, ctx)}
    result["align"] = align_work_step(conn, models, row["work_id"], ctx)
    result["quality"] = quality.compute(conn, edition_id)
    return result


def purge(
    conn: psycopg.Connection,
    qc: QdrantClient,
    mc,
    bucket: str,
    ctx: JobContext,
    *,
    older_than_days: int,
    edition_ids: list[uuid.UUID] | None = None,
) -> dict:
    """Suppression définitive des éditions à la corbeille depuis plus de
    `older_than_days` jours (ou de celles demandées), puis des œuvres à la
    corbeille devenues vides."""
    from swann_core import s3

    rows = conn.execute(
        """
        SELECT id, work_id, epub_object_key, source_file FROM editions
        WHERE deleted_at IS NOT NULL
          AND (deleted_at < now() - make_interval(days => %(days)s) OR id = ANY(%(ids)s))
        """,
        {"days": older_than_days, "ids": edition_ids or []},
    ).fetchall()
    purged = []
    for i, r in enumerate(rows, 1):
        ctx.check_cancel()
        ctx.progress("purge", i, len(rows), r["source_file"])
        drop_edition(conn, qc, r["id"])
        with conn.transaction():
            # Unités d'alignement de l'œuvre si l'édition en était la référence.
            is_reference = conn.execute(
                "SELECT 1 FROM edition_alignments WHERE edition_id = %s AND reference_edition_id IS NULL",
                (r["id"],),
            ).fetchone()
            if is_reference:
                conn.execute("DELETE FROM work_units WHERE work_id = %s", (r["work_id"],))
                conn.execute(
                    "DELETE FROM edition_alignments WHERE edition_id IN "
                    "(SELECT id FROM editions WHERE work_id = %s)",
                    (r["work_id"],),
                )
            conn.execute("DELETE FROM editions WHERE id = %s", (r["id"],))
            emit(conn, "edition.deleted", r["work_id"], r["id"], actor=ctx.params.get("actor"))
        key = r["epub_object_key"]
        if mc and key:
            still_used = conn.execute("SELECT 1 FROM editions WHERE epub_object_key = %s", (key,)).fetchone()
            if not still_used:
                s3.remove(mc, bucket, key)
        purged.append(str(r["id"]))
    works = conn.execute(
        """
        DELETE FROM works w WHERE w.deleted_at IS NOT NULL
          AND NOT EXISTS (SELECT 1 FROM editions e WHERE e.work_id = w.id)
        RETURNING id
        """
    ).fetchall()
    for w in works:
        emit(conn, "work.deleted", w["id"], actor=ctx.params.get("actor"))
    return {"editions": purged, "works": [str(w["id"]) for w in works]}
