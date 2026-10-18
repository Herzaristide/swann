"""Qualité des éditions (table `edition_quality`, docs/console.md §7).

Deux sources :
- au parsing (`record_parse`, appelé par extract) : ce qui n'existe plus
  ensuite (méthode de structure, avertissements, langue détectée, OPF) ;
- depuis la base (`compute`) : mesures recalculables à tout moment (après une
  modification de structure, un changement de seuil…), d'où les signaux et le
  score.

Un signal = un code stable (affiché par la console) + un détail lisible. Les
signaux acceptés par un admin (`quality_acks`) restent listés mais ne
comptent plus dans le score.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from decimal import Decimal

import psycopg
from psycopg.types.json import Jsonb

from swann_ingest.parse import ParsedEdition

# Seuils (docs/console.md §7)
THIN_BODY_CHARS = 20_000
OUT_OF_BODY_MAX = 0.30
GIANT_SEGMENT_CHARS = 5_000
TINY_MEDIAN_CHARS = 40
TINY_MIN_SEGMENTS = 50
NO_CHAPTERS_MIN_SEGMENTS = 200
ALIGNED_RATIO_MIN = 0.50
UNREFERENCED_NOTES_MIN = 3

# Poids dans le score (100 = aucun signal)
WEIGHTS = {
    "empty_body": 60,
    "language_mismatch": 40,
    "not_indexed": 20,
    "giant_segments": 15,
    "tiny_segments": 15,
    "alignment_doubtful": 15,
    "no_toc": 10,
    "thin_body": 10,
    "out_of_body": 10,
    "no_chapters": 10,
    "not_aligned": 10,
    "empty_sections": 5,
    "unreferenced_notes": 5,
    "parse_warnings": 5,
    "no_epub": 5,
}

TOC_WARNINGS = ("pas de table des matières", "table des matières inutilisable", "aucun segment classé")


@dataclass
class Signal:
    code: str
    detail: str


def record_parse(
    conn: psycopg.Connection, edition_id: uuid.UUID, parsed: ParsedEdition, detected_language: str | None
) -> None:
    md = parsed.metadata
    opf = {
        "titles": md.titles,
        "languages": md.languages,
        "creators": [{"name": n, "role": r} for n, r in md.creators],
        "publisher": md.publisher,
        "date": md.date,
    }
    conn.execute(
        """
        INSERT INTO edition_quality (edition_id, structure_method, parse_warnings, detected_language,
                                     opf_metadata, parsed_at)
        VALUES (%s, %s, %s, %s, %s, now())
        ON CONFLICT (edition_id) DO UPDATE SET
            structure_method = EXCLUDED.structure_method, parse_warnings = EXCLUDED.parse_warnings,
            detected_language = EXCLUDED.detected_language, opf_metadata = EXCLUDED.opf_metadata,
            parsed_at = now()
        """,
        (edition_id, parsed.structure_method, Jsonb(parsed.warnings), detected_language, Jsonb(opf)),
    )


METRICS_SQL = """
WITH e AS (
    SELECT e.id, e.work_id, e.language, e.epub_object_key FROM editions e WHERE e.id = %(id)s
),
seg AS (
    SELECT s.kind::text AS kind, length(s.text) AS len, coalesce(sec.matter::text, 'body') AS matter
    FROM segments s LEFT JOIN sections sec ON sec.id = s.section_id
    WHERE s.edition_id = %(id)s
),
body AS (SELECT len FROM seg WHERE matter = 'body' AND kind NOT IN ('note', 'heading'))
SELECT
    (SELECT count(*) FROM sections WHERE edition_id = %(id)s) AS n_sections,
    (SELECT count(*) FROM sections WHERE edition_id = %(id)s AND kind = 'chapter') AS n_chapters,
    (SELECT count(*) FROM sections sec WHERE sec.edition_id = %(id)s AND sec.matter = 'body'
        AND sec.kind <> 'note'
        AND EXISTS (SELECT 1 FROM segments s WHERE s.section_id = sec.id)) AS n_text_sections,
    (SELECT count(*) FROM sections WHERE edition_id = %(id)s AND kind = 'note') AS n_notes,
    (SELECT count(*) FROM note_refs r JOIN segments s ON s.id = r.segment_id
      WHERE s.edition_id = %(id)s) AS n_note_refs,
    (SELECT count(*) FROM sections n WHERE n.edition_id = %(id)s AND n.kind = 'note'
        AND NOT EXISTS (SELECT 1 FROM note_refs r WHERE r.note_section_id = n.id)) AS unreferenced_notes,
    (SELECT count(*) FROM section_ranges(%(id)s) r JOIN sections sec ON sec.id = r.section_id
      WHERE sec.matter = 'body' AND sec.kind <> 'note' AND r.n_segments = 0) AS empty_sections,
    (SELECT count(*) FROM page_breaks WHERE edition_id = %(id)s) AS n_page_breaks,
    (SELECT count(*) FROM seg) AS n_segments,
    (SELECT count(*) FROM body) AS n_body_segments,
    (SELECT coalesce(sum(len), 0) FROM seg) AS total_chars,
    (SELECT coalesce(sum(len), 0) FROM seg WHERE matter = 'body') AS body_chars,
    (SELECT coalesce(max(len), 0) FROM body) AS max_segment_chars,
    (SELECT percentile_cont(0.5) WITHIN GROUP (ORDER BY len) FROM body) AS median_segment_chars,
    (SELECT round(avg(len)) FROM body) AS mean_segment_chars,
    (SELECT e.epub_object_key IS NOT NULL FROM e) AS has_epub,
    (SELECT count(*) FROM editions o WHERE o.work_id = (SELECT work_id FROM e)
        AND o.deleted_at IS NULL
        AND EXISTS (SELECT 1 FROM segments s WHERE s.edition_id = o.id)) AS work_editions,
    (SELECT v.collection FROM vector_indexes v WHERE v.status = 'active' LIMIT 1) AS active_index,
    (SELECT ei.n_points FROM edition_indexings ei JOIN vector_indexes v ON v.id = ei.index_id
      WHERE ei.edition_id = %(id)s AND v.status = 'active') AS active_index_points,
    (SELECT json_build_object('status', ea.status::text, 'aligned_ratio', ea.aligned_ratio,
                              'mean_score', ea.mean_score, 'low_score_ratio', ea.low_score_ratio,
                              'is_reference', ea.reference_edition_id IS NULL)
       FROM edition_alignments ea WHERE ea.edition_id = %(id)s) AS alignment,
    q.structure_method, q.parse_warnings, q.detected_language,
    (SELECT language FROM e) AS language,
    coalesce((SELECT array_agg(signal) FROM quality_acks WHERE edition_id = %(id)s), '{}') AS acked
FROM (SELECT 1) one LEFT JOIN edition_quality q ON q.edition_id = %(id)s
"""


def _signals(m: dict) -> list[Signal]:
    out: list[Signal] = []
    if m["n_segments"] == 0:
        return [Signal("empty_body", "aucun texte extrait")]
    if m["structure_method"] == "headings":
        out.append(
            Signal("no_toc", "table des matières absente ou inutilisable : sections déduites des titres")
        )
    detected, language = m["detected_language"], m["language"]
    if detected and detected != language.split("-")[0]:
        out.append(Signal("language_mismatch", f"langue détectée « {detected} », fiche « {language} »"))
    if m["n_body_segments"] == 0:
        out.append(Signal("empty_body", "aucun paragraphe dans le corps du texte"))
    elif m["body_chars"] < THIN_BODY_CHARS:
        out.append(
            Signal("thin_body", f"corps très court ({m['body_chars']:,} caractères)".replace(",", " "))
        )
    if m["total_chars"]:
        ratio = 1 - m["body_chars"] / m["total_chars"]
        if ratio > OUT_OF_BODY_MAX:
            out.append(
                Signal("out_of_body", f"{ratio:.0%} du texte hors du corps (préfaces, annexes, notes)")
            )
    if m["max_segment_chars"] > GIANT_SEGMENT_CHARS:
        out.append(
            Signal(
                "giant_segments",
                f"segment de {m['max_segment_chars']:,} caractères (paragraphes collés ?)".replace(",", " "),
            )
        )
    median = m["median_segment_chars"]
    if median is not None and m["n_body_segments"] >= TINY_MIN_SEGMENTS and median < TINY_MEDIAN_CHARS:
        out.append(Signal("tiny_segments", f"segments très courts (médiane {median:.0f} caractères)"))
    if m["n_text_sections"] <= 1 and m["n_body_segments"] > NO_CHAPTERS_MIN_SEGMENTS:
        out.append(
            Signal(
                "no_chapters",
                f"structure plate : {m['n_text_sections']} section pour {m['n_body_segments']} paragraphes",
            )
        )
    if m["empty_sections"]:
        out.append(Signal("empty_sections", f"{m['empty_sections']} section(s) du corps sans texte"))
    if m["unreferenced_notes"] >= UNREFERENCED_NOTES_MIN:
        out.append(
            Signal("unreferenced_notes", f"{m['unreferenced_notes']} note(s) jamais appelée(s) dans le texte")
        )
    warnings = [w for w in (m["parse_warnings"] or []) if not w.startswith(TOC_WARNINGS)]
    if warnings:
        out.append(Signal("parse_warnings", f"{len(warnings)} avertissement(s) de lecture : {warnings[0]}"))
    if m["active_index"] and m["active_index_points"] is None:
        out.append(Signal("not_indexed", f"absente de l'index actif {m['active_index']}"))
    alignment = m["alignment"]
    if alignment is None:
        if m["work_editions"] >= 2:
            out.append(Signal("not_aligned", "œuvre en plusieurs éditions, alignement pas encore fait"))
    elif not alignment["is_reference"]:
        ratio = alignment["aligned_ratio"] or 0
        if alignment["status"] in ("doubtful", "rejected") or ratio < ALIGNED_RATIO_MIN:
            out.append(
                Signal(
                    "alignment_doubtful",
                    f"alignement {alignment['status']}, {ratio:.0%} des segments alignés",
                )
            )
    if not m["has_epub"]:
        out.append(Signal("no_epub", "EPUB absent du stockage"))
    return out


def compute(conn: psycopg.Connection, edition_id: uuid.UUID) -> dict:
    """Recalcule mesures, signaux et score d'une édition ; renvoie la ligne."""
    m = conn.execute(METRICS_SQL, {"id": edition_id}).fetchone()
    signals = _signals(m)
    acked = set(m["acked"])
    score = max(0, 100 - sum(WEIGHTS[s.code] for s in signals if s.code not in acked))
    metrics = {
        k: (float(v) if isinstance(v, Decimal | float) else v)
        for k, v in m.items()
        if k not in ("structure_method", "parse_warnings", "detected_language", "language", "acked")
    }
    metrics["signal_details"] = {s.code: s.detail for s in signals}
    conn.execute(
        """
        INSERT INTO edition_quality (edition_id, metrics, signals, score, computed_at)
        VALUES (%s, %s, %s, %s, now())
        ON CONFLICT (edition_id) DO UPDATE SET
            metrics = EXCLUDED.metrics, signals = EXCLUDED.signals, score = EXCLUDED.score,
            computed_at = now()
        """,
        (edition_id, Jsonb(metrics), [s.code for s in signals], score),
    )
    return {"edition_id": edition_id, "signals": [s.code for s in signals], "score": score}


def compute_all(conn: psycopg.Connection, on_progress=None) -> int:
    ids = [r["id"] for r in conn.execute("SELECT id FROM editions ORDER BY source_file").fetchall()]
    for i, edition_id in enumerate(ids):
        compute(conn, edition_id)
        if on_progress:
            on_progress(i + 1, len(ids))
    return len(ids)
