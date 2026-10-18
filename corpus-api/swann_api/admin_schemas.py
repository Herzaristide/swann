"""Modèles de l'API d'administration (/v1/admin, docs/console.md §9)."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, Field

JobKind = Literal[
    "ingest",
    "reprocess",
    "align",
    "sync_payload",
    "trash_edition",
    "restore_edition",
    "purge",
    "import_books",
    "quality",
    "cli",
]
JobStatus = Literal["queued", "running", "needs_review", "succeeded", "failed", "cancelled"]


# ------------------------------------------------------------------ supervision
class ServiceHealth(BaseModel):
    status: Literal["ok", "error", "disabled"]
    detail: str | None = None


class PostgresInfo(ServiceHealth):
    version: str | None = None
    size_bytes: int | None = None
    connections: int | None = None
    migrations_applied: int | None = None
    last_migration: str | None = None


class QdrantCollection(BaseModel):
    name: str
    status: str | None = None
    points: int | None = None
    indexed_vectors: int | None = None
    segments: int | None = None
    is_active: bool = False


class QdrantInfo(ServiceHealth):
    alias: str
    alias_target: str | None = None
    collections: list[QdrantCollection] = []


class BucketInfo(BaseModel):
    name: str
    exists: bool
    objects: int | None = None
    size_bytes: int | None = None
    truncated: bool = False


class StorageInfo(ServiceHealth):
    buckets: list[BucketInfo] = []


class WorkerInfo(BaseModel):
    id: str
    hostname: str
    device: str | None
    gpu_name: str | None
    models: dict[str, Any]
    current_job_id: UUID | None
    current_job_title: str | None = None
    started_at: datetime
    seen_at: datetime
    alive: bool


class Funnel(BaseModel):
    works: int
    editions: int
    extracted: int
    indexed: int
    aligned_reliable: int
    aligned_other: int
    trashed: int


class LanguageFunnel(BaseModel):
    language: str
    editions: int
    extracted: int
    indexed: int
    aligned_reliable: int


class CountByKey(BaseModel):
    key: str
    count: int


class Overview(BaseModel):
    generated_at: datetime
    postgres: PostgresInfo
    qdrant: QdrantInfo
    storage: StorageInfo
    workers: list[WorkerInfo]
    funnel: Funnel
    languages: list[LanguageFunnel]
    jobs_by_status: list[CountByKey]
    quality_signals: list[CountByKey]
    quality_mean_score: float | None
    recent_failures: list[JobSummary]
    running: list[JobSummary]


# ------------------------------------------------------------------- tâches
class JobSummary(BaseModel):
    id: UUID
    kind: JobKind
    status: JobStatus
    title: str
    progress: dict[str, Any]
    error: str | None
    work_id: UUID | None
    work_title: str | None = None
    edition_id: UUID | None
    edition_title: str | None = None
    created_by_sub: str | None
    created_at: datetime
    started_at: datetime | None
    finished_at: datetime | None
    heartbeat_at: datetime | None
    attempts: int
    cancel_requested: bool


class Ingestion(BaseModel):
    id: UUID
    stage: str | None
    status: str
    source_file: str
    edition_id: UUID | None
    n_segments: int
    n_chunks: int
    error: str | None
    started_at: datetime
    finished_at: datetime | None


class JobDetail(JobSummary):
    params: dict[str, Any]
    result: dict[str, Any] | None
    parent_id: UUID | None
    ingestions: list[Ingestion]


class JobCreate(BaseModel):
    kind: Literal["import_books", "quality", "align", "purge", "sync_payload", "reprocess"]
    params: dict[str, Any] = Field(default_factory=dict)
    title: str | None = None


# ------------------------------------------------------------------- qualité
class QualityRow(BaseModel):
    edition_id: UUID
    title: str
    language: str
    is_original: bool
    access: str
    source_file: str
    work_id: UUID
    work_title: str
    authors: list[str]
    score: int | None
    signals: list[str]
    acked: list[str]
    signal_details: dict[str, str]
    metrics: dict[str, Any]
    structure_method: str | None
    detected_language: str | None
    parse_warnings: list[str]
    computed_at: datetime | None
    deleted_at: datetime | None


class AckRequest(BaseModel):
    signal: str
    comment: str | None = None


class SignalInfo(BaseModel):
    code: str
    count: int
    acked: int


# -------------------------------------------------------------- cohérence
class ConsistencyRow(BaseModel):
    edition_id: UUID
    title: str
    language: str
    source_file: str
    chunks: int
    indexed_points: int | None = Field(description="Points déclarés dans edition_indexings (index actif).")
    qdrant_points: int | None = Field(description="Points réellement présents dans Qdrant.")
    epub_key: str | None
    epub_present: bool | None
    problems: list[str]


class Consistency(BaseModel):
    active_collection: str | None
    checked: int
    with_problems: int
    rows: list[ConsistencyRow]


# ------------------------------------------------------------------- index
class AdminIndex(BaseModel):
    id: UUID
    collection: str
    dense_model: str
    dense_dim: int
    sparse_model: str | None
    chunker_version: str
    status: str
    editions: int
    points: int
    pending: int
    created_at: datetime


# --------------------------------------------------------------- historique
class Event(BaseModel):
    id: int
    at: datetime
    type: str
    work_id: UUID | None
    edition_id: UUID | None
    actor: str | None
    data: dict[str, Any]


Overview.model_rebuild()
