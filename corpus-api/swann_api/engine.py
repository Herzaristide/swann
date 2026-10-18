"""Index vectoriel actif et encodeur de requêtes.

L'index actif est la collection vers laquelle pointe l'alias Qdrant
(`swann index activate`). Il est revérifié toutes les 30 s : si l'alias change
de collection, la recherche suit ; si le modèle dense change, l'encodeur est
rechargé (les requêtes attendent la fin du chargement).
"""

from __future__ import annotations

import time
from dataclasses import dataclass

import anyio
import anyio.to_thread
import numpy as np
from psycopg_pool import AsyncConnectionPool
from qdrant_client import AsyncQdrantClient

from swann_api.config import Settings
from swann_api.errors import Problem

RECHECK_SECONDS = 30


@dataclass(frozen=True)
class ActiveIndex:
    collection: str
    dense_model: str
    dense_dim: int
    sparse_model: str | None
    chunker_version: str


class SearchEngine:
    def __init__(self, settings: Settings, qdrant: AsyncQdrantClient, pool: AsyncConnectionPool) -> None:
        self.settings = settings
        self.qdrant = qdrant
        self.pool = pool
        self.index: ActiveIndex | None = None
        self.encoder = None  # swann_core.embed.dense.DenseEncoder
        self._checked_at = 0.0
        self._lock = anyio.Lock()
        # Un seul encodage à la fois : le modèle n'est pas fait pour être
        # appelé en parallèle depuis plusieurs threads.
        self._encode_limiter = anyio.CapacityLimiter(1)

    async def _resolve(self) -> ActiveIndex | None:
        alias = self.settings.qdrant_collection
        aliases = await self.qdrant.get_aliases()
        collection = next((a.collection_name for a in aliases.aliases if a.alias_name == alias), alias)
        async with self.pool.connection() as conn:
            row = await (
                await conn.execute(
                    "SELECT collection, dense_model, dense_dim, sparse_model, chunker_version "
                    "FROM vector_indexes WHERE collection = %s",
                    (collection,),
                )
            ).fetchone()
        return ActiveIndex(**row) if row else None

    async def _load_encoder(self, model_name: str) -> None:
        from swann_core.embed.dense import DenseEncoder

        def load() -> DenseEncoder:
            encoder = DenseEncoder(model_name, self.settings.api_device or None, batch_size=1)
            # Chauffe : le premier appel (initialisation CUDA, compilation des
            # noyaux) coûte plusieurs secondes ; autant le payer au démarrage.
            encoder.encode_query("chauffe", "theme")
            return encoder

        self.encoder = await anyio.to_thread.run_sync(load)

    async def refresh(self, force: bool = False) -> ActiveIndex | None:
        async with self._lock:
            if not force and time.monotonic() - self._checked_at < RECHECK_SECONDS:
                return self.index
            index = await self._resolve()
            if (
                self.settings.load_encoder
                and index
                and (
                    self.encoder is None or self.index is None or index.dense_model != self.index.dense_model
                )
            ):
                await self._load_encoder(index.dense_model)
            self.index = index
            self._checked_at = time.monotonic()
            return index

    async def active(self) -> ActiveIndex:
        index = await self.refresh()
        if index is None:
            raise Problem(503, "no-index", "Aucun index vectoriel actif (swann index activate).")
        return index

    async def encode_query(self, text: str, mode: str) -> list[float]:
        await self.active()
        if self.encoder is None:
            raise Problem(503, "no-encoder", "Recherche sémantique désactivée (LOAD_ENCODER=false).")
        encoder = self.encoder
        vector: np.ndarray = await anyio.to_thread.run_sync(
            encoder.encode_query, text, mode, limiter=self._encode_limiter
        )
        return vector.tolist()
