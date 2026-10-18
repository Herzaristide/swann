"""Classement d'un EPUB déposé : à quelle œuvre appartient-il ?
(docs/console.md §4.2)

Indices, du moins coûteux au plus coûteux :
1. langue : dc:language, confirmée par la détection sur le texte ;
2. auteurs (dc:creator, rôle aut) comparés aux personnes en base et à leurs
   variantes de nom ; Wikidata donne les noms dans toutes les langues
   (Dostoïevski / Dostoevsky / Достоевский) ;
3. titre (dc:title) comparé aux titres des œuvres de ces auteurs, dans toutes
   les langues ; Wikidata relie un titre traduit à l'œuvre (QID) ;
4. contenu : LaBSE sur des paragraphes du corps, comparés à une édition déjà
   en base de l'œuvre candidate (une traduction du même texte est proche,
   une autre œuvre du même auteur ne l'est pas) ;
5. traducteurs (rôle trl) : repèrent un doublon (même œuvre, même langue,
   mêmes traducteurs).

Le rapport (JSON) liste les indices, les candidats et une proposition. Avec
IDENTIFY_AUTO=false (défaut), la proposition attend toujours la validation
d'un admin dans la console.
"""

from __future__ import annotations

import re
import unicodedata
import uuid
from dataclasses import asdict, dataclass, field
from difflib import SequenceMatcher

import numpy as np
import psycopg

from swann_ingest.parse import ParsedEdition
from swann_ingest.text.language import detect_majority
from swann_ingest.wikidata import Entity, Wikidata, WikidataError, year_of

AUTHOR_MIN = 0.80  # similarité minimale d'un nom pour retenir une personne
TITLE_MIN = 0.55
CONTENT_SAMPLE = 16  # paragraphes du dépôt comparés
CONTENT_WINDOW = 0.04  # fenêtre de recherche autour de la même position relative
CONTENT_MAX_ENCODED = 12_000  # paragraphes de l'édition existante encodés au plus
CONTENT_MATCH = 0.70  # similarité LaBSE d'un paragraphe et de sa traduction
CONTENT_SAME_WORK = 0.30  # part de paragraphes retrouvés : au-dessus, même texte
CONTENT_FULL = 0.50  # part de passages retrouvés qui vaut certitude dans le score
MARGIN = 0.10  # écart minimal avec le 2ᵉ candidat pour un rattachement auto

ARTICLES = {
    "le", "la", "les", "l", "un", "une", "des", "the", "a", "an", "der", "die", "das", "ein", "eine",
    "el", "los", "las", "il", "lo", "gli", "i", "o", "os", "as", "de", "du",
}  # fmt: skip
TRANSLATOR_ROLES = {"trl", "translator", "tr"}
AUTHOR_ROLES = {"aut", "author", "cre", None, ""}


# ------------------------------------------------------------------ texte
def normalize(text: str) -> str:
    text = unicodedata.normalize("NFKD", text)
    text = "".join(c for c in text if not unicodedata.combining(c)).lower()
    return re.sub(r"[^\w]+", " ", text).strip()


def title_key(title: str) -> str:
    """Titre comparable : sans sous-titre, ponctuation ni article initial."""
    main = re.split(r"\s[:;.—–-]\s|[:;]\s|\.\s", title, maxsplit=1)[0]
    words = normalize(main).split()
    while len(words) > 1 and words[0] in ARTICLES:
        words = words[1:]
    return " ".join(words)


def ratio(a: str, b: str) -> float:
    if not a or not b:
        return 0.0
    if a == b:
        return 1.0
    seq = SequenceMatcher(None, a, b).ratio()
    ta, tb = set(a.split()), set(b.split())
    tokens = len(ta & tb) / max(len(ta), len(tb))
    return max(seq, tokens)


def name_similarity(a: str, b: str) -> float:
    """« Dostoevsky, Fyodor » ≈ « Fyodor Dostoevsky » ≈ « F. M. Dostoïevski »."""
    na, nb = normalize(a), normalize(b)
    if not na or not nb:
        return 0.0
    full = ratio(na, nb)
    sorted_ = ratio(" ".join(sorted(na.split())), " ".join(sorted(nb.split())))
    # Nom de famille (le mot le plus long) : tolère prénoms abrégés ou absents.
    la, lb = max(na.split(), key=len), max(nb.split(), key=len)
    surname = SequenceMatcher(None, la, lb).ratio() * 0.9 if len(la) > 3 and len(lb) > 3 else 0
    return max(full, sorted_, surname)


def clean_person_name(name: str) -> str:
    """« Dostoevsky, Fyodor » → « Fyodor Dostoevsky » ; dates retirées."""
    name = re.sub(r"\(?\b\d{3,4}\s*[-–]\s*\d{0,4}\)?", "", name).strip(" ,;")
    if name.count(",") == 1:
        last, first = (p.strip() for p in name.split(","))
        if first and last and len(first.split()) <= 3:
            name = f"{first} {last}"
    return " ".join(name.split())


KEEP_REGION = {"pt", "zh"}  # variantes qui changent vraiment le texte (pt-BR, zh-Hant…)


def edition_language(code: str | None) -> str | None:
    """« en-US » → « en », « pt-BR » reste « pt-BR »."""
    if not code:
        return None
    base, _, region = code.partition("-")
    base = base.lower()
    return f"{base}-{region}" if region and base in KEEP_REGION else base


def base_language(code: str | None) -> str | None:
    return code.split("-")[0].lower() if code else None


# ------------------------------------------------------------------ rapport
@dataclass
class PersonMatch:
    name: str  # nom lu dans l'EPUB
    person_id: str | None = None
    display_name: str | None = None
    score: float = 0.0
    wikidata_id: str | None = None
    wikidata_label: str | None = None


@dataclass
class Candidate:
    work_id: str
    title: str
    slug: str | None
    original_language: str | None
    authors: list[str]
    title_score: float = 0.0
    author_score: float = 0.0
    content_score: float | None = None
    wikidata_match: bool = False
    score: float = 0.0
    editions: list[dict] = field(default_factory=list)  # éditions existantes (langue, traducteurs)
    same_language: list[str] = field(default_factory=list)  # éditions déjà dans cette langue
    duplicates: list[str] = field(default_factory=list)  # même langue et mêmes traducteurs (ou aucun connu)
    reasons: list[str] = field(default_factory=list)


@dataclass
class Report:
    metadata: dict
    language: str | None
    detected_language: str | None
    authors: list[PersonMatch]
    translators: list[str]
    title: str | None
    candidates: list[Candidate]
    wikidata: dict | None = None  # œuvre trouvée sur Wikidata (QID, titres, année, langue)
    wikidata_error: str | None = None
    warnings: list[str] = field(default_factory=list)
    decision: str = "review"  # attach | create_work | review
    confident: bool = False
    proposal: dict | None = None  # résolution proposée (même format que la validation)

    def to_json(self) -> dict:
        return asdict(self)


# ----------------------------------------------------------------- personnes
def _match_persons(conn: psycopg.Connection, names: set[str]) -> PersonMatch | None:
    """Meilleure personne en base pour un ensemble de noms (variantes)."""
    rows = conn.execute(
        """
        SELECT p.id, p.display_name, p.wikidata_id,
               array_remove(array_agg(DISTINCT pn.name), NULL) AS names
        FROM persons p LEFT JOIN person_names pn ON pn.person_id = p.id
        GROUP BY p.id
        """
    ).fetchall()
    best: PersonMatch | None = None
    for r in rows:
        known = {r["display_name"], *r["names"]}
        score = max(name_similarity(a, b) for a in names for b in known)
        if best is None or score > best.score:
            best = PersonMatch(
                name=next(iter(names)),
                person_id=str(r["id"]),
                display_name=r["display_name"],
                score=round(score, 3),
                wikidata_id=r["wikidata_id"],
            )
    return best


def _resolve_author(
    conn: psycopg.Connection, wd: Wikidata | None, name: str, language: str | None, report: Report
) -> tuple[PersonMatch, Entity | None]:
    name = clean_person_name(name)
    match = PersonMatch(name=name)
    entity = None
    if wd is not None and report.wikidata_error is None:
        try:
            humans = wd.humans(name, language or "en")
            entity = humans[0] if humans else None
        except WikidataError as e:
            report.wikidata_error = str(e)
    names = {name} | (entity.names() if entity else set())
    local = _match_persons(conn, names) if names else None
    if entity is not None:
        match.wikidata_id = entity.id
        match.wikidata_label = entity.label(language or "en")
        # Même QID en base : certain.
        row = conn.execute(
            "SELECT id, display_name FROM persons WHERE wikidata_id = %s", (entity.id,)
        ).fetchone()
        if row:
            match.person_id, match.display_name, match.score = str(row["id"]), row["display_name"], 1.0
            return match, entity
    if local and local.score >= AUTHOR_MIN:
        match.person_id, match.display_name, match.score = local.person_id, local.display_name, local.score
    return match, entity


# ------------------------------------------------------------------- œuvres
def _works_of(conn: psycopg.Connection, person_ids: list[str]) -> list[dict]:
    return conn.execute(
        """
        SELECT w.id, w.title, w.slug, w.original_language, w.wikidata_id, w.first_published_year,
               array_remove(array_agg(DISTINCT t.title), NULL) AS titles,
               array(SELECT p.display_name FROM work_authors wa2 JOIN persons p ON p.id = wa2.person_id
                     WHERE wa2.work_id = w.id ORDER BY wa2.position) AS authors
        FROM works w
        JOIN work_authors wa ON wa.work_id = w.id
        LEFT JOIN work_titles t ON t.work_id = w.id
        WHERE wa.person_id = ANY(%s::uuid[]) AND w.deleted_at IS NULL
        GROUP BY w.id
        """,
        (person_ids,),
    ).fetchall()


def _work_editions(conn: psycopg.Connection, work_id) -> list[dict]:
    return conn.execute(
        """
        SELECT e.id, e.language, e.title, e.is_original,
               array(SELECT p.display_name FROM edition_contributors c JOIN persons p ON p.id = c.person_id
                     WHERE c.edition_id = e.id AND c.role = 'translator' ORDER BY c.position) AS translators,
               (SELECT count(*) FROM segments s WHERE s.edition_id = e.id) AS n_segments
        FROM editions e WHERE e.work_id = %s AND e.deleted_at IS NULL
        ORDER BY e.is_original DESC, e.language
        """,
        (work_id,),
    ).fetchall()


# ------------------------------------------------------------------ contenu
def body_paragraphs(parsed: ParsedEdition) -> list[str]:
    return [
        s.text
        for s in parsed.text.segments
        if s.kind == "paragraph" and parsed.text.matter.get(s.section_id) == "body"
    ]


def db_body_paragraphs(conn: psycopg.Connection, edition_id) -> list[str]:
    return [
        r["text"]
        for r in conn.execute(
            """
            SELECT s.text FROM segments s JOIN sections sec ON sec.id = s.section_id
            WHERE s.edition_id = %s AND sec.matter = 'body' AND s.kind = 'paragraph'
            ORDER BY s.seq
            """,
            (edition_id,),
        ).fetchall()
    ]


def content_similarity(encoder, new: list[str], existing: list[str]) -> float | None:
    """Part des paragraphes échantillonnés du dépôt dont la traduction se
    trouve dans l'édition existante, au même endroit du livre (± CONTENT_WINDOW).
    Même texte traduit : ~0,9 ; autre œuvre du même auteur : ~0."""
    new = [t for t in new if len(t) >= 80]
    if len(new) < 5 or len(existing) < 5:
        return None
    n_samples = min(CONTENT_SAMPLE, len(new))
    positions = np.linspace(0.05, 0.95, n_samples)
    picks = [new[min(int(p * len(new)), len(new) - 1)] for p in positions]
    half = max(int(CONTENT_WINDOW * len(existing)), 15)
    # Plafond du nombre de paragraphes encodés (romans très longs).
    half = min(half, max(15, CONTENT_MAX_ENCODED // (2 * n_samples)))
    windows = [
        range(max(0, int(p * len(existing)) - half), min(len(existing), int(p * len(existing)) + half + 1))
        for p in positions
    ]
    needed = sorted({j for w in windows for j in w})
    index = {j: k for k, j in enumerate(needed)}
    va = encoder.encode(picks)
    vb = encoder.encode([existing[j] for j in needed])
    best = np.array([max(float(va[i] @ vb[index[j]]) for j in w) for i, w in enumerate(windows)])
    return round(float(np.mean(best >= CONTENT_MATCH)), 3)


# ------------------------------------------------------------------ classement
def identify(
    conn: psycopg.Connection,
    parsed: ParsedEdition,
    *,
    wikidata: Wikidata | None,
    encoder=None,
    hints: dict | None = None,
    threshold: float = 0.85,
) -> Report:
    hints = hints or {}
    md = parsed.metadata
    new_paragraphs = body_paragraphs(parsed)
    detected = detect_majority(new_paragraphs)
    declared = next((lang for lang in md.languages if re.fullmatch(r"[A-Za-z]{2,3}(-\w+)*", lang)), None)
    language = hints.get("language") or edition_language(declared) or detected
    authors_raw = [n for n, role in md.creators if (role or "").lower() in AUTHOR_ROLES]
    translators = [
        clean_person_name(n) for n, role in md.creators if (role or "").lower() in TRANSLATOR_ROLES
    ]
    title = hints.get("title") or (md.titles[0] if md.titles else None)
    report = Report(
        metadata={
            "titles": md.titles,
            "languages": md.languages,
            "creators": [{"name": n, "role": r} for n, r in md.creators],
            "publisher": md.publisher,
            "date": md.date,
        },
        language=language,
        detected_language=detected,
        authors=[],
        translators=hints.get("translators") or translators,
        title=title,
        candidates=[],
    )
    if detected and language and base_language(language) != detected:
        report.warnings.append(f"langue déclarée « {language} », texte détecté en « {detected} »")
    if not title:
        report.warnings.append("aucun titre dans les métadonnées (dc:title)")
    if not authors_raw:
        report.warnings.append("aucun auteur dans les métadonnées (dc:creator)")

    # 1. Auteurs
    entities: list[Entity] = []
    for name in authors_raw[:4]:
        match, entity = _resolve_author(conn, wikidata, name, language, report)
        report.authors.append(match)
        if entity is not None:
            entities.append(entity)
    person_ids = [a.person_id for a in report.authors if a.person_id]

    # 2. Œuvre sur Wikidata (titre traduit → QID, titres dans toutes les langues)
    wd_work = None
    if wikidata is not None and title and report.wikidata_error is None:
        try:
            hits = wikidata.find_works(title, language or "en", {e.id for e in entities} or None)
            key = title_key(title)
            scored = sorted(
                ((max(ratio(key, title_key(lbl)) for lbl in h.labels), h) for h in hits),
                key=lambda x: -x[0],
            )
            if scored and scored[0][0] >= TITLE_MIN:
                h = scored[0][1]
                wd_work = h
                report.wikidata = {
                    "id": h.id,
                    "labels": sorted(h.labels)[:40],
                    "year": h.year,
                    "original_language": h.language,
                    "authors": sorted(h.authors),
                }
        except WikidataError as e:
            report.wikidata_error = str(e)

    # 3. Œuvres candidates en base
    works = _works_of(conn, person_ids) if person_ids else []
    if wd_work is not None:
        extra = conn.execute(
            "SELECT id FROM works WHERE wikidata_id = %s AND deleted_at IS NULL", (wd_work.id,)
        ).fetchall()
        known = {w["id"] for w in works}
        for r in extra:
            if r["id"] not in known:
                works += conn.execute(
                    """
                    SELECT w.id, w.title, w.slug, w.original_language, w.wikidata_id, w.first_published_year,
                           array_remove(array_agg(DISTINCT t.title), NULL) AS titles,
                           array(SELECT p.display_name FROM work_authors wa JOIN persons p
                                 ON p.id = wa.person_id WHERE wa.work_id = w.id
                                 ORDER BY wa.position) AS authors
                    FROM works w LEFT JOIN work_titles t ON t.work_id = w.id WHERE w.id = %s GROUP BY w.id
                    """,
                    (r["id"],),
                ).fetchall()
    author_score = max((a.score for a in report.authors), default=0.0)
    key = title_key(title) if title else ""
    wd_titles = {title_key(lbl) for lbl in wd_work.labels} if wd_work else set()
    for w in works:
        titles = {w["title"], *w["titles"]}
        t_score = max((ratio(key, title_key(t)) for t in titles), default=0.0) if key else 0.0
        if wd_titles:
            t_score = max(
                t_score, max((ratio(k, title_key(t)) for k in wd_titles for t in titles), default=0)
            )
        c = Candidate(
            work_id=str(w["id"]),
            title=w["title"],
            slug=w["slug"],
            original_language=w["original_language"],
            authors=w["authors"],
            title_score=round(t_score, 3),
            author_score=author_score,
            wikidata_match=bool(wd_work and w["wikidata_id"] == wd_work.id),
        )
        if c.wikidata_match:
            c.title_score = 1.0
            c.reasons.append(f"même œuvre Wikidata ({wd_work.id})")  # type: ignore[union-attr]
        editions = _work_editions(conn, w["id"])
        c.editions = [
            {
                "id": str(e["id"]),
                "language": e["language"],
                "title": e["title"],
                "is_original": e["is_original"],
                "translators": e["translators"],
            }
            for e in editions
        ]
        same_lang = [e for e in editions if base_language(e["language"]) == base_language(language)]
        new_tr = {normalize(t) for t in report.translators}
        for e in same_lang:
            c.same_language.append(str(e["id"]))
            old_tr = {normalize(t) for t in e["translators"]}
            if old_tr == new_tr or not new_tr or not old_tr:
                c.duplicates.append(str(e["id"]))
        c.score = round(0.5 * c.author_score + 0.5 * c.title_score, 3)
        report.candidates.append(c)

    report.candidates.sort(key=lambda c: -c.score)
    # 4. Contenu : seulement pour les candidats plausibles (coûteux)
    if encoder is not None:
        for c in report.candidates[:3]:
            if c.title_score < TITLE_MIN and not c.wikidata_match:
                continue
            ref = next((e for e in c.editions if e["is_original"]), c.editions[0] if c.editions else None)
            if ref is None:
                continue
            c.content_score = content_similarity(encoder, new_paragraphs, db_body_paragraphs(conn, ref["id"]))
            if c.content_score is not None:
                content = min(1.0, c.content_score / CONTENT_FULL)
                c.score = round(0.3 * c.author_score + 0.3 * c.title_score + 0.4 * content, 3)
                if c.content_score >= CONTENT_SAME_WORK:
                    c.reasons.append(
                        f"{c.content_score:.0%} des passages retrouvés dans l'édition {ref['language']}"
                    )
                else:
                    c.reasons.append(
                        f"contenu différent de l'édition {ref['language']} "
                        f"({c.content_score:.0%} des passages retrouvés)"
                    )
        report.candidates.sort(key=lambda c: -c.score)
    for c in report.candidates:
        if c.author_score >= AUTHOR_MIN:
            c.reasons.insert(0, f"auteur reconnu ({c.author_score:.2f})")
        if c.title_score >= TITLE_MIN and not c.wikidata_match:
            c.reasons.append(f"titre proche ({c.title_score:.2f})")

    _decide(report, threshold, entities, wd_work)
    return report


def _edition_fields(report: Report, hints: dict, original_language: str | None) -> dict:
    language = report.language or "und"
    return {
        "title": report.title,
        "language": language,
        "is_original": bool(original_language)
        and base_language(language) == base_language(original_language),
        "translators": report.translators,
        "publisher": hints.get("publisher") or report.metadata.get("publisher"),
        "year": hints.get("year") or year_of_date(report.metadata.get("date")),
        "access": hints.get("access", "restricted"),
    }


def year_of_date(date: str | None) -> int | None:
    m = re.match(r"^(\d{4})", date or "")
    return int(m.group(1)) if m else None


def _decide(report: Report, threshold: float, entities: list[Entity], wd_work) -> None:
    hints: dict = {}
    best = report.candidates[0] if report.candidates else None
    second = report.candidates[1].score if len(report.candidates) > 1 else 0.0
    if best is not None and best.score >= 0.6 and (best.title_score >= TITLE_MIN or best.wikidata_match):
        report.decision = "attach"
        report.confident = (
            best.score >= threshold
            and best.score - second >= MARGIN
            and not best.duplicates
            and (best.content_score is None or best.content_score >= CONTENT_SAME_WORK)
        )
        report.proposal = {
            "action": "attach",
            "work_id": best.work_id,
            "replace_edition_id": None,
            "edition": _edition_fields(report, hints, best.original_language),
        }
        if best.duplicates:
            report.warnings.append(
                "une édition de cette œuvre existe déjà dans cette langue avec les mêmes traducteurs "
                "(nouvelle édition ou remplacement du texte ?)"
            )
        return
    # Nouvelle œuvre
    report.decision = "create_work"
    original_language = wd_work.language if wd_work else None
    titles = {}
    if wd_work is not None:
        report.warnings.append(f"œuvre trouvée sur Wikidata ({wd_work.id}) mais absente de la base")
    if report.title and report.language:
        titles[base_language(report.language)] = report.title
    work_title = report.title or "Sans titre"
    authors = []
    for a in report.authors:
        if a.person_id:
            authors.append({"person_id": a.person_id})
        else:
            entity = next((e for e in entities if e.id == a.wikidata_id), None)
            authors.append(
                {
                    "name": a.wikidata_label or a.name,
                    "wikidata_id": a.wikidata_id,
                    "birth_year": year_of(entity.first("P569")) if entity else None,
                    "death_year": year_of(entity.first("P570")) if entity else None,
                }
            )
    report.confident = bool(authors) and bool(report.title) and not report.candidates
    report.proposal = {
        "action": "create_work",
        "work": {
            "title": work_title,
            "original_language": original_language,
            "first_published_year": wd_work.year if wd_work else None,
            "wikidata_id": wd_work.id if wd_work else None,
            "titles": titles,
            "authors": authors,
        },
        "edition": _edition_fields(report, hints, original_language),
    }


def new_slug(conn: psycopg.Connection, author: str | None, title: str) -> str:
    """« auteur/titre » translittéré, unique."""

    def part(text: str) -> str:
        text = unicodedata.normalize("NFKD", text)
        text = "".join(c for c in text if not unicodedata.combining(c)).lower()
        text = _translit(text)
        return re.sub(r"[^a-z0-9]+", "-", text).strip("-")[:60] or uuid.uuid4().hex[:8]

    base = f"{part(author or 'anonyme')}/{part(title)}"
    slug, i = base, 2
    while conn.execute("SELECT 1 FROM works WHERE slug = %s", (slug,)).fetchone():
        slug, i = f"{base}-{i}", i + 1
    return slug


CYRILLIC = dict(
    zip(
        "абвгдеёжзийклмнопрстуфхцчшщъыьэюя",
        # ъ et ь ne s'écrivent pas
        ["a", "b", "v", "g", "d", "e", "e", "zh", "z", "i", "i", "k", "l", "m", "n", "o", "p", "r", "s"]
        + ["t", "u", "f", "kh", "ts", "ch", "sh", "shch", "", "y", "", "e", "iu", "ia"],
        strict=True,
    )
)


def _translit(text: str) -> str:
    return "".join(CYRILLIC.get(c, c) for c in text)
