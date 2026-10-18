"""Dépendances communes : connexion Postgres, langue d'affichage, pagination."""

from __future__ import annotations

import base64
import json
from collections.abc import AsyncIterator
from typing import Annotated, Any

from fastapi import Depends, Header, Query, Request
from psycopg import AsyncConnection

from swann_api.errors import Problem


async def get_conn(request: Request) -> AsyncIterator[AsyncConnection]:
    async with request.app.state.pool.connection() as conn:
        yield conn


Conn = Annotated[AsyncConnection, Depends(get_conn)]


def display_language(
    lang: Annotated[
        str | None, Query(description="Langue d'affichage (BCP 47), sinon Accept-Language.")
    ] = None,
    accept_language: Annotated[str | None, Header()] = None,
) -> str | None:
    """Langue des titres, noms et libellés. None = valeurs de référence."""
    if lang:
        return lang
    if accept_language:
        first = accept_language.split(",")[0].split(";")[0].strip()
        if first and first != "*":
            return first
    return None


Lang = Annotated[str | None, Depends(display_language)]


# ------------------------------------------------------------------ curseurs
def encode_cursor(values: dict[str, Any]) -> str:
    raw = json.dumps(values, separators=(",", ":"), default=str).encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def decode_cursor(cursor: str | None) -> dict[str, Any] | None:
    if not cursor:
        return None
    try:
        raw = base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4))
        value = json.loads(raw)
    except (ValueError, json.JSONDecodeError) as e:
        raise Problem(400, "invalid-cursor", "Curseur invalide.") from e
    if not isinstance(value, dict):
        raise Problem(400, "invalid-cursor", "Curseur invalide.")
    return value


Limit = Annotated[int, Query(ge=1, le=100, description="Nombre d'éléments (max 100).")]
Cursor = Annotated[str | None, Query(description="Curseur de la page suivante (`next_cursor`).")]
