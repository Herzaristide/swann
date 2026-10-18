"""Stockage S3 (Garage aujourd'hui) des fichiers EPUB des éditions.

Clé = empreinte du fichier ("editions/<sha256>.epub") : un objet n'est jamais
modifié, un fichier remplacé donne un nouvel objet et l'ancien est supprimé.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse

from minio import Minio
from minio.error import S3Error

EPUB_CONTENT_TYPE = "application/epub+zip"
CHUNK_SIZE = 64 * 1024


def client(endpoint: str | None, access_key: str, secret_key: str) -> Minio | None:
    """None si aucun endpoint n'est configuré (stockage désactivé)."""
    if not endpoint:
        return None
    url = urlparse(endpoint)
    return Minio(url.netloc, access_key=access_key, secret_key=secret_key, secure=url.scheme == "https")


def ensure_bucket(mc: Minio, bucket: str) -> None:
    if not mc.bucket_exists(bucket):
        mc.make_bucket(bucket)


def epub_key(sha256: str) -> str:
    return f"editions/{sha256}.epub"


def _missing(e: S3Error) -> bool:
    return e.code in ("NoSuchKey", "NoSuchObject", "NoSuchBucket")


def exists(mc: Minio, bucket: str, key: str) -> bool:
    try:
        mc.stat_object(bucket, key)
    except S3Error as e:
        if _missing(e):
            return False
        raise
    return True


def put_epub(mc: Minio, bucket: str, path: Path, sha256: str) -> str:
    """Envoie l'EPUB s'il n'est pas déjà stocké ; renvoie sa clé."""
    key = epub_key(sha256)
    if not exists(mc, bucket, key):
        mc.fput_object(bucket, key, str(path), content_type=EPUB_CONTENT_TYPE)
    return key


def remove(mc: Minio, bucket: str, key: str) -> None:
    mc.remove_object(bucket, key)


@dataclass
class ObjectStream:
    size: int  # taille totale de l'objet
    start: int  # premier octet servi
    length: int  # nombre d'octets servis
    chunks: Iterator[bytes]


def open_object(
    mc: Minio, bucket: str, key: str, start: int = 0, length: int | None = None
) -> ObjectStream | None:
    """Ouvre l'objet (ou la plage [start, start + length[) en flux ; None s'il
    n'existe pas. La taille est lue avant d'ouvrir le flux pour valider la plage."""
    try:
        size = mc.stat_object(bucket, key).size or 0
    except S3Error as e:
        if _missing(e):
            return None
        raise
    if length is None:
        length = size - start
    response = mc.get_object(bucket, key, offset=start, length=length) if length else None

    def chunks() -> Iterator[bytes]:
        if response is None:
            return
        try:
            yield from response.stream(CHUNK_SIZE)
        finally:
            response.close()
            response.release_conn()

    return ObjectStream(size=size, start=start, length=length, chunks=chunks())


def object_size(mc: Minio, bucket: str, key: str) -> int | None:
    try:
        return mc.stat_object(bucket, key).size
    except S3Error as e:
        if _missing(e):
            return None
        raise
