"""Cache SQLite de `swann prepare` : inventaire des EPUB (clé = sha256) et
réponses HTTP (Wikidata, Wikisource)."""

from __future__ import annotations

import json
import sqlite3
import threading
from pathlib import Path


class Cache:
    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path, check_same_thread=False)
        self.lock = threading.Lock()
        self.db.executescript(
            """
            PRAGMA journal_mode = WAL;
            CREATE TABLE IF NOT EXISTS books (sha TEXT PRIMARY KEY, version INTEGER, data TEXT);
            CREATE TABLE IF NOT EXISTS http (key TEXT PRIMARY KEY, value TEXT);
            CREATE TABLE IF NOT EXISTS vectors (key TEXT PRIMARY KEY, value BLOB);
            CREATE TABLE IF NOT EXISTS files (name TEXT PRIMARY KEY, size INTEGER, mtime REAL, sha TEXT);
            """
        )

    # ------------------------------------------------------------ fichiers
    def file_sha(self, name: str, size: int, mtime: float) -> str | None:
        row = self.db.execute(
            "SELECT sha FROM files WHERE name = ? AND size = ? AND mtime = ?", (name, size, mtime)
        ).fetchone()
        return row[0] if row else None

    def put_file(self, name: str, size: int, mtime: float, sha: str) -> None:
        with self.lock:
            self.db.execute(
                "INSERT OR REPLACE INTO files (name, size, mtime, sha) VALUES (?, ?, ?, ?)",
                (name, size, mtime, sha),
            )
            self.db.commit()

    # -------------------------------------------------------------- livres
    def book(self, sha: str, version: int) -> dict | None:
        row = self.db.execute(
            "SELECT data FROM books WHERE sha = ? AND version = ?", (sha, version)
        ).fetchone()
        return json.loads(row[0]) if row else None

    def put_book(self, sha: str, version: int, data: dict) -> None:
        with self.lock:
            self.db.execute(
                "INSERT OR REPLACE INTO books (sha, version, data) VALUES (?, ?, ?)",
                (sha, version, json.dumps(data, ensure_ascii=False)),
            )
            self.db.commit()

    # ---------------------------------------------------------------- HTTP
    def http(self, key: str) -> dict | None:
        row = self.db.execute("SELECT value FROM http WHERE key = ?", (key,)).fetchone()
        return json.loads(row[0]) if row else None

    def put_http(self, key: str, value: dict) -> None:
        with self.lock:
            self.db.execute(
                "INSERT OR REPLACE INTO http (key, value) VALUES (?, ?)",
                (key, json.dumps(value, ensure_ascii=False)),
            )
            self.db.commit()

    # ------------------------------------------------------------- vecteurs
    def vectors(self, keys: list[str]) -> dict:
        import numpy as np

        out = {}
        for i in range(0, len(keys), 500):
            batch = keys[i : i + 500]
            rows = self.db.execute(
                f"SELECT key, value FROM vectors WHERE key IN ({','.join('?' * len(batch))})", batch
            ).fetchall()
            out.update({k: np.frombuffer(v, dtype=np.float32) for k, v in rows})
        return out

    def put_vectors(self, vectors: dict) -> None:
        with self.lock:
            self.db.executemany(
                "INSERT OR REPLACE INTO vectors (key, value) VALUES (?, ?)",
                [(k, v.astype("float32").tobytes()) for k, v in vectors.items()],
            )
            self.db.commit()
