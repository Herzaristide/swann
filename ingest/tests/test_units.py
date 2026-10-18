"""Tests unitaires : titres, découpage, chunks, BM25, alignement, catalogue."""

from __future__ import annotations

import numpy as np
import pytest

from swann_core.embed import sparse
from swann_ingest.align.dp import align
from swann_ingest.catalog import scan
from swann_ingest.chunking.v1 import MAX_TOKENS, MIN_TOKENS, chunk_spans, plan_chunks
from swann_ingest.epub.blocks import Span
from swann_ingest.text.headings import parse_heading, roman_to_int
from swann_ingest.text.segment import render_markup, split_points


@pytest.mark.parametrize(
    ("text", "kind", "number", "label", "title"),
    [
        ("Chapitre III. La taverne", "chapter", 3, "Chapitre III", "La taverne"),
        ("PREMIÈRE PARTIE", "part", 1, "PREMIÈRE PARTIE", None),
        ("LIVRE DEUXIÈME", "book", 2, "LIVRE DEUXIÈME", None),
        ("Livre premier", "book", 1, "Livre premier", None),
        ("CHAPTER THE FIRST", "chapter", 1, "CHAPTER THE FIRST", None),
        ("Chapter 4: Wolves", "chapter", 4, "Chapter 4", "Wolves"),
        ("Глава V", "chapter", 5, "Глава V", None),
        ("ЧАСТЬ ПЕРВАЯ", "part", 1, "ЧАСТЬ ПЕРВАЯ", None),
        ("Erstes Kapitel", "chapter", 1, "Erstes Kapitel", None),
        ("XII. Le retour", None, 12, "XII", "Le retour"),
        ("Préface", "preface", None, "Préface", None),
        ("I Am Legend", None, None, None, "I Am Legend"),
    ],
)
def test_parse_heading(text, kind, number, label, title):
    h = parse_heading(text)
    assert (h.kind, h.number, h.label, h.title) == (kind, number, label, title)


def test_roman():
    assert [roman_to_int(x) for x in ("I", "iv", "XLII", "MCMXII", "IIII", "")] == [
        1,
        4,
        42,
        1912,
        None,
        None,
    ]


def test_split_points_keeps_sentences():
    text = " ".join(f"Phrase {i} « dite » ici !" for i in range(200))
    pieces = split_points(text, 300)
    assert all(b - a <= 300 for a, b in pieces)
    assert pieces[0][0] == 0 and pieces[-1][1] == len(text)
    assert all(pieces[i][1] == pieces[i + 1][0] for i in range(len(pieces) - 1))


def test_render_markup_nested():
    text = "un deux trois\nquatre"
    spans = [Span(3, 13, "em"), Span(8, 13, "strong")]
    assert render_markup(text, spans) == "un <em>deux <strong>trois</strong></em><br/>quatre"
    assert render_markup("a < b", []) is None


def test_chunk_spans_properties():
    tokens = [50, 120, 30, 200, 80, 90, 40, 300, 20, 10]
    spans = chunk_spans(tokens)
    covered = {i for a, b in spans for i in range(a, b + 1)}
    assert covered == set(range(len(tokens)))
    assert all(spans[i][0] <= spans[i + 1][0] for i in range(len(spans) - 1))
    for a, b in spans:
        total = sum(tokens[a : b + 1])
        assert total <= MAX_TOKENS or a == b


def test_chunk_last_small_merged():
    spans = chunk_spans([380, 30])
    assert spans == [(0, 1)]
    assert sum([380, 30]) >= MIN_TOKENS


def test_plan_chunks_never_crosses_sections_nor_notes():
    segs = [
        {"seq": 0, "section_id": "a", "kind": "heading"},
        {"seq": 1, "section_id": "a", "kind": "paragraph"},
        {"seq": 2, "section_id": "b", "kind": "paragraph"},
        {"seq": 3, "section_id": "n", "kind": "note"},
        {"seq": 4, "section_id": "h", "kind": "heading"},
    ]
    plan = plan_chunks(segs, [5, 100, 100, 50, 5], body_sections={"a", "b", "h"})
    assert [(a, b) for a, b, _ in plan] == [(0, 1), (2, 2)]


def test_bm25_stemming_and_query():
    doc_ids, doc_vals = sparse.encode_document("Les crimes du criminel", "fr")
    q_ids, _ = sparse.encode_query("crime", "fr")
    assert set(q_ids) <= set(doc_ids)
    assert all(v > 0 for v in doc_vals)
    assert sparse.encode_query("x", "xx")[0]  # langue sans racinisation


def test_align_merge_split_drop():
    rng = np.random.default_rng(0)
    a = rng.normal(size=(60, 32))
    a /= np.linalg.norm(a, axis=1, keepdims=True)
    rows = []
    for i in range(60):
        if i in (11, 40):
            continue
        v = a[i] + (a[i + 1] if i == 10 else 0) + rng.normal(scale=0.05, size=32)
        if i == 25:
            rows += [a[i] + rng.normal(scale=0.05, size=32), a[i] + rng.normal(scale=0.05, size=32)]
        else:
            rows.append(v)
    b = np.array(rows)
    b /= np.linalg.norm(b, axis=1, keepdims=True)
    groups = align(a, b, threshold=0.4)
    irregular = [(g.a, g.b) for g in groups if len(g.a) != 1 or len(g.b) != 1]
    assert irregular == [([10, 11], [10]), ([25], [24, 25]), ([40], [])]


def test_catalog_validation(tmp_path):
    work = tmp_path / "hugo-victor" / "notre-dame"
    work.mkdir(parents=True)
    (work / "fr.epub").write_bytes(b"")
    (work / "orphelin.epub").write_bytes(b"")
    (work / "work.toml").write_text(
        '[work]\ntitle = "Notre-Dame"\nauthors = ["Victor Hugo"]\noriginal_language = "fr"\n'
        '[[editions]]\nfile = "fr.epub"\nlanguage = "fr"\noriginal = true\n'
        '[[editions]]\nfile = "en.epub"\nlanguage = "en"\n',
        "utf-8",
    )
    bad = tmp_path / "Mauvais Nom" / "x"
    bad.mkdir(parents=True)
    (bad / "fr.epub").write_bytes(b"")

    catalog = scan(tmp_path)
    messages = " | ".join(i.message for i in catalog.issues)
    assert "en.epub déclaré mais absent" in messages
    assert "non déclaré" in messages
    assert "work.toml manquant" in messages
    assert "minuscules" in messages
    assert catalog.works == []  # erreurs bloquantes


def test_catalog_edition_access(tmp_path):
    work = tmp_path / "hugo-victor" / "notre-dame"
    work.mkdir(parents=True)
    (work / "fr.epub").write_bytes(b"")
    (work / "en.epub").write_bytes(b"")
    manifest = (
        '[work]\ntitle = "Notre-Dame"\nauthors = ["Victor Hugo"]\n'
        '[[editions]]\nfile = "fr.epub"\nlanguage = "fr"\noriginal = true\naccess = "open"\n'
        '[[editions]]\nfile = "en.epub"\nlanguage = "en"\n'
    )
    (work / "work.toml").write_text(manifest, "utf-8")
    [w] = scan(tmp_path).works
    assert [e.access for e in w.editions] == ["open", "restricted"]  # défaut : rien n'est publié

    (work / "work.toml").write_text(manifest.replace('"open"', '"public"'), "utf-8")
    catalog = scan(tmp_path)
    assert catalog.works == []
    assert any("access" in i.message or "access" in str(i.path) for i in catalog.issues)


def test_language_detection_short_and_pre_reform_russian():
    from swann_ingest.text.language import detect_language

    assert detect_language("Помогите, братцы! Больше нету мочи! Я — полукрестьянин, Я — полурабочий.") == "ru"
    assert (
        detect_language(
            "Вѣтеръ воетъ, вьюга злится, и снѣгъ кружится надъ полемъ; тихо всё въ деревнѣ спитъ."
        )
        == "ru"
    )
