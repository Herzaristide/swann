"""Détection des notes (de bas de page / de fin) sur l'ensemble de l'EPUB.

Un lien est un appel de note quand :
  - il porte epub:type="noteref" / role="doc-noteref", ou sa cible porte
    epub:type footnote/endnote/note / role doc-footnote... (balisage explicite) ;
  - sinon (Gutenberg, Calibre...) : son texte est court ("1", "[12]", "*"), la
    cible vient APRÈS lui dans l'ordre de lecture et contient un lien retour
    vers lui. La contrainte d'ordre évite de prendre le lien retour de la note
    pour un appel (les deux liens pointent l'un vers l'autre).

Le conteneur de la note (retiré du fil du texte) est le plus haut ancêtre "qui
a l'air d'une note" sans contenir d'autre note ; à défaut, le bloc le plus
proche de la cible. Un conteneur implicite trop long est refusé (garde-fou
contre un chapitre entier pris pour une note).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from lxml import etree

from swann_ingest.epub.container import resolve_href

OPS = "http://www.idpf.org/2007/ops"
NOTE_TYPES = {"footnote", "endnote", "rearnote", "note"}
NOTE_ROLES = {"doc-footnote", "doc-endnote"}
NOTE_CLASS_RE = re.compile(r"footnote|endnote|\bnotes?\b|\bfn\b", re.I)
CONTAINER_TAGS = {"aside", "li", "div", "section", "p", "dd", "blockquote"}
MAX_CALL_LABEL = 8
MAX_IMPLICIT_NOTE_CHARS = 4000

ORIGIN_PATTERNS = [
    (
        "translator",
        re.compile(
            r"N\.\s?d\.\s?T\.|note du traducteur|translator'?s note|\(T\.\s?N\.\)|"
            r"прим\.\s*пер|примечание переводчика|anm\.\s*d\.\s*übers|"
            r"nota del traductor|nota del traduttore|\(N\.\s?T\.\)",
            re.I,
        ),
    ),
    (
        "editor",
        re.compile(
            r"N\.\s?d\.\s?[ÉE]\.|note de l['’][ée]diteur|editor'?s note|прим\.\s*ред|"
            r"anm\.\s*d\.\s*hrsg|nota del editor|nota dell['’]editore",
            re.I,
        ),
    ),
]


def local(el) -> str:
    return etree.QName(el).localname if isinstance(el.tag, str) else ""


def epub_types(el) -> set[str]:
    return set((el.get(f"{{{OPS}}}type") or "").split())


def _explicit_note(el) -> bool:
    return bool(epub_types(el) & NOTE_TYPES) or el.get("role") in NOTE_ROLES


def _noteish(el) -> bool:
    return _explicit_note(el) or bool(NOTE_CLASS_RE.search(el.get("class") or ""))


def _text_len(el) -> int:
    return len("".join(el.itertext()))


def note_origin(text: str) -> str:
    for origin, pattern in ORIGIN_PATTERNS:
        if pattern.search(text):
            return origin
    return "unknown"


@dataclass
class Note:
    key: str  # "chemin#id" de la cible
    container: etree._Element
    order: tuple[int, int]  # position de la cible dans l'ordre de lecture
    calls: list[etree._Element] = field(default_factory=list)


@dataclass
class NoteIndex:
    notes: dict[str, Note] = field(default_factory=dict)  # par clé de cible
    # Les éléments lxml servent de clés : les garder référencés garantit que
    # lxml renvoie toujours le même objet Python pour un même nœud.
    call_targets: dict = field(default_factory=dict)  # <a> d'appel -> clé
    containers: set = field(default_factory=set)  # conteneurs de notes
    backlinks: set = field(default_factory=set)  # liens retour

    def ordered(self) -> list[Note]:
        return sorted(self.notes.values(), key=lambda n: n.order)


def _link_key(base: str, href: str) -> str | None:
    path, frag = resolve_href(base, href)
    return f"{path}#{frag}" if frag else None


def _call_ids(doc: str, a) -> set[str]:
    """Clés qui désignent l'appel : id du <a>, de son parent (sup, span) ou
    d'une ancre vide juste avant (Gutenberg : <a id="FNanchor_1"/><a href=...>)."""
    ids = {a.get("id")}
    parent = a.getparent()
    if parent is not None and local(parent) in ("sup", "span", "small"):
        ids.add(parent.get("id"))
    prev = a.getprevious()
    if prev is not None and local(prev) == "a" and not prev.get("href"):
        ids.add(prev.get("id") or prev.get("name"))
    return {f"{doc}#{i}" for i in ids if i}


def _find_backlink(target, target_doc: str, call_keys: set[str]):
    scope = target
    for _ in range(3):
        for x in scope.iter():
            if local(x) == "a" and x.get("href") and _link_key(target_doc, x.get("href")) in call_keys:
                return x
        scope = scope.getparent()
        if scope is None or local(scope) == "body":
            break
    return None


def _effective_target(target):
    """Ancre seule dans un bloc vide (<p><a id="note-1"/></p>) : la note est
    le bloc suivant (ancien format Gutenberg)."""
    block = target
    while block is not None and local(block) not in CONTAINER_TAGS:
        block = block.getparent()
    if block is None or "".join(block.itertext()).strip():
        return target
    nxt = block.getnext()
    while nxt is not None and not isinstance(nxt.tag, str):
        nxt = nxt.getnext()
    return nxt if nxt is not None else target


def build_note_index(docs: dict[str, etree._Element], skip_docs: set[str]) -> NoteIndex:
    """docs : documents du spine, dans l'ordre de lecture."""
    by_id: dict[str, etree._Element] = {}
    order: dict = {}  # élément -> (index dans le spine, position)
    links: list[tuple[str, etree._Element, str]] = []
    for spine_idx, (path, root) in enumerate(docs.items()):
        for i, el in enumerate(root.iter()):
            if not isinstance(el.tag, str):
                continue
            order[el] = (spine_idx, i)
            if el.get("id"):
                by_id[f"{path}#{el.get('id')}"] = el
            if local(el) == "a" and el.get("href") and path not in skip_docs:
                key = _link_key(path, el.get("href"))
                if key and key.split("#")[0] in docs:
                    links.append((path, el, key))

    index = NoteIndex()
    for doc, a, key in links:
        target = by_id.get(key)
        if target is None:
            continue
        target = _effective_target(target)
        explicit = "noteref" in epub_types(a) or a.get("role") == "doc-noteref" or _explicit_note(target)
        back = _find_backlink(target, key.split("#")[0], _call_ids(doc, a))
        if not explicit:
            label = " ".join("".join(a.itertext()).split())
            if back is None or not label or len(label) > MAX_CALL_LABEL or order[target] <= order[a]:
                continue
        if back is not None:
            index.backlinks.add(back)
        index.call_targets[a] = key
        if key in index.notes:
            index.notes[key].calls.append(a)
        else:
            index.notes[key] = Note(key, target, order[target], [a])

    targets = {n.container for n in index.notes.values()}
    for note in list(index.notes.values()):
        container = _pick_container(note.container, targets)
        explicit = any(_explicit_note(el) for el in (note.container, container))
        if not explicit and _text_len(container) > MAX_IMPLICIT_NOTE_CHARS:
            # Probablement pas une note : on annule.
            del index.notes[note.key]
            for a in note.calls:
                index.call_targets.pop(a, None)
            continue
        note.container = container
    index.containers = {n.container for n in index.notes.values()}
    return index


def _pick_container(target, targets: set):
    chain = [target]
    parent = target.getparent()
    while parent is not None and local(parent) not in ("body", "html"):
        chain.append(parent)
        parent = parent.getparent()

    best = None
    for el in chain:
        if any(x in targets and x is not target for x in el.iter()):
            break
        if _noteish(el) and local(el) in CONTAINER_TAGS:
            best = el
    if best is not None:
        return best
    return next((el for el in chain if local(el) in CONTAINER_TAGS), target)
