"""Qdrant : une collection par index vectoriel, un alias pour l'index actif."""

from __future__ import annotations

import uuid
from collections.abc import Iterable

from qdrant_client import QdrantClient, models

DENSE = "dense"
SPARSE = "bm25"

PAYLOAD_INDEXES = {
    "work_id": models.PayloadSchemaType.KEYWORD,
    "edition_id": models.PayloadSchemaType.KEYWORD,
    "language": models.PayloadSchemaType.KEYWORD,
    "is_original": models.PayloadSchemaType.BOOL,
    # Droits sur le texte (editions.access) : la recherche exclut les éditions
    # "restricted". Mis à jour sans réindexer (set_edition_payload).
    "access": models.PayloadSchemaType.KEYWORD,
    "author_ids": models.PayloadSchemaType.KEYWORD,
    "movement_ids": models.PayloadSchemaType.KEYWORD,
    "first_published_year": models.PayloadSchemaType.INTEGER,
    "section_kind": models.PayloadSchemaType.KEYWORD,
}


def client(url: str, api_key: str | None = None) -> QdrantClient:
    return QdrantClient(url=url, api_key=api_key or None, prefer_grpc=False, timeout=120)


def ensure_collection(qc: QdrantClient, name: str, dim: int) -> None:
    if qc.collection_exists(name):
        return
    qc.create_collection(
        collection_name=name,
        vectors_config={DENSE: models.VectorParams(size=dim, distance=models.Distance.COSINE)},
        sparse_vectors_config={SPARSE: models.SparseVectorParams(modifier=models.Modifier.IDF)},
    )
    ensure_payload_indexes(qc, name)


def ensure_payload_indexes(qc: QdrantClient, name: str) -> None:
    """Crée les index de payload manquants (collections créées avant l'ajout
    d'un champ)."""
    existing = qc.get_collection(name).payload_schema or {}
    for field_name, schema in PAYLOAD_INDEXES.items():
        if field_name not in existing:
            qc.create_payload_index(name, field_name=field_name, field_schema=schema)


def set_edition_payload(qc: QdrantClient, collection: str, edition_id: uuid.UUID, payload: dict) -> None:
    """Modifie des champs du payload de tous les points d'une édition."""
    qc.set_payload(
        collection_name=collection,
        payload=payload,
        points=models.FilterSelector(
            filter=models.Filter(
                must=[models.FieldCondition(key="edition_id", match=models.MatchValue(value=str(edition_id)))]
            )
        ),
        wait=True,
    )


def delete_edition(qc: QdrantClient, collection: str, edition_id: uuid.UUID) -> None:
    qc.delete(
        collection_name=collection,
        points_selector=models.FilterSelector(
            filter=models.Filter(
                must=[models.FieldCondition(key="edition_id", match=models.MatchValue(value=str(edition_id)))]
            )
        ),
        wait=True,
    )


def upsert(
    qc: QdrantClient, collection: str, points: Iterable[models.PointStruct], batch_size: int = 256
) -> None:
    batch: list[models.PointStruct] = []
    for p in points:
        batch.append(p)
        if len(batch) >= batch_size:
            qc.upsert(collection_name=collection, points=batch, wait=False)
            batch = []
    if batch:
        qc.upsert(collection_name=collection, points=batch, wait=True)


def alias_target(qc: QdrantClient, alias: str) -> str | None:
    for a in qc.get_aliases().aliases:
        if a.alias_name == alias:
            return a.collection_name
    return None


def set_alias(qc: QdrantClient, alias: str, collection: str) -> None:
    """Bascule atomique de l'alias vers la collection."""
    ops: list = []
    if alias_target(qc, alias) is not None:
        ops.append(models.DeleteAliasOperation(delete_alias=models.DeleteAlias(alias_name=alias)))
    ops.append(
        models.CreateAliasOperation(
            create_alias=models.CreateAlias(collection_name=collection, alias_name=alias)
        )
    )
    qc.update_collection_aliases(change_aliases_operations=ops)
