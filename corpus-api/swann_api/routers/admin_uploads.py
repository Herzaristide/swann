"""Dépôt d'EPUB et validation de leur classement (docs/console.md §4)."""

from __future__ import annotations

import hashlib
import json
import tempfile
import uuid
import zipfile
from typing import Annotated, Literal
from uuid import UUID

import anyio.to_thread
from fastapi import APIRouter, File, Form, Request, UploadFile
from pydantic import BaseModel, Field, ValidationError

from swann_api.admin_schemas import JobDetail
from swann_api.auth import Admin
from swann_api.deps import Conn
from swann_api.errors import Problem, not_found
from swann_api.routers.admin import create_job, jsonb, load_job
from swann_api.schemas import Access

router = APIRouter(prefix="/admin", tags=["administration"])

LANGUAGE = r"^[a-z]{2,3}(-[A-Za-z0-9]{2,8})*$"
EPUB_MIMETYPE = b"application/epub+zip"


# --------------------------------------------------------------------- modèles
class UploadHints(BaseModel):
    """Indications facultatives données au dépôt (sinon : métadonnées de l'EPUB)."""

    work_id: UUID | None = None
    language: str | None = Field(None, pattern=LANGUAGE)
    title: str | None = None
    translators: list[str] | None = None
    publisher: str | None = None
    year: int | None = None
    access: Access = "restricted"


class UploadItem(BaseModel):
    filename: str
    status: Literal["queued", "duplicate", "invalid"]
    message: str | None = None
    job_id: UUID | None = None
    sha256: str | None = None
    existing_edition_id: UUID | None = None


class UploadResult(BaseModel):
    batch_id: UUID
    items: list[UploadItem]


class EditionFields(BaseModel):
    title: str | None = None
    language: str = Field(pattern=LANGUAGE)
    is_original: bool = False
    translators: list[str] = []
    publisher: str | None = None
    year: int | None = None
    access: Access = "restricted"


class AuthorRef(BaseModel):
    """Auteur existant (`person_id`) ou à créer (`name`, Wikidata facultatif)."""

    person_id: UUID | None = None
    name: str | None = None
    wikidata_id: str | None = Field(None, pattern=r"^Q\d+$")
    birth_year: int | None = None
    death_year: int | None = None


class NewWork(BaseModel):
    title: str = Field(min_length=1)
    original_language: str | None = Field(None, pattern=LANGUAGE)
    first_published_year: int | None = None
    wikidata_id: str | None = Field(None, pattern=r"^Q\d+$")
    titles: dict[str, str] = {}
    authors: list[AuthorRef] = Field(min_length=1)


class Resolution(BaseModel):
    action: Literal["attach", "create_work", "reject"]
    work_id: UUID | None = None
    replace_edition_id: UUID | None = None
    work: NewWork | None = None
    edition: EditionFields | None = None


# ----------------------------------------------------------------------- dépôt
def _inspect(fileobj) -> str | None:
    """Raison du refus, ou None si le fichier ressemble à un EPUB."""
    try:
        with zipfile.ZipFile(fileobj) as zf:
            names = set(zf.namelist())
            if "META-INF/container.xml" not in names:
                return "pas un EPUB (META-INF/container.xml absent)"
            if "META-INF/encryption.xml" in names:
                data = zf.read("META-INF/encryption.xml")
                if b"EncryptedData" in data and b"font" not in data.lower():
                    return "EPUB protégé par DRM"
            if "mimetype" in names and zf.read("mimetype").strip() != EPUB_MIMETYPE:
                return "type MIME inattendu dans le fichier mimetype"
    except zipfile.BadZipFile:
        return "pas une archive ZIP (un EPUB est un ZIP)"
    return None


def _store(mc, bucket: str, key: str, fileobj, size: int) -> None:
    if not mc.bucket_exists(bucket):
        mc.make_bucket(bucket)
    fileobj.seek(0)
    mc.put_object(bucket, key, fileobj, size, content_type="application/epub+zip")


@router.post("/uploads", response_model=UploadResult, status_code=201, summary="Déposer des EPUB")
async def upload(
    request: Request,
    conn: Conn,
    principal: Admin,
    files: Annotated[list[UploadFile], File(description="Un ou plusieurs fichiers .epub")],
    options: Annotated[str | None, Form(description="UploadHints en JSON (facultatif)")] = None,
) -> dict:
    state = request.app.state
    if state.s3 is None:
        raise Problem(503, "storage-disabled", "Stockage S3 désactivé : dépôt impossible.")
    try:
        hints = UploadHints.model_validate(json.loads(options)) if options else UploadHints()
    except (json.JSONDecodeError, ValidationError) as e:
        raise Problem(422, "validation", f"options invalides : {e}") from e
    if (
        hints.work_id
        and not await (
            await conn.execute("SELECT 1 FROM works WHERE id = %s AND deleted_at IS NULL", (hints.work_id,))
        ).fetchone()
    ):
        raise not_found("Œuvre")

    limit = state.settings.upload_max_bytes
    batch_id = uuid.uuid4()
    items = []
    for f in files:
        name = f.filename or "sans-nom.epub"
        with tempfile.SpooledTemporaryFile(max_size=8 * 1024 * 1024) as tmp:
            digest, size = hashlib.sha256(), 0
            while chunk := await f.read(1024 * 1024):
                size += len(chunk)
                if size > limit:
                    break
                digest.update(chunk)
                tmp.write(chunk)
            if size > limit:
                items.append(
                    {"filename": name, "status": "invalid", "message": f"plus de {limit // 2**20} Mo"}
                )
                continue
            if reason := await anyio.to_thread.run_sync(_inspect, tmp):
                items.append({"filename": name, "status": "invalid", "message": reason})
                continue
            sha = digest.hexdigest()
            existing = await (
                await conn.execute(
                    "SELECT e.id, e.title, e.language, e.deleted_at FROM editions e WHERE e.sha256 = %s",
                    (sha,),
                )
            ).fetchone()
            if existing:
                where = " (dans la corbeille)" if existing["deleted_at"] else ""
                items.append(
                    {
                        "filename": name,
                        "status": "duplicate",
                        "sha256": sha,
                        "existing_edition_id": existing["id"],
                        "message": f"déjà en base{where} : {existing['title']} [{existing['language']}]",
                    }
                )
                continue
            pending = await (
                await conn.execute(
                    "SELECT id FROM jobs WHERE kind = 'ingest' AND params->>'sha256' = %s "
                    "AND status IN ('queued', 'running', 'needs_review')",
                    (sha,),
                )
            ).fetchone()
            if pending:
                items.append(
                    {
                        "filename": name,
                        "status": "duplicate",
                        "sha256": sha,
                        "job_id": pending["id"],
                        "message": "déjà déposé, en cours de traitement",
                    }
                )
                continue
            key = f"{sha}.epub"
            await anyio.to_thread.run_sync(
                _store, state.s3, state.settings.minio_inbox_bucket, key, tmp, size
            )
        job_id = await create_job(
            conn,
            "ingest",
            f"Dépôt de {name}",
            {
                "object_key": key,
                "filename": name,
                "sha256": sha,
                "size": size,
                "batch_id": str(batch_id),
                "hints": hints.model_dump(mode="json", exclude_none=True),
                "actor": principal.sub,
            },
            principal.sub,
            work_id=hints.work_id,
        )
        items.append({"filename": name, "status": "queued", "job_id": job_id, "sha256": sha})
    return {"batch_id": batch_id, "items": items}


# ------------------------------------------------------------------ validation
@router.post("/jobs/{job_id}/resolve", response_model=JobDetail, summary="Valider le classement d'un dépôt")
async def resolve(job_id: UUID, body: Resolution, request: Request, conn: Conn, principal: Admin) -> dict:
    job = await (
        await conn.execute(
            "SELECT kind::text AS kind, status::text AS status, params FROM jobs WHERE id = %s", (job_id,)
        )
    ).fetchone()
    if job is None:
        raise not_found("Tâche")
    if job["kind"] != "ingest" or job["status"] != "needs_review":
        raise Problem(409, "not-reviewable", "Seul un dépôt en attente de validation se valide.")
    if body.action == "attach":
        if body.work_id is None or body.edition is None:
            raise Problem(400, "resolution", "Rattachement : `work_id` et `edition` requis.")
        if not await (
            await conn.execute("SELECT 1 FROM works WHERE id = %s AND deleted_at IS NULL", (body.work_id,))
        ).fetchone():
            raise not_found("Œuvre")
        if (
            body.replace_edition_id
            and not await (
                await conn.execute(
                    "SELECT 1 FROM editions WHERE id = %s AND work_id = %s",
                    (body.replace_edition_id, body.work_id),
                )
            ).fetchone()
        ):
            raise Problem(400, "resolution", "L'édition à remplacer n'appartient pas à cette œuvre.")
    elif body.action == "create_work":
        if body.work is None or body.edition is None:
            raise Problem(400, "resolution", "Nouvelle œuvre : `work` et `edition` requis.")
        for a in body.work.authors:
            if a.person_id is None and not (a.name or "").strip():
                raise Problem(400, "resolution", "Chaque auteur : `person_id` ou `name`.")

    if body.action == "reject":
        state = request.app.state
        if state.s3 is not None:
            await anyio.to_thread.run_sync(
                state.s3.remove_object, state.settings.minio_inbox_bucket, job["params"]["object_key"]
            )
        await conn.execute(
            "UPDATE jobs SET status = 'cancelled', finished_at = now(), "
            "error = 'dépôt rejeté', result = coalesce(result, '{}') || %s WHERE id = %s",
            (jsonb({"rejected_by": principal.sub}), job_id),
        )
        return await load_job(conn, job_id)

    params = dict(job["params"])
    params["resolution"] = body.model_dump(mode="json")
    params["actor"] = principal.sub
    await conn.execute(
        """
        UPDATE jobs SET params = %s, status = 'queued', finished_at = NULL, error = NULL,
               cancel_requested = false, attempts = 0,
               work_id = coalesce(%s, work_id)
        WHERE id = %s
        """,
        (jsonb(params), body.work_id, job_id),
    )
    return await load_job(conn, job_id)
