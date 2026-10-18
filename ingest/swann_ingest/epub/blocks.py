"""XHTML -> suite de blocs typés (titre, paragraphe, vers, épigraphe, citation).

Chaque bloc porte son texte brut normalisé, la mise en forme en ligne sous
forme d'intervalles (italique, gras...), les appels de note et les marqueurs
de page qu'il contient, ainsi que les identifiants et epub:type des éléments
qui commencent avec lui (pour placer les débuts de sections).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from lxml import etree

from swann_ingest.epub.notes import NoteIndex, epub_types, local
from swann_ingest.text.normalize import clean_piece

BLOCK_TAGS = {
    "address",
    "article",
    "aside",
    "blockquote",
    "body",
    "center",
    "dd",
    "details",
    "dialog",
    "div",
    "dl",
    "dt",
    "fieldset",
    "figcaption",
    "figure",
    "footer",
    "form",
    "h1",
    "h2",
    "h3",
    "h4",
    "h5",
    "h6",
    "header",
    "hgroup",
    "hr",
    "li",
    "main",
    "nav",
    "ol",
    "p",
    "pre",
    "section",
    "summary",
    "table",
    "ul",
    "caption",
    # cellules : lues seulement dans un tableau de mise en page (_layout_table)
    "tbody",
    "thead",
    "tfoot",
    "tr",
    "td",
    "th",
}
LAYOUT_CELL_CHARS = 300  # texte moyen par cellule au-delà duquel un tableau est de la prose
HEADING_TAGS = {"h1", "h2", "h3", "h4", "h5", "h6"}
SKIP_TAGS = {
    "head",
    "script",
    "style",
    "svg",
    "math",
    "table",
    "img",
    "image",
    "object",
    "video",
    "audio",
    "iframe",
    "noscript",
    "template",
    "figure",
}
INLINE_STYLE = {
    "em": "em",
    "i": "em",
    "cite": "em",
    "dfn": "em",
    "strong": "strong",
    "b": "strong",
    "sup": "sup",
    "sub": "sub",
}
SMALLCAPS_RE = re.compile(r"small-?caps|\bsc\b|smcap", re.I)
VERSE_RE = re.compile(r"poem|verse|stanza|\blg\b|\bline-group\b|\bpoetry\b", re.I)
EPIGRAPH_RE = re.compile(r"epigraph", re.I)
GUTENBERG_IDS = {
    "pg-header",
    "pg-footer",
    "pg-machine-header",
    "pg-start-separator",
    "pg-end-separator",
    "pg-smallprint",
}
# Reste du numéro en tête d'une note une fois le lien retour retiré :
# "63 ()", "[12]", "(3)".
NOTE_LABEL_RE = re.compile(r"^\s*(?:\w{1,4}\s*\(\s*\)|\[\s*\w{1,4}\s*\]|\(\s*\w{1,4}\s*\))\s*")
HEADING_TYPES = {"title", "subtitle", "ordinal", "fulltitle", "label"}


@dataclass
class Span:
    start: int
    end: int
    tag: str


@dataclass
class NoteCall:
    offset: int
    label: str
    target: str  # clé de la note


@dataclass
class Block:
    kind: str  # heading | paragraph | verse | epigraph | quote
    text: str
    spans: list[Span]
    note_calls: list[NoteCall]
    page_breaks: list[tuple[int, str]]  # (position dans le bloc, numéro de page)
    doc: str
    anchors: set[str] = field(default_factory=set)  # id d'éléments commençant ici
    start_types: set[str] = field(default_factory=set)  # epub:type commençant ici
    heading_level: int | None = None


class TextBuilder:
    """Assemble le texte d'un bloc en normalisant les blancs au fil de l'eau,
    pour que les positions (mise en forme, notes, pages) restent exactes."""

    def __init__(self) -> None:
        self.parts: list[str] = []
        self.length = 0
        self.last = ""
        self.spans: list[Span] = []
        self.note_calls: list[NoteCall] = []
        self.page_breaks: list[tuple[int, str]] = []

    def add(self, raw: str | None) -> None:
        if not raw:
            return
        piece = clean_piece(raw)
        if piece.startswith(" ") and (self.length == 0 or self.last in (" ", "\n")):
            piece = piece[1:]
        if not piece:
            return
        self.parts.append(piece)
        self.length += len(piece)
        self.last = piece[-1]

    def newline(self) -> None:
        if self.length == 0:
            return
        if self.last == " ":
            self._drop_last_char()
        if self.last != "\n":
            self.parts.append("\n")
            self.length += 1
            self.last = "\n"

    def space(self) -> None:
        if self.length and self.last not in (" ", "\n"):
            self.add(" ")

    def _drop_last_char(self) -> None:
        tail = self.parts[-1][:-1]
        if tail:
            self.parts[-1] = tail
        else:
            self.parts.pop()
        self.length -= 1
        self.last = self.parts[-1][-1] if self.parts else ""

    def finish(self) -> tuple[str, list[Span], list[NoteCall], list[tuple[int, str]]]:
        text = "".join(self.parts)
        stripped = text.rstrip(" \n")
        n = len(stripped)
        spans = [Span(s.start, min(s.end, n), s.tag) for s in self.spans if s.start < min(s.end, n)]
        calls = [NoteCall(min(c.offset, n), c.label, c.target) for c in self.note_calls]
        pages = [(min(o, n), label) for o, label in self.page_breaks]
        return stripped, spans, calls, pages


def _is_pagebreak(el) -> bool:
    return "pagebreak" in epub_types(el) or el.get("role") == "doc-pagebreak"


def _page_label(el) -> str:
    label = el.get("title") or el.get("aria-label") or " ".join("".join(el.itertext()).split())
    if not label and el.get("id"):
        label = re.sub(r"^\D*", "", el.get("id"))
    return label or "?"


class BlockExtractor:
    def __init__(self, notes: NoteIndex) -> None:
        self.notes = notes
        self.blocks: list[Block] = []
        self.pending_anchors: set[str] = set()
        self.pending_types: set[str] = set()
        self.pending_pages: list[str] = []
        self.doc = ""
        # Mode "corps de note" : on ignore les liens retour et on n'extrait pas
        # les notes imbriquées comme appels.
        self.in_note = False

    # ------------------------------------------------------------------ blocs
    def extract_document(self, doc: str, root) -> None:
        self.doc = doc
        body = next((el for el in root.iter() if local(el) == "body"), root)
        self.pending_types |= epub_types(body)
        self._walk(body, frozenset())

    def extract_note(self, doc: str, container) -> list[Block]:
        saved = self.blocks, self.doc, self.in_note
        self.blocks, self.doc, self.in_note = [], doc, True
        self._walk(container, frozenset(), is_note_root=True)
        blocks = self.blocks
        self.blocks, self.doc, self.in_note = saved
        if blocks and (m := NOTE_LABEL_RE.match(blocks[0].text)) and m.end() < len(blocks[0].text):
            _drop_prefix(blocks[0], m.end())
        return blocks

    def _skip(self, el, is_note_root: bool = False) -> bool:
        tag = local(el)
        if not tag or (tag in SKIP_TAGS and not (tag == "table" and _layout_table(el))):
            return True
        if el.get("id") in GUTENBERG_IDS or (el.get("class") or "") in GUTENBERG_IDS:
            return True
        if el.get("hidden") is not None or "display:none" in (el.get("style") or "").replace(" ", ""):
            return True
        return not is_note_root and el in self.notes.containers

    def _context(self, el, ctx: frozenset[str]) -> frozenset[str]:
        markers = f"{el.get('class') or ''} {' '.join(epub_types(el))}"
        add = set()
        if VERSE_RE.search(markers) or "z3998:poem" in markers or "z3998:verse" in markers:
            add.add("verse")
        if EPIGRAPH_RE.search(markers):
            add.add("epigraph")
        if local(el) == "blockquote":
            add.add("quote")
        return ctx | add if add else ctx

    def _note_anchor(self, el) -> None:
        if el.get("id"):
            self.pending_anchors.add(el.get("id"))
        if local(el) == "a" and el.get("name"):
            self.pending_anchors.add(el.get("name"))
        self.pending_types |= epub_types(el)

    def _walk(self, el, ctx: frozenset[str], is_note_root: bool = False) -> None:
        if self._skip(el, is_note_root):
            return
        if _is_pagebreak(el):
            self.pending_pages.append(_page_label(el))
            return
        self._note_anchor(el)
        ctx = self._context(el, ctx)
        children = [c for c in el if isinstance(c.tag, str)]
        has_block_child = any(local(c) in BLOCK_TAGS for c in children)
        if not has_block_child:
            if local(el) not in ("body", "hr"):
                self._emit_leaf(el, ctx)
            return
        # Conteneur mixte : le texte entre les blocs enfants forme des
        # paragraphes anonymes.
        run = TextBuilder()
        self._inline_text(run, el.text)
        for child in el:
            if not isinstance(child.tag, str):
                self._inline_text(run, child.tail)
                continue
            if local(child) in BLOCK_TAGS:
                self._flush(run, el, ctx)
                run = TextBuilder()
                self._walk(child, ctx)
            else:
                self._inline(run, child)
            self._inline_text(run, child.tail)
        self._flush(run, el, ctx)

    def _flush(self, run: TextBuilder, el, ctx) -> None:
        if run.length:
            self._push(run, self._kind(el, ctx), None)

    def _emit_leaf(self, el, ctx) -> None:
        builder = TextBuilder()
        self._inline_text(builder, el.text)
        for child in el:
            if isinstance(child.tag, str):
                self._inline(builder, child)
            self._inline_text(builder, child.tail)
        level = int(local(el)[1]) if local(el) in HEADING_TAGS else None
        self._push(builder, self._kind(el, ctx), level)

    def _kind(self, el, ctx) -> str:
        tag = local(el)
        if tag in HEADING_TAGS or epub_types(el) & HEADING_TYPES:
            return "heading"
        if "verse" in ctx:
            return "verse"
        if "epigraph" in ctx:
            return "epigraph"
        if "quote" in ctx:
            return "quote"
        return "paragraph"

    def _push(self, builder: TextBuilder, kind: str, level: int | None) -> None:
        text, spans, calls, pages = builder.finish()
        if not text.strip():
            # Bloc vide (image, séparateur) : ses ancres et pages passent au suivant.
            self.pending_pages.extend(label for _, label in pages)
            return
        pages = [(0, label) for label in self.pending_pages] + pages
        self.pending_pages = []
        if kind == "heading":
            text = text.replace("\n", " ")
        self.blocks.append(
            Block(
                kind=kind,
                text=text,
                spans=spans,
                note_calls=calls,
                page_breaks=pages,
                doc=self.doc,
                anchors=self.pending_anchors,
                start_types=self.pending_types,
                heading_level=level if kind == "heading" else None,
            )
        )
        self.pending_anchors, self.pending_types = set(), set()

    # ---------------------------------------------------------------- en ligne
    def _inline_text(self, builder: TextBuilder, text: str | None) -> None:
        builder.add(text)

    def _inline(self, builder: TextBuilder, el) -> None:
        tag = local(el)
        if self._skip(el):
            return
        if _is_pagebreak(el):
            builder.page_breaks.append((builder.length, _page_label(el)))
            return
        if tag == "br":
            builder.newline()
            return
        if el in self.notes.backlinks and self.in_note:
            return
        if el in self.notes.call_targets and not self.in_note:
            label = " ".join("".join(el.itertext()).split()).strip("[]()")
            builder.note_calls.append(NoteCall(builder.length, label, self.notes.call_targets[el]))
            return
        if el.get("id"):
            self.pending_anchors.add(el.get("id"))
        if tag in BLOCK_TAGS:
            builder.space()
        style = INLINE_STYLE.get(tag)
        if style is None and SMALLCAPS_RE.search(el.get("class") or ""):
            style = "smallcaps"
        start = builder.length
        builder.add(el.text)
        for child in el:
            if isinstance(child.tag, str):
                self._inline(builder, child)
            builder.add(child.tail)
        if style and builder.length > start:
            builder.spans.append(Span(start, builder.length, style))
        if tag in BLOCK_TAGS:
            builder.space()


def _drop_prefix(block: Block, n: int) -> None:
    """Retire les n premiers caractères d'un bloc en décalant les positions."""
    block.text = block.text[n:]
    block.spans = [Span(max(0, x.start - n), x.end - n, x.tag) for x in block.spans if x.end > n]
    block.note_calls = [NoteCall(max(0, c.offset - n), c.label, c.target) for c in block.note_calls]
    block.page_breaks = [(max(0, o - n), label) for o, label in block.page_breaks]


def extract_blocks(
    docs: dict[str, etree._Element], notes: NoteIndex
) -> tuple[list[Block], dict[str, list[Block]]]:
    """Retourne (blocs du fil du texte, blocs de chaque note par clé)."""
    extractor = BlockExtractor(notes)
    for doc, root in docs.items():
        extractor.extract_document(doc, root)
    note_blocks = {
        note.key: extractor.extract_note(note.key.split("#")[0], note.container) for note in notes.ordered()
    }
    return extractor.blocks, note_blocks


def _layout_table(table) -> bool:
    """Tableau de mise en page (poèmes de Wikisource, textes en regard) et
    non tableau de données : il contient un titre, un poème ou de longues
    cellules de prose."""
    cells = 0
    for el in table.iter():
        tag = local(el)
        if tag in HEADING_TAGS or VERSE_RE.search(el.get("class") or ""):
            return True
        cells += tag in ("td", "th")
    text = sum(len(t.strip()) for t in table.itertext())
    return cells > 0 and text / cells > LAYOUT_CELL_CHARS
