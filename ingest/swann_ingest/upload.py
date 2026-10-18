"""Traitement d'un EPUB déposé dans la console (tâche `ingest`,
docs/console.md §4).

1. lecture de l'EPUB (bucket `inbox`) ;
2. classement (`identify`) : sans décision de l'admin, la tâche s'arrête en
   `needs_review` avec le rapport ; elle reprend quand l'admin a validé
   (`params.resolution`, posée par l'API) ;
3. application : œuvre existante ou créée (personnes comprises, enrichies
   par Wikidata), édition enregistrée, EPUB rangé dans le bucket `books` ;
4. qualité, indexation dans l'index actif, alignement des nouvelles éditions.

Format d'une résolution (même format que la proposition du rapport) :
  {"action": "attach", "work_id": …, "replace_edition_id": null | …, "edition": {…}}
  {"action": "create_work", "work": {title, original_language, first_published_year,
     wikidata_id, titles: {langue: titre}, authors: [{person_id} | {name, wikidata_id,
     birth_year, death_year}]}, "edition": {…}}
  {"action": "reject"}
  edition = {title, language, is_original, translators, publisher, year, access}
"""

from __future__ import annotations

import tempfile
import uuid
from pathlib import Path
from typing import TYPE_CHECKING

import psycopg

from swann_ingest import quality
from swann_ingest.catalog import LANGUAGE_RE, EditionSpec
from swann_ingest.identify import identify, new_slug
from swann_ingest.jobs import JobContext, NeedsReview
from swann_ingest.parse import ParsedEdition, parse_epub
from swann_ingest.pipeline import steps
from swann_ingest.store.pg import Store, emit
from swann_ingest.text.language import detect_majority
from swann_ingest.wikidata import Wikidata, WikidataError, year_of

if TYPE_CHECKING:
    from swann_ingest.worker import Worker


class UploadError(Exception):
    pass


def handle(w: Worker, ctx: JobContext) -> dict:
    params = ctx.params
    settings = w.settings
    if w.mc is None:
        raise UploadError("stockage S3 désactivé : impossible de lire le dépôt")
    key = params["object_key"]
    with tempfile.TemporaryDirectory(prefix="swann-upload-") as tmp:
        path = Path(tmp) / "upload.epub"
        ctx.progress("read", message=f"lecture de {params.get('filename', key)}", force=True)
        w.mc.fget_object(settings.minio_inbox_bucket, key, str(path))
        parsed = parse_epub(path)

        resolution = params.get("resolution")
        identification = (ctx.result or {}).get("identification")
        if resolution is None:
            ctx.progress("identify", message="classement de l'œuvre", force=True)
            wd = Wikidata(w.conn, settings.wikidata_user_agent, settings.wikidata_enabled)
            report = identify(
                w.conn,
                parsed,
                wikidata=wd,
                encoder=w.models.align(),
                hints=params.get("hints") or {},
                threshold=settings.identify_threshold,
            )
            identification = report.to_json()
            ctx.result = {"identification": identification}
            if not (settings.identify_auto and report.confident and report.proposal):
                raise NeedsReview("classement à valider", ctx.result)
            resolution = report.proposal
        if resolution.get("action") == "reject":
            w.mc.remove_object(settings.minio_inbox_bucket, key)
            return {"identification": identification, "rejected": True}

        ctx.check_cancel()
        ctx.progress("save", message="enregistrement de l'édition", force=True)
        store = Store(w.conn)
        ingestion = store.start_ingestion(f"inbox/{key}", stage="extract", job_id=ctx.id)
        try:
            edition_id, work_id, replaced = apply(w, ctx, parsed, path, resolution, identification)
        except Exception as e:
            store.finish_ingestion(ingestion, error=f"{type(e).__name__}: {e}")
            raise
        store.finish_ingestion(ingestion, edition_id=edition_id, n_segments=len(parsed.text.segments))
        ctx.link(work_id=work_id, edition_id=edition_id)
    w.mc.remove_object(settings.minio_inbox_bucket, key)

    ctx.check_cancel()
    result: dict = {
        "identification": identification,
        "resolution": resolution,
        "edition_id": str(edition_id),
        "work_id": str(work_id),
        "replaced": replaced,
    }
    result["index"] = steps.index_in_active(w.conn, w.qc, w.models, edition_id, ctx)
    ctx.check_cancel()
    result["align"] = steps.align_work_step(w.conn, w.models, work_id, ctx)
    result["quality"] = quality.compute(w.conn, edition_id)
    return result


def apply(
    w: Worker,
    ctx: JobContext,
    parsed: ParsedEdition,
    path: Path,
    resolution: dict,
    identification: dict | None,
) -> tuple[uuid.UUID, uuid.UUID, bool]:
    """Crée ou complète l'œuvre, enregistre l'édition ; renvoie
    (édition, œuvre, texte remplacé ?)."""
    from swann_core import s3

    conn = w.conn
    actor = ctx.params.get("actor")
    edition = resolution["edition"]
    language = edition.get("language") or ""
    if not LANGUAGE_RE.match(language):
        raise UploadError(f"code de langue invalide : {language!r}")

    replace_id = resolution.get("replace_edition_id")
    clash = conn.execute(
        "SELECT id, source_file FROM editions WHERE sha256 = %s", (parsed.sha256,)
    ).fetchone()
    if clash and str(clash["id"]) != str(replace_id):
        raise UploadError(f"fichier identique déjà en base ({clash['source_file']})")

    wd = Wikidata(conn, w.settings.wikidata_user_agent, w.settings.wikidata_enabled)
    with conn.transaction():
        if resolution["action"] == "create_work":
            work_id = create_work(conn, wd, resolution["work"], actor)
        elif resolution["action"] == "attach":
            work_id = uuid.UUID(str(resolution["work_id"]))
            if not conn.execute("SELECT 1 FROM works WHERE id = %s", (work_id,)).fetchone():
                raise UploadError("œuvre introuvable")
        else:
            raise UploadError(f"action inconnue : {resolution.get('action')!r}")
        for person_id, qid in enrich_from_identification(conn, work_id, identification):
            _add_wikidata_names(conn, wd, person_id, qid)

    source_file = f"uploads/{parsed.sha256}.epub"
    if replace_id:
        row = conn.execute(
            "SELECT source_file, work_id FROM editions WHERE id = %s", (replace_id,)
        ).fetchone()
        if row is None:
            raise UploadError("édition à remplacer introuvable")
        source_file = row["source_file"]
        work_id = row["work_id"]

    key = s3.put_epub(w.mc, w.settings.minio_books_bucket, path, parsed.sha256) if w.mc else None
    spec = EditionSpec(
        path=path,
        source_file=source_file,
        language=language,
        original=bool(edition.get("is_original")),
        title=edition.get("title"),
        translators=[t for t in edition.get("translators") or [] if t.strip()],
        publisher=edition.get("publisher"),
        year=edition.get("year"),
        access=edition.get("access") or "restricted",
    )
    store = Store(conn, overwrite_metadata=True)
    title = edition.get("title") or (parsed.metadata.titles[0] if parsed.metadata.titles else "Sans titre")
    saved = store.save_edition(work_id, spec, parsed, title, epub_key=key)
    conn.execute(
        "UPDATE editions SET identification = %s WHERE id = %s",
        (_jsonb({"report": identification, "resolution": resolution, "by": actor}), saved.id),
    )
    if w.mc and saved.previous_epub_key and saved.previous_epub_key != key:
        s3.remove(w.mc, w.settings.minio_books_bucket, saved.previous_epub_key)
    quality.record_parse(conn, saved.id, parsed, detect_majority(parsed.body_paragraphs()))
    quality.compute(conn, saved.id)
    return saved.id, work_id, bool(replace_id)


def _jsonb(value):
    from swann_ingest.jobs import to_jsonb

    return to_jsonb(value)


def create_person(conn: psycopg.Connection, wd: Wikidata, author: dict) -> uuid.UUID:
    """Personne d'après le rapport : réutilise la personne au même QID, sinon
    la crée (noms dans toutes les langues repris de Wikidata)."""
    qid = author.get("wikidata_id")
    if qid:
        row = conn.execute("SELECT id FROM persons WHERE wikidata_id = %s", (qid,)).fetchone()
        if row:
            return row["id"]
    name = (author.get("name") or "").strip()
    if not name:
        raise UploadError("auteur sans nom")
    person_id = conn.execute(
        "INSERT INTO persons (display_name, wikidata_id, birth_year, death_year) VALUES (%s, %s, %s, %s) "
        "RETURNING id",
        (name, qid, author.get("birth_year"), author.get("death_year")),
    ).fetchone()["id"]
    if qid:
        _add_wikidata_names(conn, wd, person_id, qid)
    return person_id


def _add_wikidata_names(conn: psycopg.Connection, wd: Wikidata, person_id, qid: str) -> None:
    try:
        entity = wd.entities([qid]).get(qid)
    except WikidataError:
        return
    if entity is None:
        return
    for lang, label in entity.labels.items():
        if LANGUAGE_RE.match(lang):
            conn.execute(
                "INSERT INTO person_names (person_id, language, name) VALUES (%s, %s, %s) "
                "ON CONFLICT DO NOTHING",
                (person_id, lang, label),
            )
    birth, death = year_of(entity.first("P569")), year_of(entity.first("P570"))
    conn.execute(
        "UPDATE persons SET birth_year = coalesce(birth_year, %s), death_year = coalesce(death_year, %s) "
        "WHERE id = %s",
        (birth, death, person_id),
    )


def create_work(conn: psycopg.Connection, wd: Wikidata, work: dict, actor: str | None) -> uuid.UUID:
    title = (work.get("title") or "").strip()
    if not title:
        raise UploadError("titre de l'œuvre requis")
    authors = work.get("authors") or []
    person_ids = []
    for a in authors:
        person_ids.append(
            uuid.UUID(str(a["person_id"])) if a.get("person_id") else create_person(conn, wd, a)
        )
    first_author = None
    if person_ids:
        first_author = conn.execute(
            "SELECT display_name FROM persons WHERE id = %s", (person_ids[0],)
        ).fetchone()["display_name"]
    qid = work.get("wikidata_id") or None
    if qid and (row := conn.execute("SELECT id FROM works WHERE wikidata_id = %s", (qid,)).fetchone()):
        raise UploadError(
            f"l'œuvre {qid} existe déjà en base ({row['id']}) : la rattacher plutôt que la créer"
        )
    original_language = work.get("original_language") or None
    work_id = conn.execute(
        """
        INSERT INTO works (slug, title, original_language, first_published_year, wikidata_id)
        VALUES (%s, %s, %s, %s, %s) RETURNING id
        """,
        (
            new_slug(conn, first_author, title),
            title,
            original_language,
            work.get("first_published_year"),
            qid,
        ),
    ).fetchone()["id"]
    titles = dict(work.get("titles") or {})
    if original_language:
        titles.setdefault(original_language, title)
    for lang, t in titles.items():
        if t and LANGUAGE_RE.match(lang):
            conn.execute(
                "INSERT INTO work_titles (work_id, language, title) VALUES (%s, %s, %s) "
                "ON CONFLICT DO NOTHING",
                (work_id, lang, t),
            )
    for pos, person_id in enumerate(person_ids):
        conn.execute(
            "INSERT INTO work_authors (work_id, person_id, position) VALUES (%s, %s, %s) "
            "ON CONFLICT DO NOTHING",
            (work_id, person_id, pos),
        )
    emit(conn, "work.created", work_id, actor=actor)
    return work_id


def enrich_from_identification(
    conn: psycopg.Connection, work_id, identification: dict | None
) -> list[tuple[str, str]]:
    """Après validation : recopie les QID Wikidata trouvés sur l'œuvre et les
    auteurs reconnus qui n'en avaient pas (meilleur classement des dépôts suivants)."""
    updated: list[tuple[str, str]] = []
    if not identification:
        return updated
    wd_work = identification.get("wikidata") or {}
    if wd_work.get("id"):
        taken = conn.execute("SELECT 1 FROM works WHERE wikidata_id = %s", (wd_work["id"],)).fetchone()
        if not taken:
            conn.execute(
                "UPDATE works SET wikidata_id = %s WHERE id = %s AND wikidata_id IS NULL",
                (wd_work["id"], work_id),
            )
    for a in identification.get("authors") or []:
        if a.get("person_id") and a.get("wikidata_id"):
            taken = conn.execute(
                "SELECT 1 FROM persons WHERE wikidata_id = %s", (a["wikidata_id"],)
            ).fetchone()
            if not taken:
                row = conn.execute(
                    "UPDATE persons SET wikidata_id = %s WHERE id = %s AND wikidata_id IS NULL RETURNING id",
                    (a["wikidata_id"], a["person_id"]),
                ).fetchone()
                if row:
                    updated.append((a["person_id"], a["wikidata_id"]))
    return updated
