"""Classement des dépôts et signaux de qualité (fonctions pures, sans base)."""

from __future__ import annotations

from swann_ingest import quality
from swann_ingest.identify import (
    _translit,
    clean_person_name,
    edition_language,
    name_similarity,
    ratio,
    title_key,
    year_of_date,
)
from swann_ingest.text.language import detect_majority


def test_title_key_drops_articles_and_subtitles():
    assert title_key("Les Démons") == "demons"
    assert title_key("The Magic Mountain: A Novel") == "magic mountain"
    assert title_key("Buddenbrooks. Verfall einer Familie") == "buddenbrooks"
    assert title_key("Le") == "le"  # un titre réduit à un article reste lisible


def test_name_similarity_handles_order_and_transliteration():
    assert name_similarity("Dostoevsky, Fyodor", "Fyodor Dostoevsky") == 1.0
    assert name_similarity("Fiodor Dostoïevski", "Fyodor Dostoevsky") > 0.8
    assert name_similarity("Thomas Mann", "Heinrich Mann") < 0.8
    assert name_similarity("Victor Hugo", "Leo Tolstoy") < 0.5


def test_clean_person_name():
    assert clean_person_name("Dostoevsky, Fyodor") == "Fyodor Dostoevsky"
    assert clean_person_name("Hugo, Victor, 1802-1885") == "Victor Hugo"
    assert clean_person_name("  Thomas   Mann ") == "Thomas Mann"


def test_edition_language_keeps_meaningful_regions():
    assert edition_language("en-US") == "en"
    assert edition_language("FR") == "fr"
    assert edition_language("pt-BR") == "pt-BR"
    assert edition_language(None) is None


def test_helpers():
    assert ratio("crime et chatiment", "crime et chatiment") == 1.0
    assert ratio("", "x") == 0.0
    assert year_of_date("1866-01-01") == 1866 and year_of_date(None) is None
    assert _translit("записки из подполья") == "zapiski iz podpolia"


def test_detect_majority_votes_on_three_samples():
    english = ["It was a dark and stormy night, and the rain fell in torrents across the city."] * 30
    assert detect_majority(english) == "en"
    assert detect_majority([]) is None


def metrics(**kw) -> dict:
    base = {
        "n_segments": 1000,
        "n_body_segments": 900,
        "n_text_sections": 20,
        "n_chapters": 20,
        "body_chars": 400_000,
        "total_chars": 420_000,
        "max_segment_chars": 1200,
        "median_segment_chars": 250.0,
        "empty_sections": 0,
        "unreferenced_notes": 0,
        "parse_warnings": [],
        "structure_method": "toc",
        "detected_language": "fr",
        "language": "fr",
        "active_index": "chunks",
        "active_index_points": 500,
        "alignment": None,
        "work_editions": 1,
        "has_epub": True,
    }
    return base | kw


def codes(m: dict) -> set[str]:
    return {s.code for s in quality._signals(m)}


def test_clean_edition_has_no_signal():
    assert codes(metrics()) == set()


def test_signals():
    assert codes(metrics(n_segments=0)) == {"empty_body"}
    assert "no_toc" in codes(metrics(structure_method="headings"))
    assert "language_mismatch" in codes(metrics(detected_language="de"))
    assert "language_mismatch" not in codes(metrics(language="fr-CA"))
    assert "giant_segments" in codes(metrics(max_segment_chars=9000))
    assert "tiny_segments" in codes(metrics(median_segment_chars=20.0))
    assert "no_chapters" in codes(metrics(n_text_sections=1))
    assert "out_of_body" in codes(metrics(total_chars=1_000_000))
    assert "not_indexed" in codes(metrics(active_index_points=None))
    assert "not_aligned" in codes(metrics(work_editions=2))
    doubtful = {"status": "doubtful", "aligned_ratio": 0.4, "is_reference": False}
    assert "alignment_doubtful" in codes(metrics(alignment=doubtful))
    reference = {"status": "reliable", "aligned_ratio": 1.0, "is_reference": True}
    assert "alignment_doubtful" not in codes(metrics(alignment=reference, work_editions=2))
    # Les avertissements de table des matières ont leur propre signal (no_toc)
    assert "parse_warnings" not in codes(metrics(parse_warnings=["pas de table des matières : …"]))
    assert "parse_warnings" in codes(metrics(parse_warnings=["document illisible : x.xhtml"]))


def test_weights_cover_all_signals():
    produced = codes(
        metrics(
            structure_method="headings",
            detected_language="de",
            max_segment_chars=9000,
            median_segment_chars=20.0,
            n_text_sections=1,
            total_chars=1_000_000,
            active_index_points=None,
            work_editions=2,
            empty_sections=2,
            unreferenced_notes=5,
            parse_warnings=["x"],
            has_epub=False,
            body_chars=10_000,
        )
    )
    assert produced <= set(quality.WEIGHTS)
