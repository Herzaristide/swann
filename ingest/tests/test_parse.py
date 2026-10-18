"""Lecture d'EPUB synthétiques couvrant les cas limites rencontrés sur de
vrais livres (Gutenberg, Standard Ebooks, Calibre)."""

from __future__ import annotations

import pytest
from conftest import lorem

from swann_ingest.epub.container import DrmError
from swann_ingest.parse import parse_epub


def sections_by_kind(parsed, kind):
    return [s for s in parsed.text.sections if s.kind == kind]


def segment_texts(parsed, section):
    return [s.text for s in parsed.text.segments if s.section_id == section.id]


def test_chapters_from_toc(make_epub):
    epub = make_epub(
        {
            "c1.xhtml": f"<h2>Chapitre I. Le départ</h2><p>{lorem()}</p><p>{lorem()}</p>",
            "c2.xhtml": f"<h2>Chapitre II</h2><p>{lorem()}</p>",
        },
        toc=[("Chapitre I", "c1.xhtml"), ("Chapitre II", "c2.xhtml")],
    )
    parsed = parse_epub(epub)
    chapters = sections_by_kind(parsed, "chapter")
    assert [(c.number, c.label, c.title) for c in chapters] == [
        (1, "Chapitre I", "Le départ"),
        (2, "Chapitre II", None),
    ]
    assert all(c.matter == "body" for c in chapters)
    assert parsed.structure_method == "toc"


def test_full_text_is_reconstructible(make_epub):
    epub = make_epub(
        {"c1.xhtml": f"<h2>I</h2><p>{lorem(2)}</p><p>Deuxième <em>paragraphe</em>.</p>"},
        toc=[("I", "c1.xhtml"), ("I bis", "c1.xhtml")],
    )
    parsed = parse_epub(epub)
    full = "\n\n".join(s.text for s in parsed.text.segments)
    for s in parsed.text.segments:
        assert full[s.char_start : s.char_end] == s.text


def test_flat_gutenberg_toc_is_renested_and_collapsed(make_epub):
    """Gutenberg : LIVRE PREMIER, I, LA SALLE (titre en sous-entrée), tous au
    même niveau que les livres -> livre > chapitre "I — LA SALLE"."""
    body = (
        '<h2 id="b1">LIVRE PREMIER</h2>'
        f'<h3 id="c1">I</h3><h4 id="t1">LA SALLE</h4><p>{lorem()}</p>'
        f'<h3 id="c2">II</h3><h4 id="t2">LA RUE</h4><p>{lorem()}</p>'
        '<h2 id="b2">LIVRE DEUXIÈME</h2>'
        f'<h3 id="c3">I</h3><h4 id="t3">LE PONT</h4><p>{lorem()}</p>'
    )
    toc = [
        ("LIVRE PREMIER", "all.xhtml#b1"),
        ("I", "all.xhtml#c1", [("LA SALLE", "all.xhtml#t1")]),
        ("II", "all.xhtml#c2", [("LA RUE", "all.xhtml#t2")]),
        ("LIVRE DEUXIÈME", "all.xhtml#b2"),
        ("I", "all.xhtml#c3", [("LE PONT", "all.xhtml#t3")]),
    ]
    parsed = parse_epub(make_epub({"all.xhtml": body}, toc))
    books = sections_by_kind(parsed, "book")
    chapters = sections_by_kind(parsed, "chapter")
    assert [(b.number, b.label) for b in books] == [(1, "LIVRE PREMIER"), (2, "LIVRE DEUXIÈME")]
    assert [(c.number, c.title) for c in chapters] == [(1, "LA SALLE"), (2, "LA RUE"), (1, "LE PONT")]
    assert all(c.parent_id in {b.id for b in books} for c in chapters)


def test_part_and_first_chapter_starting_together(make_epub):
    body = f'<h1 id="p1">Première partie</h1><h2 id="c1">Chapitre I</h2><p>{lorem()}</p>'
    toc = [("Première partie", "a.xhtml#p1", [("Chapitre I", "a.xhtml#c1")])]
    parsed = parse_epub(make_epub({"a.xhtml": body}, toc))
    part = sections_by_kind(parsed, "part")[0]
    chapter = sections_by_kind(parsed, "chapter")[0]
    assert segment_texts(parsed, part) == ["Première partie"]
    assert segment_texts(parsed, chapter)[0] == "Chapitre I"


def test_parts_under_halftitle_stay_in_body(make_epub):
    """Standard Ebooks range les parties sous la page de faux-titre."""
    docs = {
        "half.xhtml": '<section epub:type="halftitlepage"><h2>Le Roman</h2></section>',
        "p1.xhtml": '<section epub:type="part"><h2>Part I</h2></section>',
        "c1.xhtml": f'<section epub:type="chapter"><h3>I</h3><p>{lorem()}</p></section>',
    }
    toc = [("Le Roman", "half.xhtml", [("Part I", "p1.xhtml", [("I", "c1.xhtml")])])]
    parsed = parse_epub(make_epub(docs, toc))
    assert sections_by_kind(parsed, "part")[0].matter == "body"
    assert sections_by_kind(parsed, "chapter")[0].matter == "body"


def test_front_matter_before_first_chapter(make_epub):
    docs = {
        "title.xhtml": "<h1>Le Livre</h1><p>par Quelqu'un</p>",
        "pref.xhtml": f"<h2>Préface</h2><p>{lorem()}</p>",
        "c1.xhtml": f"<h2>Chapitre premier</h2><p>{lorem()}</p>",
    }
    toc = [("Le Livre", "title.xhtml"), ("Préface", "pref.xhtml"), ("Chapitre premier", "c1.xhtml")]
    parsed = parse_epub(make_epub(docs, toc))
    matters = {(s.kind, s.matter) for s in parsed.text.sections}
    assert ("preface", "front") in matters
    assert ("chapter", "body") in matters
    assert ("other", "front") in matters


def test_gutenberg_footnotes(make_epub):
    """Appel = ancre vide + lien ; note = div.footnote avec lien retour."""
    chapter = (
        f'<h2>Chapitre I</h2><p>{lorem()} Un mot<a id="FNanchor_1_1"/>'
        '<a class="fnanchor" href="notes.xhtml#Footnote_1_1">[1]</a> suivi de texte.</p>'
    )
    notes = (
        '<div class="footnote"><p><a id="Footnote_1_1"/><a href="c1.xhtml#FNanchor_1_1">'
        '<span class="label">[1]</span></a> Une note de bas de page (N.d.T.).</p></div>'
    )
    parsed = parse_epub(make_epub({"c1.xhtml": chapter, "notes.xhtml": notes}, [("Chapitre I", "c1.xhtml")]))
    assert len(parsed.text.note_refs) == 1
    ref = parsed.text.note_refs[0]
    segment = next(s for s in parsed.text.segments if s.id == ref.segment_id)
    assert segment.text[: ref.char_offset].endswith("Un mot")
    assert "[1]" not in segment.text
    assert ref.label == "1"
    assert ref.origin == "translator"
    note = next(s for s in parsed.text.sections if s.id == ref.note_section_id)
    assert note.kind == "note" and note.matter == "back"
    assert segment_texts(parsed, note) == ["Une note de bas de page (N.d.T.)."]


def test_old_gutenberg_endnotes(make_epub):
    """Ancre seule dans un <p> vide, texte de la note dans le <p> suivant."""
    chapter = f'<h2>Chapitre I</h2><p>{lorem()} Fin<a href="n.xhtml#note-1" id="ref-1">[1]</a>.</p>'
    notes = (
        '<p><a id="note-1"/></p><p class="footnote">1 (<a href="c1.xhtml#ref-1">return</a>)<br/>La note.</p>'
    )
    parsed = parse_epub(make_epub({"c1.xhtml": chapter, "n.xhtml": notes}, [("Chapitre I", "c1.xhtml")]))
    note = sections_by_kind(parsed, "note")[0]
    assert segment_texts(parsed, note) == ["La note."]


def test_noteref_epub3(make_epub):
    chapter = (
        f'<h2>Chapitre I</h2><p>{lorem()} Mot<a epub:type="noteref" href="#n1">1</a>.</p>'
        '<aside epub:type="footnote" id="n1"><p>Contenu de la note.</p></aside>'
        f"<p>{lorem(2)}</p>"
    )
    parsed = parse_epub(make_epub({"c1.xhtml": chapter}, [("Chapitre I", "c1.xhtml")]))
    body_texts = [s.text for s in parsed.text.segments if s.kind != "note"]
    assert not any("Contenu de la note" in t for t in body_texts)
    assert len(parsed.text.note_refs) == 1


def test_toc_backlink_is_not_a_note(make_epub):
    """Un lien "retour au sommaire" sous un titre ne fait pas du chapitre une note."""
    docs = {
        "toc.xhtml": '<p><a id="t1" href="c1.xhtml#c1">Chapitre I</a></p>',
        "c1.xhtml": f'<h2 id="c1"><a href="toc.xhtml#t1">Chapitre I</a></h2><p>{lorem(30)}</p>',
    }
    parsed = parse_epub(make_epub(docs, [("Sommaire", "toc.xhtml"), ("Chapitre I", "c1.xhtml")]))
    assert not sections_by_kind(parsed, "note")
    assert sections_by_kind(parsed, "chapter")


def test_markup_verse_and_pagebreaks(make_epub):
    chapter = (
        "<h2>Chapitre I</h2>"
        f'<p>{lorem()} Un mot <em>en <strong>italique</strong></em> ici.<span epub:type="pagebreak" title="12"/> Suite.</p>'
        '<div class="poem"><p>Premier vers,<br/>Second vers.</p></div>'
    )
    parsed = parse_epub(make_epub({"c1.xhtml": chapter}, [("Chapitre I", "c1.xhtml")]))
    prose = next(s for s in parsed.text.segments if "italique" in s.text)
    assert "<em>en <strong>italique</strong></em>" in prose.markup
    verse = next(s for s in parsed.text.segments if s.kind == "verse")
    assert verse.text == "Premier vers,\nSecond vers."
    assert verse.markup == "Premier vers,<br/>Second vers."
    (page,) = parsed.text.page_breaks
    full = "\n\n".join(s.text for s in parsed.text.segments)
    assert page.label == "12" and full[page.char_offset :].startswith(" Suite.")


def test_dropcap_and_soft_hyphen(make_epub):
    chapter = f'<h2>Chapitre I</h2><p><span class="dropcap">L</span>a nuit tom­bait. {lorem()}</p>'
    parsed = parse_epub(make_epub({"c1.xhtml": chapter}, [("Chapitre I", "c1.xhtml")]))
    assert any(s.text.startswith("La nuit tombait.") for s in parsed.text.segments)


def test_gutenberg_license_removed(make_epub):
    body = (
        "<p>The Project Gutenberg eBook of Test</p>"
        "<p>*** START OF THE PROJECT GUTENBERG EBOOK TEST ***</p>"
        f"<h2>Chapter I</h2><p>{lorem()}</p>"
        "<p>*** END OF THE PROJECT GUTENBERG EBOOK TEST ***</p>"
        "<p>Full license text.</p>"
    )
    parsed = parse_epub(make_epub({"a.xhtml": body}, None, language="en"))
    texts = " ".join(s.text for s in parsed.text.segments)
    assert "Gutenberg" not in texts and "license" not in texts
    assert parsed.structure_method == "headings"


def test_long_paragraph_split_on_sentences(make_epub):
    long = " ".join(f"Phrase numéro {i} assez longue pour compter." for i in range(80))
    parsed = parse_epub(
        make_epub({"c1.xhtml": f"<h2>Chapitre I</h2><p>{long}</p>"}, [("Chapitre I", "c1.xhtml")])
    )
    pieces = [s for s in parsed.text.segments if s.kind == "paragraph"]
    assert len(pieces) > 1
    assert all(len(p.text) <= 1200 for p in pieces)
    assert all(p.text.endswith(".") for p in pieces)


def test_drm_refused(make_epub):
    enc = (
        '<encryption xmlns="urn:oasis:names:tc:opendocument:xmlns:container" '
        'xmlns:enc="http://www.w3.org/2001/04/xmlenc#"><enc:EncryptedData>'
        '<enc:EncryptionMethod Algorithm="http://www.w3.org/2001/04/xmlenc#aes128-cbc"/>'
        '<enc:CipherData><enc:CipherReference URI="OEBPS/c1.xhtml"/></enc:CipherData>'
        "</enc:EncryptedData></encryption>"
    )
    epub = make_epub({"c1.xhtml": "<p>x</p>"}, None, extra_files={"META-INF/encryption.xml": enc})
    with pytest.raises(DrmError):
        parse_epub(epub)


def test_malformed_xhtml_falls_back_to_html_parser(make_epub):
    # Balises fermantes orphelines juste après <body> : le parseur XML
    # (recover) referme le document et perdrait tout le texte.
    epub = make_epub({"c1.xhtml": f"</span></div></header><pre/><h2>Chapitre I</h2><p>{lorem(40)}</p>"})
    parsed = parse_epub(epub)
    assert parsed.stats()["body_chars"] > 1000


def test_library_boilerplate_pages_are_dropped(make_epub):
    # Wolne Lektury : appel aux dons et page éditoriale après le texte
    epub = make_epub(
        {
            "part1.xhtml": f"<h2>Ślizgawka</h2><p>{lorem(5)}</p>",
            "fund0.xhtml": "<p>Książka, którą czytasz, pochodzi z biblioteki fundacji Wolne Lektury.</p>",
            "last.xhtml": "<p>Wszystkie zasoby Wolnych Lektur możesz swobodnie wykorzystywać.</p>",
        }
    )
    parsed = parse_epub(epub)
    text = " ".join(s.text for s in parsed.text.segments)
    assert "Wolne Lektury" not in text and "Wolnych Lektur" not in text
    assert "phrase de texte" in text


def test_page_named_like_boilerplate_is_kept_without_library_mark(make_epub):
    epub = make_epub(
        {"c1.xhtml": f"<p>{lorem(5)}</p>", "last.xhtml": f"<h2>Fin</h2><p>{lorem(5, 'fin')}</p>"}
    )
    parsed = parse_epub(epub)
    assert "phrase de fin" in " ".join(s.text for s in parsed.text.segments)


def test_layout_table_poem_is_read(make_epub):
    # Wikisource : le poème est mis en page dans un tableau
    poem = "<br/>".join(f"Строка стихотворения номер {i}" for i in range(12))
    epub = make_epub(
        {
            "c1.xhtml": "<table><tbody><tr><td><table class='poetry'><tbody><tr><td>"
            f"<h4>Раздвоение</h4><div class='poem'><p>{poem}</p></div>"
            "</td></tr></tbody></table></td></tr></tbody></table>"
        },
        language="ru",
    )
    parsed = parse_epub(epub)
    assert "Строка стихотворения номер 11" in " ".join(s.text for s in parsed.text.segments)


def test_data_table_is_still_skipped(make_epub):
    rows = "".join(f"<tr><td>{i}</td><td>{i * 2}</td></tr>" for i in range(20))
    epub = make_epub({"c1.xhtml": f"<p>{lorem(5)}</p><table>{rows}</table>"})
    parsed = parse_epub(epub)
    assert "38" not in " ".join(s.text for s in parsed.text.segments)


def test_book_title_mistaken_for_front_matter_heading_becomes_body(make_epub):
    # « Einführung in … » est le titre du livre, pas une introduction
    epub = make_epub(
        {"c1.xhtml": f"<h1>Einführung in die Darstellungsmethoden</h1><p>{lorem(10, 'Inhalt')}</p>"},
        language="de",
    )
    parsed = parse_epub(epub)
    assert parsed.stats()["body_chars"] > 0


def test_short_text_language_mismatch_is_only_a_warning(make_epub):
    from swann_ingest.catalog import EditionSpec, WorkSpec
    from swann_ingest.pipeline.extract import parse_all

    epub = make_epub({"c1.xhtml": "<p>This short English text is declared as French in its record.</p>"})
    work = WorkSpec("a/b", epub.parent, "T", None, None, ["A"], [], None)
    spec = EditionSpec(epub, "a/b/fr.epub", "fr", True, None, [], None, None)
    (result,) = parse_all([(work, spec)], workers=1)
    assert result.error is None
