"""Application FastAPI de la Corpus API (préfixe /v1)."""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import APIRouter, FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.gzip import GZipMiddleware
from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool
from qdrant_client import AsyncQdrantClient

from swann_api import errors
from swann_api.auth import TokenVerifier
from swann_api.config import Settings, get_settings
from swann_api.engine import SearchEngine
from swann_api.routers import (
    admin,
    admin_alignment,
    admin_catalog,
    admin_uploads,
    alignment,
    catalog,
    changes,
    editions,
    meta,
    search,
)
from swann_core import s3

log = logging.getLogger("swann_api")


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        pool = AsyncConnectionPool(
            settings.database_url,
            min_size=settings.db_pool_min,
            max_size=settings.db_pool_max,
            # jit=off : la compilation JIT coûte ~300 ms sur les requêtes
            # récursives (courants, sections) pour un gain nul en lecture courte.
            kwargs={"row_factory": dict_row, "autocommit": True, "options": "-c jit=off"},
            open=False,
        )
        await pool.open(wait=True, timeout=30)
        qdrant = AsyncQdrantClient(url=settings.qdrant_url, api_key=settings.qdrant_api_key or None)
        app.state.settings = settings
        app.state.pool = pool
        app.state.qdrant = qdrant
        app.state.s3 = s3.client(
            settings.minio_endpoint, settings.minio_root_user, settings.minio_root_password
        )
        app.state.verifier = TokenVerifier(settings)
        app.state.engine = SearchEngine(settings, qdrant, pool)
        if settings.auth_disabled:
            log.warning("AUTH_DISABLED=true : aucun jeton vérifié, tous les droits accordés (développement).")
        try:
            await app.state.engine.refresh(force=True)
        except Exception:  # l'API reste utile sans recherche ; /v1/health le signale
            log.exception("Index vectoriel actif introuvable")
        try:
            yield
        finally:
            await qdrant.close()
            await pool.close()

    app = FastAPI(
        title="Swann — Corpus API",
        version="1",
        description="Accès au corpus littéraire de Swann : catalogue, texte des éditions, "
        "recherche (thème, citation, mots), alignement des traductions, flux des changements.",
        lifespan=lifespan,
        openapi_url="/v1/openapi.json",
        docs_url="/v1/docs",
        redoc_url=None,
    )
    errors.install(app)
    app.add_middleware(GZipMiddleware, minimum_size=1024)
    if settings.cors_origins:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=settings.cors_origins,
            allow_methods=["GET", "HEAD", "POST", "PATCH", "PUT", "DELETE"],
            allow_headers=[
                "Authorization",
                "Content-Type",
                "If-None-Match",
                "If-Match",
                "Range",
                "Accept-Language",
            ],
            expose_headers=["ETag", "Content-Range", "Content-Disposition"],
        )

    v1 = APIRouter(prefix="/v1")
    for module in (
        meta,
        catalog,
        editions,
        search,
        alignment,
        changes,
        admin,
        admin_uploads,
        admin_catalog,
        admin_alignment,
    ):
        v1.include_router(module.router)
    app.include_router(v1)
    return app


def run() -> None:
    import uvicorn

    uvicorn.run("swann_api.main:create_app", factory=True, host="0.0.0.0", port=8000)
