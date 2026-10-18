"""Œuvres : rattacher chaque édition à une œuvre (Wikidata quand c'est
possible), regrouper les traductions, désigner l'original, écarter les
doublons."""

from __future__ import annotations

import csv
import io
from collections import defaultdict
from collections.abc import Callable
from dataclasses import dataclass, field
from difflib import SequenceMatcher

from swann_ingest.identify import ratio, title_key
from swann_ingest.prepare.sources import Draft, fold
from swann_ingest.prepare.wikidata import EDITION, CachedWikidata
from swann_ingest.wikidata import Entity, WikidataError, year_of

TITLE_MATCH = 0.92  # titre d'édition ≈ libellé d'une œuvre de l'auteur
TITLE_MARGIN = 0.04
SAME_TEXT = 0.6  # similarité des premiers paragraphes : même texte
LABEL_LANGUAGES = ("fr", "en", "ru", "pl", "es", "de", "lt", "uk", "it", "la", "pt", "cs", "he")
# Préférence entre deux fichiers du même texte (doublons).
SOURCE_RANK = {"wolnelektury": 0, "wikisource": 1, "beq": 2, "autre": 3, "gutenberg": 4, "textos": 5}


@dataclass
class Edition:
    draft: Draft
    record: dict  # fiche d'inventaire
    language: str
    author_keys: list[str]
    translator_keys: list[str]
    item: str | None = None  # élément Wikidata de la page / du livre
    work_qid: str | None = None
    how: str | None = None  # comment l'œuvre a été trouvée
    original: bool = False
    duplicate_of: Edition | None = None

    @property
    def body_chars(self) -> int:
        return self.record["text"].get("body_chars") or 0


@dataclass
class Work:
    editions: list[Edition]
    qid: str | None = None
    title: str = ""
    original_language: str | None = None
    year: int | None = None
    author_keys: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)


# ------------------------------------------------------- éléments directs
def link_items(wd: CachedWikidata, editions: list[Edition], log: Callable[[str], None]) -> None:
    """Wikisource : élément lié à la page ; Gutenberg : propriété P2034."""
    by_lang: dict[str, list[Edition]] = defaultdict(list)
    for ed in editions:
        if ed.draft.wikisource:
            by_lang[ed.draft.wikisource[0]].append(ed)
    for lang, eds in by_lang.items():
        items = wd.wikisource_items(lang, [e.draft.wikisource[1] for e in eds if e.draft.wikisource])
        for e in eds:
            assert e.draft.wikisource
            e.item = items.get(e.draft.wikisource[1])
        log(f"Wikisource ({lang}) : {sum(1 for e in eds if e.item)}/{len(eds)} pages liées à Wikidata")

    gut = [e for e in editions if e.draft.gutenberg_id]
    if gut:
        try:
            rows = wd.sparql("SELECT ?item ?id WHERE { ?item wdt:P2034 ?id }")
            ids = {r["id"]: r["item"].rsplit("/", 1)[-1] for r in rows}
        except WikidataError as e:
            log(f"[yellow]SPARQL Gutenberg indisponible : {e}")
            ids = {}
        for e in gut:
            e.item = ids.get(e.draft.gutenberg_id or "")
        log(f"Gutenberg : {sum(1 for e in gut if e.item)}/{len(gut)} livres liés à Wikidata")

    items = wd.entities([e.item for e in editions if e.item])
    for e in editions:
        entity = items.get(e.item) if e.item else None
        if entity is None:
            continue
        if entity.claims.get("P629"):
            e.work_qid, e.how = entity.first("P629"), "élément d'édition (P629)"
        elif EDITION not in entity.claims.get("P31", []):
            e.work_qid, e.how = entity.id, "élément de la page"


# --------------------------------------------- œuvres connues de l'auteur
def author_works(wd: CachedWikidata, author_qids: list[str]) -> dict[str, dict[str, set[str]]]:
    """QID d'auteur -> {QID d'œuvre -> libellés et alias}."""
    out: dict[str, dict[str, set[str]]] = defaultdict(lambda: defaultdict(set))
    langs = ", ".join(f'"{x}"' for x in LABEL_LANGUAGES)
    for i in range(0, len(author_qids), 10):
        values = " ".join(f"wd:{q}" for q in author_qids[i : i + 10])
        query = f"""
            SELECT ?a ?w ?label WHERE {{
              VALUES ?a {{ {values} }}
              ?w wdt:P50 ?a .
              FILTER NOT EXISTS {{ ?w wdt:P629 ?x }}
              ?w rdfs:label|skos:altLabel ?label .
              FILTER(LANG(?label) IN ({langs}))
            }}"""
        try:
            rows = wd.sparql(query)
        except WikidataError:
            continue
        for r in rows:
            out[r["a"].rsplit("/", 1)[-1]][r["w"].rsplit("/", 1)[-1]].add(r["label"])
    return out


def match_titles(
    editions: list[Edition], works_by_author: dict[str, dict[str, set[str]]], person_qid: dict[str, str]
) -> None:
    for e in editions:
        if e.work_qid or e.draft.volume:
            continue
        key = title_key(e.draft.title)
        if len(key) < 3:
            continue
        scores: dict[str, float] = {}
        for a in e.author_keys:
            for wq, labels in works_by_author.get(person_qid.get(a, ""), {}).items():
                scores[wq] = max([scores.get(wq, 0.0), *(ratio(key, title_key(lbl)) for lbl in labels)])
        ranked = sorted(scores.items(), key=lambda x: -x[1])
        if not ranked or ranked[0][1] < TITLE_MATCH:
            continue
        if len(ranked) > 1 and ranked[1][1] > ranked[0][1] - TITLE_MARGIN:
            continue
        e.work_qid, e.how = ranked[0][0], f"titre parmi les œuvres de l'auteur ({ranked[0][1]:.2f})"


# ------------------------------------------------------------ regroupement
def _same_text(a: Edition, b: Edition) -> bool:
    ha = " ".join(a.record["text"].get("head") or [])[:1500]
    hb = " ".join(b.record["text"].get("head") or [])[:1500]
    if not ha or not hb:
        return False
    return SequenceMatcher(None, ha, hb, autojunk=False).ratio() >= SAME_TEXT


def group(editions: list[Edition], identity: dict[str, str]) -> list[Work]:
    """Une œuvre par QID ; sans QID : même auteur et même titre (toutes
    langues), sauf deux textes différents de même langue et même titre
    (homonymes : « Письмо в редакцию »)."""
    works: list[Work] = []
    by_qid: dict[str, Work] = {}
    loose: dict[tuple, list[Work]] = defaultdict(list)
    for e in sorted(editions, key=lambda x: x.draft.file):
        if e.work_qid:
            w = by_qid.get(e.work_qid)
            if w is None:
                w = by_qid[e.work_qid] = Work([], qid=e.work_qid)
                works.append(w)
            w.editions.append(e)
            continue
        authors = tuple(sorted({identity.get(k, k) for k in e.author_keys})) or ("anonyme",)
        key = (authors, title_key(e.draft.title) or fold(e.draft.title), e.draft.volume)
        target = None
        for w in loose[key]:
            same_lang = [x for x in w.editions if x.language == e.language]
            if not same_lang:
                target = w
                break
            # Même langue : deux traductions différentes, ou le même texte deux fois.
            if any(
                (x.translator_keys and e.translator_keys and x.translator_keys != e.translator_keys)
                or _same_text(x, e)
                for x in same_lang
            ):
                target = w
                break
        if target is None:
            target = Work([])
            loose[key].append(target)
            works.append(target)
        target.editions.append(e)
    return works


# ------------------------------------------------------ par le contenu
CONTENT_MIN_CHARS = 40  # paragraphes plus courts ignorés (« * * * », numéros)
CONTENT_PARA = 0.70  # similarité LaBSE d'un paragraphe et de sa traduction
CONTENT_SHARE = 0.35  # part de paragraphes retrouvés : même texte
CONTENT_MARGIN = 0.15  # écart avec le 2ᵉ candidat
LENGTH_RATIO = (0.4, 2.5)  # une traduction n'a pas une longueur quelconque


def _paragraphs(e: Edition) -> list[str]:
    text = e.record["text"]
    out = []
    for p in [*(text.get("head") or []), *(text.get("sample") or [])]:
        if len(p) >= CONTENT_MIN_CHARS and p not in out:
            out.append(p)
    return out


def match_content(
    works: list[Work], identity: dict[str, str], encode: Callable, log: Callable[[str], None]
) -> list[tuple[Work, Work, float]]:
    """Réunit les traductions d'une même œuvre que ni Wikidata ni le titre
    n'ont reliées : même auteur, langues disjointes, longueurs compatibles,
    et assez de paragraphes dont la traduction se retrouve dans l'autre
    (LaBSE). Renvoie les fusions faites (pour le rapport)."""
    import numpy as np

    by_author: dict[str, list[Work]] = defaultdict(list)
    for w in works:
        ids = {identity.get(k, k) for e in w.editions for k in e.author_keys}
        if len(ids) == 1:
            by_author[next(iter(ids))].append(w)
    candidates = {
        a: ws
        for a, ws in by_author.items()
        if len(ws) > 1 and len({e.language for w in ws for e in w.editions}) > 1
    }
    reps = {id(w): max(w.editions, key=lambda x: x.body_chars) for ws in candidates.values() for w in ws}
    texts: dict[int, list[str]] = {k: _paragraphs(e) for k, e in reps.items()}
    flat = [(k, t) for k, ts in texts.items() for t in ts]
    log(f"Rapprochement par le contenu : {len(candidates)} auteurs, {len(reps)} œuvres, {len(flat)} extraits")
    if not flat:
        return []
    vectors = encode([t for _, t in flat])
    by_work: dict[int, list] = defaultdict(list)
    for (k, _), v in zip(flat, vectors, strict=True):
        by_work[k].append(v)
    mats = {k: np.stack(vs) for k, vs in by_work.items()}

    merged: list[tuple[Work, Work, float]] = []
    alive = {id(w) for ws in candidates.values() for w in ws}
    for ws in candidates.values():
        for w in sorted(ws, key=lambda x: -len(x.editions)):
            if id(w) not in alive or id(w) not in mats:
                continue
            langs = {e.language for e in w.editions}
            size = reps[id(w)].body_chars
            scores = []
            for other in ws:
                if other is w or id(other) not in alive or id(other) not in mats:
                    continue
                if langs & {e.language for e in other.editions} or (w.qid and other.qid):
                    continue
                ratio = reps[id(other)].body_chars / max(1, size)
                if not LENGTH_RATIO[0] <= ratio <= LENGTH_RATIO[1]:
                    continue
                a, b = mats[id(w)], mats[id(other)]
                small, big = (a, b) if len(a) <= len(b) else (b, a)
                share = float(((small @ big.T).max(axis=1) >= CONTENT_PARA).mean())
                scores.append((share, other))
            scores.sort(key=lambda x: -x[0])
            if not scores or scores[0][0] < CONTENT_SHARE:
                continue
            if len(scores) > 1 and scores[1][0] > scores[0][0] - CONTENT_MARGIN:
                continue
            share, other = scores[0]
            keep, drop = (w, other) if w.qid or not other.qid else (other, w)
            for e in drop.editions:
                e.how = f"contenu (LaBSE, {share:.0%} des extraits retrouvés)"
            keep.editions += drop.editions
            alive.discard(id(drop))
            merged.append((keep, drop, share))
    dropped = {id(d) for _, d, _ in merged}
    works[:] = [w for w in works if id(w) not in dropped]
    return merged


# ---------------------------------------------------- fiche de l'œuvre
def language_codes(wd: CachedWikidata, qids: set[str]) -> dict[str, str]:
    entities = wd.entities(sorted(qids))
    return {q: str(e.first("P218")) for q, e in entities.items() if e.first("P218")}


def finish(
    works: list[Work],
    work_entities: dict[str, Entity],
    native: dict[str, list[str]],
    lang_codes: dict[str, str],
    identity: dict[str, str],
) -> None:
    """Langue originale, édition originale, doublons, titre, année.
    native : clé de personne -> codes de ses langues (Wikidata) ;
    identity : clé de personne -> QID (ou la clé elle-même)."""
    for w in works:
        entity = work_entities.get(w.qid) if w.qid else None
        keys: list[str] = []
        seen: set[str] = set()
        # Graphie de l'auteur prise dans l'édition originale de préférence (la plus longue)
        for e in sorted(w.editions, key=lambda x: (bool(x.translator_keys), -x.body_chars)):
            for k in e.author_keys:
                if identity.get(k, k) not in seen:
                    seen.add(identity.get(k, k))
                    keys.append(k)
        w.author_keys = keys

        # Langue originale
        langs = {e.language.split("-")[0] for e in w.editions}
        untranslated = {e.language.split("-")[0] for e in w.editions if not e.translator_keys}
        original = None
        if entity is not None:
            for prop in ("P407", "P364"):
                for q in entity.claims.get(prop, []):
                    if q in lang_codes:
                        original = lang_codes[q]
                        break
                if original:
                    break
        author_langs = [c for k in keys for c in native.get(k, [])]
        if original is None and author_langs:
            known = [c for c in author_langs if c in untranslated]
            original = known[0] if known else author_langs[0]
            if not known and len(untranslated) == len(langs) and len(langs) == 1:
                # Aucune édition dans la langue de l'auteur et aucun traducteur
                # nommé : traduction anonyme (textos.info) ou auteur bilingue.
                w.notes.append(
                    f"langue de l'auteur ({', '.join(author_langs)}) absente des éditions : "
                    "traduction sans traducteur nommé ?"
                )
        if original is None and len(untranslated) == 1:
            original = next(iter(untranslated))
            w.notes.append("langue originale supposée (auteur sans langue connue)")
        w.original_language = original

        # Doublons : même langue et mêmes traducteurs
        kept: dict[tuple, Edition] = {}
        for e in sorted(w.editions, key=lambda x: (SOURCE_RANK.get(x.draft.source, 9), -x.body_chars)):
            key = (e.language, tuple(sorted(e.translator_keys)))
            if key in kept and (w.qid or _same_text(kept[key], e) or not e.translator_keys):
                e.duplicate_of = kept[key]
            else:
                kept.setdefault(key, e)
        live = [e for e in w.editions if e.duplicate_of is None]

        # Édition originale : dans la langue originale, sans traducteur
        candidates = [
            e for e in live if original and e.language.split("-")[0] == original and not e.translator_keys
        ]
        if candidates:
            max(candidates, key=lambda x: x.body_chars).original = True

        # Titre et année
        ref = next((e for e in live if e.original), None)
        title = None
        if entity is not None and original and original in entity.labels:
            title = entity.labels[original]
        if title is None and ref is not None:
            title = ref.draft.title
        if title is None and entity is not None:
            title = entity.label("fr", "en")
        w.title = title or max(live or w.editions, key=lambda x: x.body_chars).draft.title
        if entity is not None:
            w.year = year_of(entity.first("P577"))


def csv_rows(rows: list[dict]) -> str:
    buf = io.StringIO()
    if rows:
        writer = csv.DictWriter(buf, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)
    return buf.getvalue()
