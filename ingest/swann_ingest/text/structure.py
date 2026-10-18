"""Construction de l'arbre des sections à partir de la table des matières (ou,
à défaut, des titres), puis classement : type, numéro, préliminaires / corps /
annexes."""

from __future__ import annotations

import re
import uuid
from dataclasses import dataclass, field

from swann_ingest.epub.blocks import Block
from swann_ingest.epub.toc import Landmark, TocEntry
from swann_ingest.text.headings import HeadingInfo, merge_heading_texts, parse_heading

GUTENBERG_MARKER = re.compile(r"\*{3}\s*(?P<which>START|END)\s+OF\s+(THE|THIS)\s+PROJECT\s+GUTENBERG", re.I)

# epub:type -> type de section
TYPE_TO_KIND = {
    "volume": "volume",
    "part": "part",
    "chapter": "chapter",
    "subchapter": "section",
    "division": "book",
    "prologue": "prologue",
    "epilogue": "epilogue",
    "preface": "preface",
    "foreword": "foreword",
    "introduction": "introduction",
    "afterword": "afterword",
    "appendix": "appendix",
    "dedication": "dedication",
    "epigraph": "epigraph",
    "glossary": "glossary",
    "endnotes": "notes",
    "footnotes": "notes",
    "rearnotes": "notes",
}
FRONT_TYPES = {
    "frontmatter",
    "titlepage",
    "halftitlepage",
    "cover",
    "toc",
    "copyright-page",
    "imprint",
    "loi",
    "lot",
    "seriespage",
    "other-credits",
    "contributors",
}
BACK_TYPES = {
    "backmatter",
    "colophon",
    "bibliography",
    "index",
    "acknowledgments",
    "endnotes",
    "rearnotes",
    "footnotes",
    "appendix",
    "afterword",
    "glossary",
}
FRONT_KINDS = {"dedication", "epigraph", "preface", "foreword", "introduction"}
BACK_KINDS = {"afterword", "appendix", "glossary", "notes"}
BODY_ANCHOR_KINDS = {"volume", "part", "book", "chapter", "prologue"}
# Sections toujours dans le corps, même rangées sous une page préliminaire
# (Standard Ebooks place les parties sous la page de faux-titre).
BODY_KINDS = BODY_ANCHOR_KINDS | {"epilogue"}
# Rang des divisions, pour réimbriquer une table des matières aplatie.
RANK = {"volume": 0, "part": 1, "book": 2, "chapter": 3}


@dataclass
class Section:
    kind: str = "other"
    matter: str = "body"
    label: str | None = None
    title: str | None = None
    number: int | None = None
    source_href: str | None = None
    blocks: list[Block] = field(default_factory=list)
    children: list[Section] = field(default_factory=list)
    note_key: str | None = None  # pour les sections 'note'
    id: uuid.UUID = field(default_factory=uuid.uuid4)
    # interne
    start: int = 0
    toc_label: str | None = None
    explicit_matter: str | None = None

    def walk(self):
        yield self
        for c in self.children:
            yield from c.walk()


def strip_gutenberg(blocks: list[Block]) -> list[Block]:
    """Retire l'en-tête et la licence Project Gutenberg (marqueurs *** START/END ***)."""
    start, end = 0, len(blocks)
    for i, b in enumerate(blocks):
        m = GUTENBERG_MARKER.search(b.text[:300])
        if not m:
            continue
        if m["which"].upper() == "START" and i < len(blocks) // 2:
            start = i + 1
        elif m["which"].upper() == "END" and i > start:
            end = i
            break
    if start == 0 and end == len(blocks):
        return blocks
    kept = blocks[start:end]
    if kept and start:
        # les ancres des blocs retirés en tête ne doivent pas être perdues
        for b in blocks[:start]:
            kept[0].anchors |= b.anchors
    return kept


# --------------------------------------------------------------------------- TOC
def _map_toc(entries: list[TocEntry], blocks: list[Block]) -> list[tuple[TocEntry, int, int]]:
    """Retourne (entrée, index du premier bloc, profondeur) en ordre préfixe,
    en écartant les entrées introuvables ou hors de l'ordre de lecture."""
    doc_first: dict[str, int] = {}
    anchor_at: dict[tuple[str, str], int] = {}
    for i, b in enumerate(blocks):
        doc_first.setdefault(b.doc, i)
        for a in b.anchors:
            anchor_at.setdefault((b.doc, a), i)

    mapped: list[tuple[TocEntry, int, int]] = []
    last = -1

    def visit(entry: TocEntry, depth: int) -> None:
        nonlocal last
        idx = anchor_at.get((entry.path, entry.fragment)) if entry.fragment else doc_first.get(entry.path)
        if idx is not None and idx >= last:
            mapped.append((entry, idx, depth))
            last = idx
            child_depth = depth + 1
        else:
            child_depth = depth  # entrée écartée : ses enfants remontent d'un niveau
        for child in entry.children:
            visit(child, child_depth)

    for e in entries:
        visit(e, 0)
    return mapped


def _tree_from_starts(starts: list[tuple[int, int, str | None, str | None]]) -> list[Section]:
    """starts : (index de début, profondeur, libellé TOC, href) en ordre préfixe."""
    roots: list[Section] = []
    stack: list[tuple[int, Section]] = []
    for start, depth, label, href in starts:
        section = Section(start=start, toc_label=label, source_href=href)
        while stack and stack[-1][0] >= depth:
            stack.pop()
        (stack[-1][1].children if stack else roots).append(section)
        stack.append((depth, section))
    return roots


def _sections_from_toc(toc: list[TocEntry], blocks: list[Block]) -> list[Section] | None:
    mapped = _map_toc(toc, blocks)
    if len(mapped) < 2:
        return None
    starts = [
        (idx, depth, e.label, e.path + (f"#{e.fragment}" if e.fragment else "")) for e, idx, depth in mapped
    ]
    return _tree_from_starts(starts)


def _is_structural(info: HeadingInfo) -> bool:
    return info.kind is not None or info.number is not None


def _sections_from_headings(blocks: list[Block]) -> list[Section]:
    starts = []
    prev_heading = False
    for i, b in enumerate(blocks):
        if b.kind != "heading":
            prev_heading = False
            continue
        # Un titre qui suit immédiatement un autre titre est un sous-titre, sauf
        # s'il est lui-même structurel ("Première partie" puis "Chapitre I").
        if not prev_heading or _is_structural(parse_heading(b.text)):
            level = b.heading_level or 6
            href = b.doc + (f"#{min(b.anchors)}" if b.anchors else "")
            starts.append((i, level, None, href))
        prev_heading = True
    return _tree_from_starts(starts)


# ------------------------------------------------------------------- attribution
def _assign_blocks(roots: list[Section], blocks: list[Block]) -> Section | None:
    """Range chaque bloc dans la section commencée le plus récemment ; renvoie
    une section préliminaire implicite pour les blocs d'avant la première.

    Quand plusieurs sections imbriquées commencent au même bloc (une partie et
    son premier chapitre), les titres consécutifs sont distribués de la plus
    externe à la plus interne : "Première partie" va à la partie, "Chapitre I"
    au chapitre.
    """
    flat = [s for r in roots for s in r.walk()]
    groups: dict[int, list[Section]] = {}
    for s in flat:
        groups.setdefault(s.start, []).append(s)

    front = None
    k = -1
    group: list[Section] | None = None
    gp = 0
    for i, b in enumerate(blocks):
        while k + 1 < len(flat) and flat[k + 1].start <= i:
            k += 1
        if k < 0:
            if front is None:
                front = Section(kind="other", matter="front", start=0, explicit_matter="front")
            front.blocks.append(b)
            continue
        if i in groups and len(groups[i]) > 1:
            group, gp = groups[i], 0
            (group[0] if b.kind == "heading" else group[-1]).blocks.append(b)
            if b.kind != "heading":
                group = None
            continue
        if group is not None and b.kind == "heading" and gp + 1 < len(group):
            gp += 1
            group[gp].blocks.append(b)
            continue
        group = None
        flat[k].blocks.append(b)
    return front


def _describe(section: Section) -> None:
    leading = []
    for b in section.blocks:
        if b.kind != "heading":
            break
        leading.append(b.text)
    h = merge_heading_texts(leading) if leading else HeadingInfo()
    t = parse_heading(section.toc_label) if section.toc_label else HeadingInfo()

    types = section.blocks[0].start_types if section.blocks else set()
    type_kind = next((TYPE_TO_KIND[t_] for t_ in types if t_ in TYPE_TO_KIND), None)

    section.kind = h.kind or t.kind or type_kind or "other"
    if section.kind == "other" and type_kind:
        section.kind = type_kind
    section.number = h.number if h.number is not None else t.number
    section.label = h.label or t.label
    section.title = h.title or t.title
    if not section.label and not section.title and section.toc_label:
        section.title = section.toc_label

    if types & FRONT_TYPES or h.matter == "front" or t.matter == "front":
        section.explicit_matter = "front"
    elif types & BACK_TYPES or h.matter == "back" or t.matter == "back":
        section.explicit_matter = "back"
    elif section.kind in FRONT_KINDS:
        section.explicit_matter = "front"
    elif section.kind in BACK_KINDS:
        section.explicit_matter = "back"


def _collapse(sections: list[Section]) -> None:
    """Fusionne "numéro seul" + "sous-section titre" en une seule section.

    Gutenberg produit souvent : I (titre seul) > LA GRAND'SALLE (le texte).
    """
    for s in sections:
        _collapse(s.children)
        if (
            len(s.children) == 1
            and not s.children[0].children
            and all(b.kind == "heading" for b in s.blocks)
            and s.children[0].kind == "other"
            and s.children[0].number is None
        ):
            child = s.children[0]
            s.blocks += child.blocks
            s.title = s.title or child.title or child.label
            s.children = []


def _renest(sections: list[Section]) -> list[Section]:
    """Range les chapitres sous le livre / la partie qui les précède quand la
    table des matières les a mis au même niveau."""
    out: list[Section] = []
    stack: list[tuple[int, Section]] = []
    for s in sections:
        s.children = _renest(s.children)
        rank = RANK.get(s.kind)
        if rank is None:
            stack.clear()
            out.append(s)
            continue
        while stack and stack[-1][0] >= rank:
            stack.pop()
        (stack[-1][1].children if stack else out).append(s)
        stack.append((rank, s))
    return out


def _classify(roots: list[Section], landmark_body_start: int | None) -> list[Section]:
    for r in roots:
        for s in r.walk():
            _describe(s)
    _collapse(roots)
    for r in roots:
        for s in r.walk():
            if s.kind == "other" and s.number is not None and not s.explicit_matter:
                s.kind = "chapter"
    roots = _renest(roots)
    flat = [s for r in roots for s in r.walk()]

    if landmark_body_start is not None:
        body_start = landmark_body_start
    else:
        anchor = next((s for s in flat if s.kind in BODY_ANCHOR_KINDS), None)
        body_start = anchor.start if anchor else 0

    def apply(section: Section, inherited: str | None) -> None:
        if section.explicit_matter:
            section.matter = section.explicit_matter
        elif section.kind in BODY_KINDS:
            section.matter = "body"
        elif inherited:
            section.matter = inherited
        else:
            section.matter = "front" if section.start < body_start else "body"
        for c in section.children:
            apply(c, section.matter if section.matter != "body" else None)

    for r in roots:
        apply(r, None)
    return roots


def _ensure_body(roots: list[Section]) -> None:
    """Aucun bloc dans le corps : le titre de l'ouvrage a été pris pour un
    intertitre liminaire (« Einführung in die … », « Préface à … ») et tout le
    livre a suivi. Le liminaire devient alors le corps."""
    sections = [s for r in roots for s in r.walk()]
    if any(s.matter == "body" and s.blocks for s in sections):
        return
    for s in sections:
        if s.matter == "front" and s.kind != "toc":
            s.matter = "body"


def _prune(sections: list[Section]) -> list[Section]:
    kept = []
    for s in sections:
        s.children = _prune(s.children)
        if s.blocks or s.children:
            kept.append(s)
    return kept


def build_structure(
    blocks: list[Block],
    toc: list[TocEntry],
    landmarks: list[Landmark],
    note_blocks: dict[str, list[Block]],
    note_labels: dict[str, str],
) -> tuple[list[Section], str]:
    """Retourne (sections racines, méthode utilisée : 'toc' ou 'headings')."""
    roots = _sections_from_toc(toc, blocks)
    method = "toc"
    if roots is None:
        roots = _sections_from_headings(blocks)
        method = "headings"

    front = _assign_blocks(roots, blocks)
    if front is not None:
        roots.insert(0, front)

    body_start = None
    for lm in landmarks:
        if lm.type in ("bodymatter", "text", "start"):
            for i, b in enumerate(blocks):
                if b.doc == lm.path and (not lm.fragment or lm.fragment in b.anchors):
                    body_start = i
                    break
            break
    roots = _classify(roots, body_start)
    roots = _prune(roots)
    _ensure_body(roots)

    if note_blocks:
        holder = next((s for r in roots for s in r.walk() if s.kind == "notes"), None)
        if holder is None:
            holder = Section(kind="notes", matter="back", title="Notes")
            roots.append(holder)
        holder.matter = "back"
        for n, (key, nblocks) in enumerate(note_blocks.items(), start=1):
            if not nblocks:
                continue
            for b in nblocks:
                b.kind = "note"
            holder.children.append(
                Section(
                    kind="note",
                    matter="back",
                    label=note_labels.get(key) or str(n),
                    number=n,
                    source_href=key,
                    blocks=nblocks,
                    note_key=key,
                )
            )
    return roots, method
