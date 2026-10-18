"""Sections + blocs -> enregistrements prêts pour Postgres (sections, segments,
appels de note, sauts de page), avec positions dans le texte reconstruit.

Le texte reconstruit d'une édition = segments joints par "\\n\\n" (vue
`edition_texts`) ; char_start / char_end et les positions de page s'y réfèrent.
"""

from __future__ import annotations

import html
import re
import uuid
from dataclasses import dataclass, field

from swann_ingest.epub.blocks import Block, NoteCall, Span
from swann_ingest.epub.notes import note_origin
from swann_ingest.text.structure import Section

# Au-delà, un paragraphe est découpé en plusieurs segments (en fin de phrase),
# pour qu'un chunk contienne toujours des segments entiers. ~1200 caractères
# ≈ 300-450 tokens selon la langue.
MAX_SEGMENT_CHARS = 1200
SEPARATOR = "\n\n"

SENTENCE_END = re.compile(r"[.!?…]+[»\"”’)\]]*\s+")
SENTENCE_START_CHARS = set('«"“‘—–-([')
MARKUP_TAGS = {
    "em": ("<em>", "</em>"),
    "strong": ("<strong>", "</strong>"),
    "sup": ("<sup>", "</sup>"),
    "sub": ("<sub>", "</sub>"),
    "smallcaps": ('<span class="smallcaps">', "</span>"),
}


@dataclass
class SectionRecord:
    id: uuid.UUID
    parent_id: uuid.UUID | None
    seq: int
    kind: str
    matter: str
    label: str | None
    title: str | None
    number: int | None
    source_href: str | None


@dataclass
class SegmentRecord:
    id: uuid.UUID
    section_id: uuid.UUID
    seq: int
    kind: str
    char_start: int
    char_end: int
    text: str
    markup: str | None


@dataclass
class NoteRefRecord:
    segment_id: uuid.UUID
    char_offset: int
    label: str
    note_section_id: uuid.UUID
    origin: str


@dataclass
class PageBreakRecord:
    char_offset: int
    label: str


@dataclass
class EditionText:
    sections: list[SectionRecord] = field(default_factory=list)
    segments: list[SegmentRecord] = field(default_factory=list)
    note_refs: list[NoteRefRecord] = field(default_factory=list)
    page_breaks: list[PageBreakRecord] = field(default_factory=list)
    # matter de chaque section, pour les statistiques et l'échantillon de langue
    matter: dict[uuid.UUID, str] = field(default_factory=dict)


def render_markup(text: str, spans: list[Span]) -> str | None:
    """Texte brut + intervalles de mise en forme -> HTML minimal (ou None)."""
    if not spans and "\n" not in text:
        return None
    spans = sorted((s for s in spans if s.end > s.start), key=lambda s: (s.start, -s.end))
    bounds = sorted({0, len(text), *(s.start for s in spans), *(s.end for s in spans)})
    out: list[str] = []
    stack: list[Span] = []
    i = 0
    pos = 0
    for p in bounds:
        out.append(html.escape(text[pos:p], quote=False).replace("\n", "<br/>"))
        pos = p
        while stack and stack[-1].end <= p:
            out.append(MARKUP_TAGS[stack.pop().tag][1])
        while i < len(spans) and spans[i].start == p:
            out.append(MARKUP_TAGS[spans[i].tag][0])
            stack.append(spans[i])
            i += 1
    while stack:
        out.append(MARKUP_TAGS[stack.pop().tag][1])
    return "".join(out)


def split_points(text: str, max_chars: int = MAX_SEGMENT_CHARS) -> list[tuple[int, int]]:
    """Découpe un texte trop long en morceaux de phrases entières."""
    if len(text) <= max_chars:
        return [(0, len(text))]
    starts = [0]
    for m in SENTENCE_END.finditer(text):
        nxt = text[m.end() : m.end() + 1]
        if nxt and (nxt.isupper() or nxt.isdigit() or nxt in SENTENCE_START_CHARS):
            starts.append(m.end())
    sentences = [(a, b) for a, b in zip(starts, [*starts[1:], len(text)], strict=True)]

    # Phrases elles-mêmes trop longues : coupe sur un blanc.
    pieces: list[tuple[int, int]] = []
    for a, b in sentences:
        while b - a > max_chars:
            cut = text.rfind(" ", a + max_chars // 2, a + max_chars)
            cut = cut + 1 if cut > a else a + max_chars
            pieces.append((a, cut))
            a = cut
        pieces.append((a, b))

    # Regroupement glouton jusqu'à max_chars.
    out: list[tuple[int, int]] = []
    cur_a, cur_b = pieces[0]
    for a, b in pieces[1:]:
        if b - cur_a <= max_chars:
            cur_b = b
        else:
            out.append((cur_a, cur_b))
            cur_a, cur_b = a, b
    out.append((cur_a, cur_b))
    return out


def _slice_block(block: Block, a: int, b: int, last: bool):
    raw = block.text[a:b]
    text = raw.rstrip()
    n = len(text)
    spans = [
        Span(max(s.start, a) - a, min(s.end, a + n) - a, s.tag)
        for s in block.spans
        if s.start < a + n and s.end > a
    ]
    calls = [
        NoteCall(c.offset - a, c.label, c.target)
        for c in block.note_calls
        if a <= c.offset < b or (last and c.offset >= b)
    ]
    pages = [(o - a, label) for o, label in block.page_breaks if a <= o < b or (last and o >= b)]
    return (
        text,
        spans,
        [NoteCall(min(c.offset, n), c.label, c.target) for c in calls],
        [(min(o, n), label) for o, label in pages],
    )


def build_records(roots: list[Section]) -> EditionText:
    out = EditionText()
    note_sections: dict[str, Section] = {}
    pending_calls: list[tuple[uuid.UUID, NoteCall]] = []
    pos = 0

    def visit(section: Section, parent: Section | None) -> None:
        nonlocal pos
        out.sections.append(
            SectionRecord(
                id=section.id,
                parent_id=parent.id if parent else None,
                seq=len(out.sections),
                kind=section.kind,
                matter=section.matter,
                label=section.label,
                title=section.title,
                number=section.number,
                source_href=section.source_href,
            )
        )
        out.matter[section.id] = section.matter
        if section.note_key:
            note_sections[section.note_key] = section
        for block in section.blocks:
            ranges = split_points(block.text) if block.kind != "heading" else [(0, len(block.text))]
            for j, (a, b) in enumerate(ranges):
                text, spans, calls, pages = _slice_block(block, a, b, last=j == len(ranges) - 1)
                if not text:
                    continue
                if out.segments:
                    pos += len(SEPARATOR)
                seg = SegmentRecord(
                    id=uuid.uuid4(),
                    section_id=section.id,
                    seq=len(out.segments),
                    kind=block.kind,
                    char_start=pos,
                    char_end=pos + len(text),
                    text=text,
                    markup=render_markup(text, spans),
                )
                out.segments.append(seg)
                out.page_breaks.extend(PageBreakRecord(pos + o, label) for o, label in pages)
                pending_calls.extend((seg.id, c) for c in calls)
                pos += len(text)
        for child in section.children:
            visit(child, section)

    for root in roots:
        visit(root, None)

    note_text = {key: " ".join(b.text for b in s.blocks) for key, s in note_sections.items()}
    seen: set[tuple[uuid.UUID, int]] = set()
    for seg_id, call in pending_calls:
        target = note_sections.get(call.target)
        if target is None or (seg_id, call.offset) in seen:
            continue
        seen.add((seg_id, call.offset))
        out.note_refs.append(
            NoteRefRecord(
                segment_id=seg_id,
                char_offset=call.offset,
                label=call.label or (target.label or "?"),
                note_section_id=target.id,
                origin=note_origin(note_text[call.target]),
            )
        )
    # Deux sauts de page au même endroit : on garde le dernier.
    out.page_breaks = list({p.char_offset: p for p in out.page_breaks}.values())
    return out
