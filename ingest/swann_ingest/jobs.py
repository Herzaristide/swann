"""File de tâches dans Postgres (table `jobs`, docs/console.md §3).

- La console (via l'API) et la CLI créent des tâches ; `swann worker` les
  prend une à une avec SELECT … FOR UPDATE SKIP LOCKED.
- Le suivi d'une tâche (statut, avancement, battement de cœur) passe par une
  connexion à part : il reste visible même pendant une longue transaction du
  traitement lui-même.
- Toute insertion ou mise à jour d'avancement est notifiée sur le canal
  `jobs` (déclencheur en base) : le worker se réveille, l'API relaie en SSE.
"""

from __future__ import annotations

import contextlib
import json
import threading
import time
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any

import psycopg
from psycopg.types.json import Jsonb

from swann_ingest.store.pg import connect

HEARTBEAT_SECONDS = 10
# Une tâche `running` sans battement depuis ce délai est considérée comme
# abandonnée (worker arrêté brutalement) et remise en file.
STALE_SECONDS = 60
MAX_ATTEMPTS = 3


def to_jsonb(value) -> Jsonb:
    """JSONB tolérant (UUID, dates, Decimal → texte) pour params / résultats."""
    return Jsonb(value, dumps=lambda obj: json.dumps(obj, default=str, ensure_ascii=False))


class Cancelled(Exception):
    """L'admin a demandé l'annulation de la tâche."""


class NeedsReview(Exception):
    """La tâche attend une décision humaine (classement d'un dépôt…).
    `result` est enregistré et montré dans la console."""

    def __init__(self, message: str, result: dict) -> None:
        super().__init__(message)
        self.result = result


def create(
    conn: psycopg.Connection,
    kind: str,
    title: str,
    params: dict | None = None,
    *,
    created_by: str | None = None,
    work_id: uuid.UUID | None = None,
    edition_id: uuid.UUID | None = None,
    parent_id: uuid.UUID | None = None,
    status: str = "queued",
) -> uuid.UUID:
    return conn.execute(
        """
        INSERT INTO jobs (kind, title, params, created_by_sub, work_id, edition_id, parent_id, status,
                          started_at, heartbeat_at)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s,
                CASE WHEN %s = 'running' THEN now() END, CASE WHEN %s = 'running' THEN now() END)
        RETURNING id
        """,
        (
            kind,
            title,
            to_jsonb(params or {}),
            created_by,
            work_id,
            edition_id,
            parent_id,
            status,
            status,
            status,
        ),
    ).fetchone()["id"]


def claim(conn: psycopg.Connection) -> dict | None:
    """Prend la plus ancienne tâche en file (sauf les exécutions CLI, suivies
    mais pas exécutées par le worker)."""
    return conn.execute(
        """
        UPDATE jobs SET status = 'running', started_at = coalesce(started_at, now()),
               heartbeat_at = now(), attempts = attempts + 1, error = NULL
        WHERE id = (
            SELECT id FROM jobs WHERE status = 'queued' AND kind <> 'cli'
            ORDER BY created_at FOR UPDATE SKIP LOCKED LIMIT 1
        )
        RETURNING *
        """
    ).fetchone()


def requeue_stale(conn: psycopg.Connection) -> int:
    """Tâches `running` abandonnées : remises en file (ou en échec après
    MAX_ATTEMPTS ; une exécution CLI interrompue passe en échec)."""
    rows = conn.execute(
        """
        UPDATE jobs SET
            status = CASE WHEN kind = 'cli' OR attempts >= %(max)s
                          THEN 'failed' ELSE 'queued' END::job_status,
            error = CASE WHEN kind = 'cli' OR attempts >= %(max)s
                         THEN 'processus interrompu (plus de battement de cœur)' END,
            finished_at = CASE WHEN kind = 'cli' OR attempts >= %(max)s THEN now() END
        WHERE status = 'running' AND heartbeat_at < now() - make_interval(secs => %(stale)s)
        RETURNING id
        """,
        {"max": MAX_ATTEMPTS, "stale": STALE_SECONDS},
    ).fetchall()
    return len(rows)


def request_cancel(conn: psycopg.Connection, job_id: uuid.UUID) -> None:
    conn.execute(
        """
        UPDATE jobs SET
            cancel_requested = true,
            status = CASE WHEN status IN ('queued', 'needs_review') THEN 'cancelled' ELSE status END,
            finished_at = CASE WHEN status IN ('queued', 'needs_review') THEN now() ELSE finished_at END
        WHERE id = %s AND status IN ('queued', 'running', 'needs_review')
        """,
        (job_id,),
    )


@dataclass
class JobContext:
    """Suivi d'une tâche en cours, passé aux traitements."""

    id: uuid.UUID
    kind: str
    params: dict
    ctl: psycopg.Connection  # connexion de suivi (autocommit)
    result: dict = field(default_factory=dict)
    work_id: uuid.UUID | None = None
    edition_id: uuid.UUID | None = None
    _step: str = ""
    _last_progress: float = 0.0
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def progress(
        self,
        step: str | None = None,
        done: int | None = None,
        total: int | None = None,
        message: str | None = None,
        force: bool = False,
        **extra: Any,
    ) -> None:
        """Enregistre l'avancement (au plus 2 fois par seconde, sauf
        changement d'étape ou `force`)."""
        now = time.monotonic()
        new_step = step is not None and step != self._step
        if not (force or new_step or now - self._last_progress >= 0.5):
            return
        if step is not None:
            self._step = step
        self._last_progress = now
        value = {"step": self._step, "done": done, "total": total, "message": message, **extra}
        with self._lock:
            self.ctl.execute(
                "UPDATE jobs SET progress = %s, heartbeat_at = now() WHERE id = %s",
                (to_jsonb({k: v for k, v in value.items() if v is not None}), self.id),
            )

    def check_cancel(self) -> None:
        with self._lock:
            row = self.ctl.execute("SELECT cancel_requested FROM jobs WHERE id = %s", (self.id,)).fetchone()
        if row and row["cancel_requested"]:
            raise Cancelled()

    def link(self, *, work_id: uuid.UUID | None = None, edition_id: uuid.UUID | None = None) -> None:
        """Rattache la tâche à l'œuvre / l'édition traitée (filtres de la console)."""
        self.work_id = work_id or self.work_id
        self.edition_id = edition_id or self.edition_id
        with self._lock:
            self.ctl.execute(
                "UPDATE jobs SET work_id = coalesce(%s, work_id), edition_id = coalesce(%s, edition_id) "
                "WHERE id = %s",
                (work_id, edition_id, self.id),
            )

    def heartbeat(self) -> None:
        with self._lock:
            self.ctl.execute("UPDATE jobs SET heartbeat_at = now() WHERE id = %s", (self.id,))

    def finish(self, status: str, error: str | None = None, result: dict | None = None) -> None:
        with self._lock:
            self.ctl.execute(
                """
                UPDATE jobs SET status = %s, error = %s, result = %s,
                       finished_at = CASE WHEN %s = 'needs_review' THEN NULL ELSE now() END
                WHERE id = %s
                """,
                (status, error, to_jsonb(result if result is not None else self.result), status, self.id),
            )


@contextmanager
def heartbeat(ctx: JobContext) -> Iterator[None]:
    """Battement de cœur en tâche de fond tant que le bloc s'exécute."""
    stop = threading.Event()

    def beat() -> None:
        while not stop.wait(HEARTBEAT_SECONDS):
            with contextlib.suppress(psycopg.Error):
                ctx.heartbeat()

    thread = threading.Thread(target=beat, daemon=True, name=f"heartbeat-{ctx.id}")
    thread.start()
    try:
        yield
    finally:
        stop.set()
        thread.join(timeout=2)


@contextmanager
def track_cli(database_url: str, title: str, params: dict | None = None) -> Iterator[JobContext | None]:
    """Rend visible dans la console une commande `swann` lancée à la main
    (tâche `cli`). Sans table `jobs` (base pas encore migrée), ne fait rien."""
    try:
        ctl = connect(database_url)
        job_id = create(ctl, "cli", title, params, created_by="cli", status="running")
    except psycopg.Error:
        yield None
        return
    ctx = JobContext(job_id, "cli", params or {}, ctl)
    try:
        with heartbeat(ctx):
            yield ctx
    except BaseException as e:
        # typer.Exit (click) porte `exit_code` ; SystemExit porte `code`.
        code = getattr(e, "exit_code", getattr(e, "code", 1))
        failed = type(e).__name__ not in ("Exit", "SystemExit") or bool(code)
        if isinstance(e, KeyboardInterrupt):
            ctx.finish("cancelled", "interrompu (Ctrl+C)")
        elif failed:
            ctx.finish("failed", _error_text(e))
        else:
            ctx.finish("succeeded")
        raise
    else:
        ctx.finish("succeeded")
    finally:
        ctl.close()


def _error_text(e: BaseException) -> str:
    # typer.Exit / SystemExit(1) : l'erreur détaillée est déjà dans `ingestions`.
    if isinstance(e, SystemExit) or type(e).__name__ == "Exit":
        return "terminé avec des échecs (voir les ingestions)"
    return f"{type(e).__name__}: {e}"
