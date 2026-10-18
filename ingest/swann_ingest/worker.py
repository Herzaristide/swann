"""`swann worker` : exécute la file `jobs` (docs/console.md §3).

Un seul traitement à la fois (un GPU), modèles gardés en mémoire entre les
tâches. Réveillé par LISTEN jobs, sinon toutes les 30 s ; signale sa présence
dans `workers` toutes les 10 s ; remet en file les tâches abandonnées ; lance
la purge de la corbeille une fois par heure s'il y a quelque chose à purger.
"""

from __future__ import annotations

import logging
import os
import signal
import socket
import threading
import time
import traceback
import uuid
from collections.abc import Callable

import psycopg
from psycopg.types.json import Jsonb

from swann_ingest import jobs, quality
from swann_ingest.config import Settings
from swann_ingest.jobs import Cancelled, JobContext, NeedsReview
from swann_ingest.pipeline import steps
from swann_ingest.store.pg import connect

log = logging.getLogger("swann.worker")

IDLE_WAIT_SECONDS = 30
PURGE_EVERY_SECONDS = 3600


class Worker:
    def __init__(self, settings: Settings) -> None:
        from swann_core import qdrant as qstore
        from swann_core import s3
        from swann_core.embed.dense import resolve_device

        self.settings = settings
        self.id = f"{socket.gethostname()}:{os.getpid()}"
        self.conn = connect(settings.database_url)  # traitements
        self.ctl = connect(settings.database_url)  # suivi des tâches
        self.qc = qstore.client(settings.qdrant_url, settings.qdrant_api_key)
        self.mc = s3.client(settings.minio_endpoint, settings.minio_root_user, settings.minio_root_password)
        self.device = resolve_device(settings.device)
        self.models = steps.Models(self.device, settings.embed_batch_size, settings.align_model)
        self.current: uuid.UUID | None = None
        self.stopping = threading.Event()
        self._lock = threading.Lock()

    # ----------------------------------------------------------- présence
    def _gpu_name(self) -> str | None:
        if self.device != "cuda":
            return None
        import torch

        return torch.cuda.get_device_name(0)

    def register(self) -> None:
        self.ctl.execute(
            """
            INSERT INTO workers (id, hostname, device, gpu_name) VALUES (%s, %s, %s, %s)
            ON CONFLICT (id) DO UPDATE SET device = EXCLUDED.device, gpu_name = EXCLUDED.gpu_name,
                started_at = now(), seen_at = now(), stopped_at = NULL
            """,
            (self.id, socket.gethostname(), self.device, self._gpu_name()),
        )

    def beat(self) -> None:
        with self._lock:
            self.ctl.execute(
                "UPDATE workers SET seen_at = now(), current_job_id = %s, models = %s WHERE id = %s",
                (self.current, Jsonb(self.models.loaded()), self.id),
            )

    def unregister(self) -> None:
        with self._lock:
            self.ctl.execute(
                "UPDATE workers SET stopped_at = now(), current_job_id = NULL WHERE id = %s", (self.id,)
            )

    # -------------------------------------------------------------- boucle
    def run(self, once: bool = False) -> None:
        self.register()
        listener = psycopg.connect(self.settings.database_url, autocommit=True)
        listener.execute("LISTEN jobs")
        presence = threading.Thread(target=self._presence_loop, daemon=True, name="presence")
        presence.start()
        last_purge = 0.0
        log.info("Worker %s prêt (%s).", self.id, self.device)
        try:
            while not self.stopping.is_set():
                with self._lock:
                    if n := jobs.requeue_stale(self.ctl):
                        log.warning("%d tâche(s) abandonnée(s) remise(s) en file", n)
                if time.monotonic() - last_purge > PURGE_EVERY_SECONDS:
                    last_purge = time.monotonic()
                    self._schedule_purge()
                with self._lock:
                    row = jobs.claim(self.ctl)
                if row is None:
                    if once:
                        break
                    # Attend une notification (nouvelle tâche) ou le délai.
                    for _ in listener.notifies(timeout=IDLE_WAIT_SECONDS, stop_after=1):
                        pass
                    continue
                self.execute(row)
        finally:
            self.unregister()
            listener.close()

    def _presence_loop(self) -> None:
        while not self.stopping.wait(jobs.HEARTBEAT_SECONDS):
            try:
                self.beat()
            except psycopg.Error:
                log.exception("Battement de présence impossible")

    def _schedule_purge(self) -> None:
        due = self.ctl.execute(
            "SELECT count(*) AS n FROM editions WHERE deleted_at < now() - make_interval(days => %s)",
            (self.settings.trash_days,),
        ).fetchone()["n"]
        pending = self.ctl.execute(
            "SELECT 1 FROM jobs WHERE kind = 'purge' AND status IN ('queued', 'running')"
        ).fetchone()
        if due and not pending:
            jobs.create(self.ctl, "purge", f"Purge de la corbeille ({due} édition(s))", created_by="worker")

    def execute(self, row: dict) -> None:
        ctx = JobContext(
            row["id"],
            row["kind"],
            row["params"],
            self.ctl,
            work_id=row["work_id"],
            edition_id=row["edition_id"],
        )
        ctx._lock = self._lock
        # Reprise après validation : le rapport déjà calculé est dans `result`.
        ctx.result = row["result"] or {}
        self.current = row["id"]
        self.beat()
        log.info("Tâche %s (%s) : %s", row["id"], row["kind"], row["title"])
        started = time.monotonic()
        try:
            with jobs.heartbeat(ctx):
                result = HANDLERS[row["kind"]](self, ctx)
            ctx.finish("succeeded", result=result if result is not None else ctx.result)
            log.info("Tâche %s terminée en %.0f s", row["id"], time.monotonic() - started)
        except Cancelled:
            ctx.finish("cancelled", "annulée à la demande")
        except NeedsReview as e:
            ctx.finish("needs_review", str(e), e.result)
            log.info("Tâche %s en attente de validation : %s", row["id"], e)
        except Exception as e:  # noqa: BLE001 - toute erreur est rapportée dans la tâche
            log.error("Tâche %s en échec :\n%s", row["id"], traceback.format_exc())
            ctx.finish("failed", f"{type(e).__name__}: {e}")
            if self.conn.info.transaction_status != psycopg.pq.TransactionStatus.IDLE:
                self.conn.rollback()
        finally:
            self.current = None
            self.beat()


# ----------------------------------------------------------------- traitements
def _uuid(value) -> uuid.UUID:
    return value if isinstance(value, uuid.UUID) else uuid.UUID(str(value))


def handle_ingest(w: Worker, ctx: JobContext) -> dict:
    from swann_ingest import upload

    return upload.handle(w, ctx)


def handle_reprocess(w: Worker, ctx: JobContext) -> dict:
    return steps.reprocess_edition(w.conn, w.qc, w.models, _uuid(ctx.params["edition_id"]), ctx)


def handle_align(w: Worker, ctx: JobContext) -> dict:
    work_id = _uuid(ctx.params["work_id"])
    ctx.link(work_id=work_id)
    return steps.align_work_step(
        w.conn,
        w.models,
        work_id,
        ctx,
        force=bool(ctx.params.get("force")),
        incremental=bool(ctx.params.get("incremental", False)),
    )


def handle_sync_payload(w: Worker, ctx: JobContext) -> dict:
    from swann_ingest.pipeline.index import sync_edition_payload

    ids = [_uuid(x) for x in ctx.params.get("edition_ids", [])]
    if work_id := ctx.params.get("work_id"):
        ids += [
            r["id"]
            for r in w.conn.execute("SELECT id FROM editions WHERE work_id = %s", (work_id,)).fetchall()
        ]
    for i, edition_id in enumerate(ids, 1):
        ctx.progress("sync_payload", i, len(ids))
        sync_edition_payload(w.conn, w.qc, edition_id)
    return {"editions": len(ids)}


def handle_trash(w: Worker, ctx: JobContext) -> dict:
    edition_id = _uuid(ctx.params["edition_id"])
    result = steps.trash_edition(w.conn, w.qc, edition_id, ctx)
    work_id = w.conn.execute("SELECT work_id FROM editions WHERE id = %s", (edition_id,)).fetchone()[
        "work_id"
    ]
    # Les autres éditions s'alignaient peut-être sur celle-ci : réalignement.
    was_reference = w.conn.execute(
        "SELECT 1 FROM edition_alignments WHERE edition_id = %s AND reference_edition_id IS NULL",
        (edition_id,),
    ).fetchone()
    if was_reference:
        result["align"] = steps.align_work_step(w.conn, w.models, work_id, ctx, incremental=False)
    return result


def handle_restore(w: Worker, ctx: JobContext) -> dict:
    return steps.restore_edition(w.conn, w.qc, w.models, _uuid(ctx.params["edition_id"]), ctx)


def handle_purge(w: Worker, ctx: JobContext) -> dict:
    ids = [_uuid(x) for x in ctx.params.get("edition_ids", [])]
    return steps.purge(
        w.conn,
        w.qc,
        w.mc,
        w.settings.minio_books_bucket,
        ctx,
        older_than_days=w.settings.trash_days,
        edition_ids=ids,
    )


def handle_quality(w: Worker, ctx: JobContext) -> dict:
    if edition_id := ctx.params.get("edition_id"):
        return quality.compute(w.conn, _uuid(edition_id)) | {"edition_id": str(edition_id)}
    n = quality.compute_all(w.conn, on_progress=lambda d, t: ctx.progress("quality", d, t))
    return {"editions": n}


def handle_import_books(w: Worker, ctx: JobContext) -> dict:
    from swann_ingest.catalog import scan
    from swann_ingest.pipeline.extract import extract_catalog

    catalog = scan(w.settings.books_dir)
    errors = [f"{i.path}: {i.message}" for i in catalog.errors]
    report = extract_catalog(
        w.conn,
        w.settings,
        catalog,
        workers=max(1, (os.cpu_count() or 2) // 2),
        force=bool(ctx.params.get("force")),
        job_id=ctx.id,
        log=lambda level, msg: log.log(logging.ERROR if level == "error" else logging.INFO, msg),
        progress=lambda d, t, cur: ctx.progress("extract", d, t, cur),
        check_cancel=ctx.check_cancel,
    )
    indexed = 0
    for i, edition_id in enumerate(report.edition_ids, 1):
        ctx.check_cancel()
        ctx.progress("index", i, len(report.edition_ids))
        if steps.index_in_active(w.conn, w.qc, w.models, edition_id, ctx):
            indexed += 1
    aligned = []
    for work_id in report.work_ids:
        ctx.check_cancel()
        aligned.append(steps.align_work_step(w.conn, w.models, work_id, ctx))
    return {
        "extracted": report.ok,
        "failed": report.failed,
        "unchanged": report.unchanged,
        "indexed": indexed,
        "aligned_works": len(aligned),
        "catalog_errors": errors,
    }


HANDLERS: dict[str, Callable[[Worker, JobContext], dict | None]] = {
    "ingest": handle_ingest,
    "reprocess": handle_reprocess,
    "align": handle_align,
    "sync_payload": handle_sync_payload,
    "trash_edition": handle_trash,
    "restore_edition": handle_restore,
    "purge": handle_purge,
    "quality": handle_quality,
    "import_books": handle_import_books,
}


def main(settings: Settings, once: bool = False) -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    worker = Worker(settings)

    def stop(*_):
        log.info("Arrêt demandé : fin de la tâche en cours puis sortie.")
        worker.stopping.set()

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    worker.run(once=once)
