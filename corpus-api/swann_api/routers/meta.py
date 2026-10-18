"""Service : santé et description de l'API."""

from __future__ import annotations

from importlib.metadata import version

import anyio
import anyio.to_thread
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from swann_api.auth import Reader
from swann_api.schemas import Meta

router = APIRouter(tags=["service"])


@router.get("/health", summary="Santé des dépendances (sans jeton)")
async def health(request: Request) -> JSONResponse:
    state = request.app.state
    checks: dict[str, str] = {}
    try:
        async with state.pool.connection(timeout=2) as conn:
            await conn.execute("SELECT 1")
        checks["postgres"] = "ok"
    except Exception as e:  # noqa: BLE001 - rapporté tel quel
        checks["postgres"] = f"erreur : {type(e).__name__}"
    try:
        await state.qdrant.get_aliases()
        checks["qdrant"] = "ok"
    except Exception as e:  # noqa: BLE001
        checks["qdrant"] = f"erreur : {type(e).__name__}"
    if state.s3 is None:
        checks["s3"] = "désactivé"
    else:
        try:
            ok = await anyio.to_thread.run_sync(state.s3.bucket_exists, state.settings.minio_books_bucket)
            checks["s3"] = "ok" if ok else "bucket absent"
        except Exception as e:  # noqa: BLE001
            checks["s3"] = f"erreur : {type(e).__name__}"
    engine = state.engine
    checks["encoder"] = (
        "ok" if engine.encoder else ("désactivé" if not state.settings.load_encoder else "absent")
    )
    healthy = checks["postgres"] == "ok" and checks["qdrant"] == "ok"
    return JSONResponse(
        {"status": "ok" if healthy else "degraded", "checks": checks}, 200 if healthy else 503
    )


@router.get("/meta", response_model=Meta, summary="Version de l'API et index actif")
async def meta(request: Request, _: Reader) -> Meta:
    engine = request.app.state.engine
    index = await engine.refresh()
    return Meta(
        api_version=version("swann-api"),
        index=None
        if index is None
        else {
            "collection": index.collection,
            "dense_model": index.dense_model,
            "dense_dim": index.dense_dim,
            "sparse_model": index.sparse_model,
            "chunker_version": index.chunker_version,
        },
    )
