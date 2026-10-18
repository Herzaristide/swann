"""Étape 1 : EPUB -> Postgres (œuvres, éditions, sections, segments, notes, pages).

Lecture des EPUB en parallèle (processus), écriture en base séquentielle dans
le processus principal (pas de concurrence sur les personnes / œuvres).
"""

from __future__ import annotations

import json
from collections.abc import Callable, Iterator
from concurrent.futures import ProcessPoolExecutor, as_completed
from dataclasses import dataclass, field
from pathlib import Path

from swann_ingest.catalog import EditionSpec, WorkSpec
from swann_ingest.epub.container import sha256_file
from swann_ingest.parse import ParsedEdition, parse_epub
from swann_ingest.text.language import LANGUAGE_CHECK_MIN_CHARS, detect_majority, same_language


@dataclass
class EditionResult:
    work: WorkSpec
    spec: EditionSpec
    parsed: ParsedEdition | None
    error: str | None
    detected_language: str | None = None


def _parse(path: Path) -> ParsedEdition:
    return parse_epub(path)


def parse_all(
    items: list[tuple[WorkSpec, EditionSpec]], workers: int, check_language: bool = True
) -> Iterator[EditionResult]:
    """Lit les EPUB en parallèle et vérifie leur langue ; renvoie les résultats
    au fil de l'eau."""
    with ProcessPoolExecutor(max_workers=max(1, workers)) as pool:
        futures = {pool.submit(_parse, spec.path): (work, spec) for work, spec in items}
        for future in as_completed(futures):
            work, spec = futures[future]
            try:
                parsed = future.result()
            except Exception as e:  # noqa: BLE001 - toute erreur de lecture est rapportée
                yield EditionResult(work, spec, None, f"{type(e).__name__}: {e}")
                continue
            detected = detect_majority(parsed.body_paragraphs())
            error = None
            reliable = parsed.stats()["body_chars"] >= LANGUAGE_CHECK_MIN_CHARS
            if not same_language(spec.language, detected) and not reliable:
                parsed.warnings.append(f"langue détectée {detected!r} sur un texte court : ignorée")
            elif check_language and not same_language(spec.language, detected):
                error = (
                    f"langue détectée {detected!r} différente de la fiche ({spec.language!r}) ; "
                    "corriger work.toml ou relancer avec --no-language-check"
                )
            yield EditionResult(work, spec, parsed, error, detected)


def dump_edition(result: EditionResult, out_dir: Path) -> Path:
    """Écrit une version lisible de l'extraction pour relecture humaine :
    <slug>/<fichier>.txt (texte structuré) et .json (statistiques, sections)."""
    parsed = result.parsed
    assert parsed is not None
    base = out_dir / result.spec.source_file
    base.parent.mkdir(parents=True, exist_ok=True)

    sections = {s.id: s for s in parsed.text.sections}
    depth: dict = {}
    for s in parsed.text.sections:
        depth[s.id] = 0 if s.parent_id is None else depth[s.parent_id] + 1
    notes_by_segment: dict = {}
    for r in parsed.text.note_refs:
        notes_by_segment.setdefault(r.segment_id, []).append(r)

    lines: list[str] = []
    current = None
    for seg in parsed.text.segments:
        if seg.section_id != current:
            current = seg.section_id
            s = sections[current]
            head = " — ".join(x for x in (s.label, s.title) if x) or "(sans titre)"
            lines.append(f"\n{'#' * (depth[s.id] + 1)} [{s.matter}/{s.kind}] {head}\n")
        text = seg.text
        for r in sorted(notes_by_segment.get(seg.id, []), key=lambda r: -r.char_offset):
            text = f"{text[: r.char_offset]}[^{r.label}]{text[r.char_offset :]}"
        prefix = {"heading": "> ", "verse": "| ", "quote": "» ", "epigraph": "~ ", "note": "^ "}
        lines.append(prefix.get(seg.kind, "") + text.replace("\n", "\n" + prefix.get(seg.kind, "")))
    base.with_suffix(".txt").write_text("\n\n".join(lines), "utf-8")

    summary = {
        "file": result.spec.source_file,
        "language": result.spec.language,
        "detected_language": result.detected_language,
        "structure_method": parsed.structure_method,
        "stats": parsed.stats(),
        "warnings": parsed.warnings,
        "error": result.error,
        "sections": [
            {
                "depth": depth[s.id],
                "kind": s.kind,
                "matter": s.matter,
                "number": s.number,
                "label": s.label,
                "title": s.title,
            }
            for s in parsed.text.sections
            if s.kind != "note"
        ],
    }
    base.with_suffix(".json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), "utf-8")
    return base.with_suffix(".txt")


def needs_extract(store, spec: EditionSpec, force: bool) -> bool:
    if force:
        return True
    state = store.edition_state(spec.source_file)
    return not (state and state.has_text and state.sha256 == sha256_file(spec.path))


# ------------------------------------------------------------ extraction en lot
@dataclass
class ExtractReport:
    ok: int = 0
    failed: int = 0
    unchanged: int = 0
    edition_ids: list = field(default_factory=list)  # éditions dont le texte a été (re)écrit
    work_ids: set = field(default_factory=set)  # œuvres concernées (à réaligner)


def extract_catalog(
    conn,
    settings,
    catalog,
    *,
    workers: int,
    force: bool = False,
    language_check: bool = True,
    overwrite_metadata: bool = False,
    job_id=None,
    log: Callable[[str, str], None] = lambda level, msg: None,
    progress: Callable[[int, int, str], None] = lambda done, total, current: None,
    check_cancel: Callable[[], None] = lambda: None,
) -> ExtractReport:
    """books/ (catalogue déjà lu) -> Postgres + MinIO, puis qualité des
    éditions écrites. `log(level, message)` avec level ∈ {info, warning, error}."""
    from swann_core import qdrant as qstore
    from swann_core import s3
    from swann_ingest import quality
    from swann_ingest.pipeline.index import sync_edition_payload
    from swann_ingest.store.pg import Store

    store = Store(conn, overwrite_metadata=overwrite_metadata, persons=catalog.persons)
    mc = s3.client(settings.minio_endpoint, settings.minio_root_user, settings.minio_root_password)
    bucket = settings.minio_books_bucket
    if mc is None:
        log("warning", "MINIO_ENDPOINT vide : les EPUB ne sont pas envoyés dans MinIO.")
    else:
        s3.ensure_bucket(mc, bucket)

    items = [(w, e) for w in catalog.works for e in w.editions]
    work_ids = {w.slug: store.upsert_work(w) for w in catalog.works}
    todo = [(w, e) for w, e in items if needs_extract(store, e, force)]
    todo_files = {e.source_file for _, e in todo}
    unchanged = [e for _, e in items if e.source_file not in todo_files]
    report = ExtractReport(unchanged=len(unchanged))
    if unchanged:
        log("info", f"{len(unchanged)} édition(s) inchangée(s), texte non relu (--force pour le refaire)")
    for spec in unchanged:
        state = store.edition_state(spec.source_file)
        assert state is not None
        key = None
        if mc and state.epub_object_key is None:
            key = s3.put_epub(mc, bucket, spec.path, state.sha256)
        changed = store.sync_edition(spec, key)
        if changed:
            log("info", f"Fiche mise à jour : {spec.source_file} ({', '.join(changed)})")
        if "access" in changed:
            # Les droits sont aussi dans le payload Qdrant (filtre de la recherche).
            sync_edition_payload(conn, qstore.client(settings.qdrant_url, settings.qdrant_api_key), state.id)

    progress(0, len(todo), "")
    for done, r in enumerate(parse_all(todo, workers, language_check), 1):
        check_cancel()
        ingestion = store.start_ingestion(r.spec.source_file, stage="extract", job_id=job_id)
        error = r.error
        edition_id = None
        n_segments = 0
        if error is None:
            try:
                key = s3.put_epub(mc, bucket, r.spec.path, r.parsed.sha256) if mc else None
                saved = store.save_edition(
                    work_ids[r.work.slug], r.spec, r.parsed, r.work.title, epub_key=key
                )
                edition_id = saved.id
                n_segments = len(r.parsed.text.segments)
                if mc and saved.previous_epub_key and saved.previous_epub_key != key:
                    s3.remove(mc, bucket, saved.previous_epub_key)
                quality.record_parse(conn, edition_id, r.parsed, r.detected_language)
                quality.compute(conn, edition_id)
            except Exception as e:  # rapportée dans `ingestions` et au journal
                error = f"{type(e).__name__}: {e}"
        store.finish_ingestion(ingestion, edition_id=edition_id, error=error, n_segments=n_segments)
        if error:
            report.failed += 1
            log("error", f"ÉCHEC {r.spec.source_file} : {error}")
        else:
            report.ok += 1
            report.edition_ids.append(edition_id)
            report.work_ids.add(work_ids[r.work.slug])
        progress(done, len(todo), r.spec.source_file)
    return report
