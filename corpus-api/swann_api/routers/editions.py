"""Éditions et texte : fiche, table des matières, segments, notes, position,
recherche dans le livre, EPUB brut, recalage des ancres."""

from __future__ import annotations

import bisect
import re
from pathlib import PurePosixPath
from typing import Annotated, Literal
from uuid import UUID

import anyio
import anyio.to_thread
from fastapi import APIRouter, Query, Request, Response
from fastapi.responses import StreamingResponse

from swann_api import sql
from swann_api.auth import Principal, Reader
from swann_api.deps import Conn, Lang
from swann_api.errors import Problem, forbidden, not_found
from swann_api.schemas import (
    AnchorResult,
    AnchorsRequest,
    AnchorsResponse,
    EditionDetail,
    FindResult,
    Note,
    Position,
    SegmentRange,
    Toc,
)
from swann_api.text import find_all, locate_quote, snippet
from swann_core import s3

router = APIRouter(prefix="/editions/{edition_id}", tags=["éditions et texte"])

MAX_SEGMENTS = 500
IMMUTABLE = "private, max-age=31536000, immutable"
REVALIDATE = "private, no-cache"

Rev = Annotated[
    int | None,
    Query(description="Révision attendue : réponse immuable (cache long) ; 409 si l'édition a changé."),
]


# ------------------------------------------------------------------- communs
async def load_edition(conn: Conn, edition_id: UUID, principal: Principal) -> dict:
    """Édition visible par l'appelant, sinon 404 (une édition `restricted` ne
    se distingue pas d'une édition absente)."""
    row = await (
        await conn.execute(
            """
            SELECT e.id, e.work_id, e.title, e.language, e.revision, e.access::text AS access,
                   e.sha256, e.epub_object_key, e.source_file, w.slug
            FROM editions e JOIN works w ON w.id = e.work_id
            WHERE e.id = %s AND e.deleted_at IS NULL
            """,
            (edition_id,),
        )
    ).fetchone()
    if row is None or not principal.can_see(row["access"]):
        raise not_found("Édition")
    return row


async def load_text_edition(conn: Conn, edition_id: UUID, principal: Principal, rev: int | None) -> dict:
    edition = await load_edition(conn, edition_id, principal)
    if not principal.can_read_text(edition["access"]):
        raise forbidden(f"Texte intégral non disponible (access = {edition['access']}).")
    check_revision(edition, rev)
    return edition


def check_revision(edition: dict, rev: int | None) -> None:
    if rev is not None and rev != edition["revision"]:
        raise Problem(
            409,
            "revision-mismatch",
            f"L'édition est en révision {edition['revision']}, la requête vise la {rev}.",
            title="Révision obsolète",
            current_revision=edition["revision"],
        )


def cache(request: Request, response: Response, etag: str, rev: int | None) -> bool:
    """Pose ETag et Cache-Control ; True si le client a déjà cette version (304)."""
    response.headers["ETag"] = etag
    response.headers["Cache-Control"] = IMMUTABLE if rev is not None else REVALIDATE
    return etag in [t.strip() for t in request.headers.get("if-none-match", "").split(",")]


def not_modified(response: Response) -> Response:
    return Response(status_code=304, headers=dict(response.headers))


def text_etag(edition: dict, suffix: str = "") -> str:
    return f'"{edition["id"]}:{edition["revision"]}{suffix}"'


# ---------------------------------------------------------------------- fiche
@router.get("", response_model=EditionDetail, summary="Fiche détaillée d'une édition")
async def get_edition(edition_id: UUID, conn: Conn, principal: Reader, lang: Lang) -> dict:
    p = {"lang": lang, "access": list(principal.visible_access), "edition_id": edition_id}
    row = await (
        await conn.execute(
            f"""
            SELECT {sql.EDITION_SUMMARY}, e.n_pages,
                   json_build_object('id', w.id, 'title', coalesce(wt.title, w.title),
                                     'first_published_year', w.first_published_year) AS work,
                   (SELECT coalesce(json_agg(json_build_object(
                                'id', p.id, 'name', {sql.person_name()}, 'role', c.role)
                            ORDER BY c.role, c.position), '[]')
                    FROM edition_contributors c JOIN persons p ON p.id = c.person_id
                    WHERE c.edition_id = e.id) AS contributors,
                   (SELECT count(*) FROM segments s WHERE s.edition_id = e.id) AS n_segments,
                   coalesce((SELECT max(char_end) FROM segments s WHERE s.edition_id = e.id), 0)
                       AS char_length,
                   EXISTS (SELECT 1 FROM page_breaks pb WHERE pb.edition_id = e.id) AS has_page_breaks,
                   (SELECT ea.reference_edition_id FROM edition_alignments ea
                    WHERE ea.edition_id = e.id) AS reference_edition_id,
                   e.epub_object_key IS NOT NULL AS epub_available
            FROM editions e JOIN works w ON w.id = e.work_id
            LEFT JOIN LATERAL ({sql.WORK_TITLE}) wt ON true
            WHERE e.id = %(edition_id)s AND {sql.visible("e")}
            """,
            p,
        )
    ).fetchone()
    if row is None:
        raise not_found("Édition")
    return row


# ---------------------------------------------------------- table des matières
@router.get("/toc", response_model=Toc, summary="Table des matières (arbre des sections)")
async def get_toc(
    edition_id: UUID,
    request: Request,
    response: Response,
    conn: Conn,
    principal: Reader,
    matter: Annotated[
        list[Literal["front", "body", "back"]] | None, Query(description="Parties gardées (défaut : toutes).")
    ] = None,
    include_notes: Annotated[bool, Query(description="Inclure chaque note comme une section.")] = False,
    rev: Rev = None,
):
    edition = await load_edition(conn, edition_id, principal)
    check_revision(edition, rev)
    if cache(request, response, text_etag(edition, f":toc:{','.join(matter or [])}:{include_notes}"), rev):
        return not_modified(response)
    rows = await (
        await conn.execute(
            """
            SELECT s.id, s.parent_id, s.kind::text AS kind, s.matter::text AS matter, s.label, s.title,
                   s.number, r.seq_start, r.seq_end
            FROM sections s JOIN section_ranges(%(e)s) r ON r.section_id = s.id
            WHERE s.edition_id = %(e)s AND (%(notes)s OR s.kind <> 'note')
              AND (%(matter)s::text[] IS NULL OR s.matter::text = ANY(%(matter)s))
            ORDER BY s.seq
            """,
            {"e": edition_id, "notes": include_notes, "matter": matter},
        )
    ).fetchall()
    nodes = {r["id"]: {**r, "children": []} for r in rows}
    roots = []
    for node in nodes.values():
        parent = nodes.get(node["parent_id"])
        (parent["children"] if parent else roots).append(node)
    return Toc(edition_id=edition_id, revision=edition["revision"], sections=roots)


# ------------------------------------------------------------------- segments
async def fetch_segments(conn: Conn, edition_id: UUID, where: str, params: dict) -> list[dict]:
    """Segments avec appels de note et sauts de page (offsets relatifs au segment)."""
    segments = await (
        await conn.execute(
            f"""
            SELECT s.id, s.seq, s.section_id, s.kind::text AS kind, s.speaker, s.char_start, s.char_end,
                   s.text, s.markup,
                   (SELECT coalesce(json_agg(json_build_object(
                        'offset', r.char_offset, 'label', r.label, 'note_id', r.note_section_id,
                        'origin', r.origin::text) ORDER BY r.char_offset), '[]')
                    FROM note_refs r WHERE r.segment_id = s.id) AS notes
            FROM segments s WHERE s.edition_id = %(e)s AND {where} ORDER BY s.seq
            """,
            {"e": edition_id, **params},
        )
    ).fetchall()
    if not segments:
        return segments
    pages = await (
        await conn.execute(
            "SELECT char_offset, label FROM page_breaks WHERE edition_id = %s "
            "AND char_offset >= %s AND char_offset < %s ORDER BY char_offset",
            (edition_id, segments[0]["char_start"], segments[-1]["char_end"] + 2),
        )
    ).fetchall()
    starts = [s["char_start"] for s in segments]
    for s in segments:
        s["pages"] = []
    for page in pages:
        # Un saut placé entre deux segments (dans le séparateur) ouvre le suivant.
        i = bisect.bisect_right(starts, page["char_offset"]) - 1
        s = segments[i]
        offset = page["char_offset"] - s["char_start"]
        if offset > len(s["text"]) and i + 1 < len(segments):
            s, offset = segments[i + 1], 0
        s["pages"].append({"offset": min(offset, len(s["text"])), "label": page["label"]})
    return segments


async def fetch_notes(conn: Conn, edition_id: UUID, note_ids: list[UUID]) -> dict[UUID, dict]:
    if not note_ids:
        return {}
    rows = await fetch_segments(conn, edition_id, "s.section_id = ANY(%(ids)s)", {"ids": note_ids})
    refs = await (
        await conn.execute(
            "SELECT DISTINCT ON (note_section_id) note_section_id, label, origin::text AS origin "
            "FROM note_refs WHERE note_section_id = ANY(%s)",
            (note_ids,),
        )
    ).fetchall()
    meta = {r["note_section_id"]: r for r in refs}
    notes: dict[UUID, dict] = {}
    for note_id in note_ids:
        notes[note_id] = {
            "note_id": note_id,
            "label": meta.get(note_id, {}).get("label"),
            "origin": meta.get(note_id, {}).get("origin"),
            "segments": [s for s in rows if s["section_id"] == note_id],
        }
    return notes


@router.get("/segments", response_model=SegmentRange, summary="Texte : plage de segments")
async def get_segments(
    edition_id: UUID,
    request: Request,
    response: Response,
    conn: Conn,
    principal: Reader,
    from_seq: Annotated[int, Query(ge=0)] = 0,
    to_seq: Annotated[int | None, Query(ge=0, description="Dernier segment voulu (inclus).")] = None,
    limit: Annotated[int, Query(ge=1, le=MAX_SEGMENTS)] = 100,
    include: Annotated[
        list[Literal["notes"]] | None, Query(description="notes : contenu des notes appelées.")
    ] = None,
    rev: Rev = None,
):
    edition = await load_text_edition(conn, edition_id, principal, rev)
    etag = text_etag(edition, f":seg:{from_seq}:{to_seq}:{limit}:{','.join(include or [])}")
    if cache(request, response, etag, rev):
        return not_modified(response)
    last = from_seq + limit - 1 if to_seq is None else min(to_seq, from_seq + limit - 1)
    segments = await fetch_segments(
        conn, edition_id, "s.seq BETWEEN %(a)s AND %(b)s", {"a": from_seq, "b": last}
    )
    total = (
        await (
            await conn.execute("SELECT count(*) AS n FROM segments WHERE edition_id = %s", (edition_id,))
        ).fetchone()
    )["n"]
    end = total - 1 if to_seq is None else min(to_seq, total - 1)
    next_from = last + 1 if last < end else None
    notes = None
    if include and "notes" in include:
        ids = list(dict.fromkeys(n["note_id"] for s in segments for n in s["notes"]))
        notes = await fetch_notes(conn, edition_id, [UUID(str(i)) for i in ids])
    return SegmentRange(
        edition_id=edition_id,
        revision=edition["revision"],
        total_segments=total,
        segments=segments,
        next_from_seq=next_from,
        notes=notes,
    )


@router.get("/notes/{note_id}", response_model=Note, summary="Contenu d'une note")
async def get_note(edition_id: UUID, note_id: UUID, conn: Conn, principal: Reader, rev: Rev = None) -> dict:
    await load_text_edition(conn, edition_id, principal, rev)
    notes = await fetch_notes(conn, edition_id, [note_id])
    if not notes[note_id]["segments"]:
        raise not_found("Note")
    return notes[note_id]


# ------------------------------------------------------------------- position
async def section_paths(conn: Conn, edition_id: UUID) -> dict:
    rows = await (
        await conn.execute("SELECT section_id, path FROM section_paths WHERE edition_id = %s", (edition_id,))
    ).fetchall()
    return {r["section_id"]: r["path"] for r in rows}


@router.get("/position", response_model=Position, summary="Où se trouve une position du texte")
async def get_position(
    edition_id: UUID,
    conn: Conn,
    principal: Reader,
    seq: Annotated[int, Query(ge=0)],
    offset: Annotated[int, Query(ge=0)] = 0,
    rev: Rev = None,
) -> dict:
    edition = await load_edition(conn, edition_id, principal)
    check_revision(edition, rev)
    row = await (
        await conn.execute(
            """
            SELECT s.section_id, s.char_start, s.char_end, p.path,
                   (SELECT max(char_end) FROM segments WHERE edition_id = s.edition_id) AS total,
                   (SELECT pb.label FROM page_breaks pb WHERE pb.edition_id = s.edition_id
                    AND pb.char_offset <= s.char_start + %(o)s ORDER BY pb.char_offset DESC LIMIT 1) AS page
            FROM segments s LEFT JOIN section_paths p ON p.section_id = s.section_id
            WHERE s.edition_id = %(e)s AND s.seq = %(seq)s
            """,
            {"e": edition_id, "seq": seq, "o": offset},
        )
    ).fetchone()
    if row is None:
        raise not_found("Segment")
    offset = min(offset, row["char_end"] - row["char_start"])
    return {
        "seq": seq,
        "offset": offset,
        "section_id": row["section_id"],
        "path": row["path"] or [],
        "page_label": row["page"],
        "progress": round((row["char_start"] + offset) / row["total"], 6) if row["total"] else 0.0,
    }


# ------------------------------------------------------- recherche dans le livre
@router.get("/find", response_model=FindResult, summary="Recherche dans le livre (mots exacts)")
async def find_in_edition(
    edition_id: UUID,
    conn: Conn,
    principal: Reader,
    q: Annotated[str, Query(min_length=2, max_length=500, description="Casse et accents ignorés.")],
    limit: Annotated[int, Query(ge=1, le=500)] = 100,
    rev: Rev = None,
) -> FindResult:
    edition = await load_text_edition(conn, edition_id, principal, rev)
    # Préfiltre SQL (même normalisation qu'à l'affichage), positions exactes en Python.
    pattern = "%" + q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
    rows = await (
        await conn.execute(
            "SELECT seq, section_id, text FROM segments WHERE edition_id = %s "
            "AND search_key(text) LIKE search_key(%s) ORDER BY seq",
            (edition_id, pattern),
        )
    ).fetchall()
    paths = await section_paths(conn, edition_id) if rows else {}
    hits, truncated = [], False
    for row in rows:
        for start, end in find_all(row["text"], q):
            if len(hits) == limit:
                truncated = True
                break
            text, at = snippet(row["text"], start, end)
            hits.append(
                {
                    "seq": row["seq"],
                    "offset": start,
                    "length": end - start,
                    "snippet": text,
                    "snippet_offset": at,
                    "section_path": paths.get(row["section_id"]) or [],
                }
            )
        if truncated:
            break
    return FindResult(edition_id=edition_id, revision=edition["revision"], hits=hits, truncated=truncated)


# ------------------------------------------------------------------------ EPUB
RANGE_RE = re.compile(r"^bytes=(\d*)-(\d*)$")


def parse_range(header: str | None, size: int) -> tuple[int, int] | None:
    """Plage unique « bytes=a-b », « bytes=a- » ou « bytes=-n » ; None = tout le
    fichier ; ValueError si la plage est invalide ou hors du fichier."""
    if not header:
        return None
    m = RANGE_RE.match(header.strip())
    if not m or (not m[1] and not m[2]):
        raise ValueError(header)
    if not m[1]:  # n derniers octets
        n = int(m[2])
        if n == 0:
            raise ValueError(header)
        return max(0, size - n), size - 1
    start = int(m[1])
    end = min(int(m[2]), size - 1) if m[2] else size - 1
    if start >= size or end < start:
        raise ValueError(header)
    return start, end


@router.head("/epub", include_in_schema=False)
@router.get(
    "/epub",
    summary="Fichier EPUB brut (GET ou HEAD)",
    response_class=StreamingResponse,
    responses={200: {"content": {s3.EPUB_CONTENT_TYPE: {}}}, 206: {"description": "Plage d'octets"}},
)
async def get_epub(
    edition_id: UUID,
    request: Request,
    conn: Conn,
    principal: Reader,
    inline: Annotated[bool, Query(description="Content-Disposition: inline.")] = False,
    rev: Rev = None,
):
    edition = await load_text_edition(conn, edition_id, principal, rev)
    mc = request.app.state.s3
    if mc is None or edition["epub_object_key"] is None:
        raise not_found("EPUB")
    bucket = request.app.state.settings.minio_books_bucket
    key = edition["epub_object_key"]

    name = (
        f"{(edition['slug'] or 'swann').replace('/', '-')}-{PurePosixPath(edition['source_file']).stem}.epub"
    )
    headers = {
        "ETag": f'"{edition["sha256"]}"',
        "Cache-Control": IMMUTABLE if rev is not None else REVALIDATE,
        "Accept-Ranges": "bytes",
        "Content-Disposition": f'{"inline" if inline else "attachment"}; filename="{name}"',
    }
    if headers["ETag"] in [t.strip() for t in request.headers.get("if-none-match", "").split(",")]:
        return Response(status_code=304, headers=headers)

    size = await anyio.to_thread.run_sync(s3.object_size, mc, bucket, key)
    if size is None:
        raise not_found("EPUB")
    range_header = request.headers.get("range")
    if range_header and request.headers.get("if-range", headers["ETag"]) != headers["ETag"]:
        range_header = None  # le client a une autre version : fichier complet
    try:
        byte_range = parse_range(range_header, size)
    except ValueError:
        problem = Problem(416, "range", f"Plage invalide pour un fichier de {size} octets.")
        problem.headers["Content-Range"] = f"bytes */{size}"
        raise problem from None

    start, end = byte_range or (0, size - 1)
    length = end - start + 1 if size else 0
    headers["Content-Length"] = str(length)
    status = 200
    if byte_range is not None:
        status = 206
        headers["Content-Range"] = f"bytes {start}-{end}/{size}"
    if request.method == "HEAD":
        return Response(status_code=status, headers=headers, media_type=s3.EPUB_CONTENT_TYPE)

    stream = await anyio.to_thread.run_sync(s3.open_object, mc, bucket, key, start, length)
    if stream is None:  # supprimé entre-temps
        raise not_found("EPUB")
    return StreamingResponse(
        stream.chunks, status_code=status, headers=headers, media_type=s3.EPUB_CONTENT_TYPE
    )


# ----------------------------------------------------------------------- ancres
@router.post(
    "/anchors:resolve",
    response_model=AnchorsResponse,
    summary="Recaler des positions enregistrées sur une ancienne révision",
)
async def resolve_anchors(
    edition_id: UUID, body: AnchorsRequest, conn: Conn, principal: Reader
) -> AnchorsResponse:
    edition = await load_text_edition(conn, edition_id, principal, None)
    current = edition["revision"]
    stale = [a for a in body.anchors if a.revision != current]
    segments: list[tuple[int, str]] = []
    if stale:
        rows = await (
            await conn.execute(
                "SELECT seq, text FROM segments WHERE edition_id = %s ORDER BY seq", (edition_id,)
            )
        ).fetchall()
        segments = [(r["seq"], r["text"]) for r in rows]

    results = []
    for a in body.anchors:
        if a.revision == current:
            results.append(AnchorResult(key=a.key, status="unchanged", seq=a.seq, offset=a.offset))
            continue
        found = (
            locate_quote(segments, a.quote.exact, a.quote.prefix, a.quote.suffix, a.seq) if a.quote else None
        )
        if found:
            results.append(AnchorResult(key=a.key, status="relocated", seq=found.seq, offset=found.offset))
        elif segments:
            seq = min(max(a.seq, segments[0][0]), segments[-1][0])
            results.append(AnchorResult(key=a.key, status="approximate", seq=seq, offset=0))
        else:
            results.append(AnchorResult(key=a.key, status="lost", seq=None, offset=None))
    return AnchorsResponse(revision=current, results=results)
