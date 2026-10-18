"""Table des matières (EPUB 3 nav.xhtml, EPUB 2 toc.ncx) et repères (landmarks)."""

from __future__ import annotations

import contextlib
from dataclasses import dataclass, field

from lxml import etree

from swann_ingest.epub.container import Epub, EpubError, resolve_href

XHTML = "http://www.w3.org/1999/xhtml"
OPS = "http://www.idpf.org/2007/ops"
NCX = "http://www.daisy.org/z3986/2005/ncx/"


@dataclass
class TocEntry:
    label: str
    path: str
    fragment: str | None
    children: list[TocEntry] = field(default_factory=list)

    def walk(self):
        yield self
        for c in self.children:
            yield from c.walk()


@dataclass
class Landmark:
    type: str  # ex : "bodymatter", "toc", "cover", "copyright-page"
    path: str
    fragment: str | None


def _local(el: etree._Element) -> str:
    return etree.QName(el).localname if isinstance(el.tag, str) else ""


def _epub_types(el: etree._Element) -> set[str]:
    return set((el.get(f"{{{OPS}}}type") or "").split())


def _label(el: etree._Element) -> str:
    return " ".join("".join(el.itertext()).split())


def _parse_nav_list(ol: etree._Element, nav_path: str) -> list[TocEntry]:
    entries: list[TocEntry] = []
    for li in ol:
        if _local(li) != "li":
            continue
        a = next((c for c in li if _local(c) in ("a", "span")), None)
        sub = next((c for c in li if _local(c) == "ol"), None)
        children = _parse_nav_list(sub, nav_path) if sub is not None else []
        if a is None or _local(a) != "a" or not a.get("href"):
            # entrée sans lien : on remonte ses enfants
            entries.extend(children)
            continue
        path, frag = resolve_href(nav_path, a.get("href"))
        entries.append(TocEntry(_label(a), path, frag, children))
    return entries


def _parse_nav(epub: Epub) -> tuple[list[TocEntry], list[Landmark]]:
    root = epub.parse_xml(epub.nav_path)
    toc: list[TocEntry] = []
    landmarks: list[Landmark] = []
    for nav in root.iter(f"{{{XHTML}}}nav", "nav"):
        types = _epub_types(nav)
        ol = next((c for c in nav.iter() if _local(c) == "ol"), None)
        if ol is None:
            continue
        if "toc" in types and not toc:
            toc = _parse_nav_list(ol, epub.nav_path)
        elif "landmarks" in types:
            for a in ol.iter(f"{{{XHTML}}}a", "a"):
                if a.get("href"):
                    path, frag = resolve_href(epub.nav_path, a.get("href"))
                    for t in _epub_types(a) or {"unknown"}:
                        landmarks.append(Landmark(t.lower(), path, frag))
    return toc, landmarks


def _parse_ncx_points(parent: etree._Element, ncx_path: str) -> list[TocEntry]:
    entries = []
    for point in parent.iterfind(f"{{{NCX}}}navPoint"):
        label_el = point.find(f"{{{NCX}}}navLabel/{{{NCX}}}text")
        content = point.find(f"{{{NCX}}}content")
        children = _parse_ncx_points(point, ncx_path)
        if content is None or not content.get("src"):
            entries.extend(children)
            continue
        path, frag = resolve_href(ncx_path, content.get("src"))
        label = _label(label_el) if label_el is not None else ""
        entries.append(TocEntry(label, path, frag, children))
    return entries


def _parse_ncx(epub: Epub) -> list[TocEntry]:
    root = epub.parse_xml(epub.ncx_path)
    nav_map = root.find(f"{{{NCX}}}navMap")
    return _parse_ncx_points(nav_map, epub.ncx_path) if nav_map is not None else []


# Correspondance des types du <guide> EPUB 2 vers les landmarks EPUB 3.
GUIDE_TYPES = {
    "text": "bodymatter",
    "start": "bodymatter",
    "cover": "cover",
    "title-page": "titlepage",
    "toc": "toc",
    "copyright-page": "copyright-page",
    "dedication": "dedication",
    "preface": "preface",
    "foreword": "foreword",
    "acknowledgements": "acknowledgments",
    "colophon": "colophon",
    "notes": "endnotes",
    "bibliography": "bibliography",
    "index": "index",
    "glossary": "glossary",
}


def read_toc(epub: Epub) -> tuple[list[TocEntry], list[Landmark]]:
    toc: list[TocEntry] = []
    landmarks: list[Landmark] = []
    if epub.nav_path:
        with contextlib.suppress(EpubError):
            toc, landmarks = _parse_nav(epub)
    if not toc and epub.ncx_path:
        with contextlib.suppress(EpubError):
            toc = _parse_ncx(epub)
    if not landmarks:
        for gtype, target in epub.guide:
            path, _, frag = target.partition("#")
            landmarks.append(Landmark(GUIDE_TYPES.get(gtype, gtype), path, frag or None))
    return toc, landmarks
