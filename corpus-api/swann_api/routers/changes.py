"""Flux ordonné des changements du corpus (table corpus_events)."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Query

from swann_api.auth import Reader
from swann_api.deps import Conn
from swann_api.errors import Problem
from swann_api.schemas import Changes

router = APIRouter(tags=["changements"])


@router.get("/changes", response_model=Changes, summary="Changements du corpus depuis un curseur")
async def list_changes(
    conn: Conn,
    _: Reader,
    after: Annotated[str | None, Query(description="Dernier curseur lu ; vide = depuis le début.")] = None,
    limit: Annotated[int, Query(ge=1, le=1000)] = 100,
    type: Annotated[  # noqa: A002 - nom public du paramètre
        list[str] | None,
        Query(description="Types gardés ; « edition. » = tous les types qui commencent ainsi."),
    ] = None,
) -> dict:
    try:
        after_id = int(after) if after else 0
    except ValueError as e:
        raise Problem(400, "invalid-cursor", "Curseur invalide.") from e
    prefixes = [t for t in type or [] if t.endswith(".")]
    exact = [t for t in type or [] if not t.endswith(".")]
    rows = await (
        await conn.execute(
            """
            SELECT id, at, type, work_id, edition_id, data FROM corpus_events
            WHERE id > %(after)s
              AND (%(any)s OR type = ANY(%(exact)s) OR type LIKE ANY(%(prefixes)s))
            ORDER BY id LIMIT %(limit)s
            """,
            {
                "after": after_id,
                "any": not type,
                "exact": exact,
                "prefixes": [p.replace("%", "\\%").replace("_", "\\_") + "%" for p in prefixes],
                "limit": limit,
            },
        )
    ).fetchall()
    items = [{**r, "cursor": str(r.pop("id"))} for r in rows]
    # Rien de nouveau : le client garde son curseur.
    next_cursor = items[-1]["cursor"] if items else (str(after_id) if after else None)
    return {"items": items, "next_cursor": next_cursor}
