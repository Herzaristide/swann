"""Lecture complète d'un EPUB : du fichier aux enregistrements Postgres.

Fonction pure (aucun accès réseau ni base), exécutée en parallèle par
`swann extract` et utilisée seule par `swann check` / `--dry-run`.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from swann_ingest.epub.blocks import extract_blocks
from swann_ingest.epub.boilerplate import is_boilerplate
from swann_ingest.epub.container import EpubError, OpfMetadata, open_epub
from swann_ingest.epub.notes import build_note_index
from swann_ingest.epub.toc import read_toc
from swann_ingest.text.segment import EditionText, build_records
from swann_ingest.text.structure import build_structure, strip_gutenberg


@dataclass
class ParsedEdition:
    path: Path
    sha256: str
    metadata: OpfMetadata
    structure_method: str  # "toc" ou "headings"
    text: EditionText
    warnings: list[str] = field(default_factory=list)

    def body_sample(self, max_chars: int = 6000) -> str:
        """Extrait du corps du texte (milieu du livre) pour vérifier la langue."""
        body = [
            s.text
            for s in self.text.segments
            if s.kind == "paragraph" and self.text.matter.get(s.section_id) == "body"
        ]
        mid = len(body) // 2
        sample, i = [], max(0, mid - 20)
        while i < len(body) and sum(map(len, sample)) < max_chars:
            sample.append(body[i])
            i += 1
        return "\n".join(sample)

    def body_paragraphs(self) -> list[str]:
        return [
            s.text
            for s in self.text.segments
            if s.kind == "paragraph" and self.text.matter.get(s.section_id) == "body"
        ]

    def stats(self) -> dict[str, int]:
        body = [s for s in self.text.segments if self.text.matter.get(s.section_id) == "body"]
        return {
            "sections": len(self.text.sections),
            "segments": len(self.text.segments),
            "body_segments": len(body),
            "body_chars": sum(len(s.text) for s in body),
            "notes": sum(1 for s in self.text.sections if s.kind == "note"),
            "page_breaks": len(self.text.page_breaks),
        }


def parse_epub(path: Path) -> ParsedEdition:
    warnings: list[str] = []
    with open_epub(path) as epub:
        toc, landmarks = read_toc(epub)
        docs = {}
        for item in epub.spine:
            if item.path in docs:
                continue
            try:
                docs[item.path] = epub.parse_xml(item.path)
            except EpubError as e:
                warnings.append(str(e))
        if not docs:
            raise EpubError("aucun document lisible dans le spine")
        marks = " ".join(" ".join(root.itertext())[:2000] for root in docs.values())
        dropped = [p for p, root in docs.items() if is_boilerplate(p, root, marks)]
        if dropped and len(dropped) < len(docs):
            for p in dropped:
                del docs[p]

        nav_docs = {epub.nav_path} if epub.nav_path in docs else set()
        notes = build_note_index(docs, skip_docs=nav_docs)
        blocks, note_blocks = extract_blocks(docs, notes)
        blocks = strip_gutenberg(blocks)
        if not blocks:
            raise EpubError("aucun texte extrait")

        note_labels = {
            n.key: " ".join("".join(n.calls[0].itertext()).split()).strip("[]()") for n in notes.ordered()
        }
        roots, method = build_structure(blocks, toc, landmarks, note_blocks, note_labels)
        if not toc:
            warnings.append("pas de table des matières : sections déduites des titres")
        elif method == "headings":
            warnings.append("table des matières inutilisable : sections déduites des titres")

        text = build_records(roots)
        if not any(text.matter.get(s.section_id) == "body" for s in text.segments):
            warnings.append("aucun segment classé dans le corps du texte")

        return ParsedEdition(
            path=path,
            sha256=epub.sha256,
            metadata=epub.metadata,
            structure_method=method,
            text=text,
            warnings=warnings,
        )
