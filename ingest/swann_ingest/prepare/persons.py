"""Personnes : un nom lu dans un EPUB -> une personne Wikidata, et un nom
d'usage unique (même graphie dans toutes les fiches, exigé par
`swann extract` qui reconnaît les personnes à leur nom exact)."""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass, field

from swann_ingest.identify import name_similarity
from swann_ingest.prepare.sources import Name, fold
from swann_ingest.prepare.wikidata import PERSONLIKE, CachedWikidata, notability
from swann_ingest.wikidata import Entity, WikidataError, year_of

NAME_MIN = 0.85
# Métiers de l'écrit : départagent les homonymes sans dates.
WRITERS = {
    "Q36180", "Q49757", "Q6625963", "Q333634", "Q4853732", "Q482980", "Q11774202", "Q214917",
    "Q1930187", "Q201788", "Q4964182", "Q1234713", "Q18814623", "Q15949613", "Q28389", "Q12144794",
    "Q1350157", "Q14467526", "Q3579035", "Q18844224", "Q2526255", "Q15980158", "Q1607826",
}  # fmt: skip
# Langue du nom d'usage : français d'abord (interface de Swann), puis anglais.
DISPLAY_LANGUAGES = ("fr", "en")
NAME_LANGUAGES = ("fr", "en", "ru", "pl", "es", "de", "it", "uk", "lt", "la", "mul")


@dataclass
class Person:
    key: str  # fold(nom lu)
    names: set[str] = field(default_factory=set)  # graphies lues dans les EPUB
    languages: set[str] = field(default_factory=set)  # langues des éditions où il apparaît
    birth: int | None = None
    death: int | None = None
    count: int = 0
    qid: str | None = None
    display: str = ""
    sort_name: str | None = None
    variants: dict[str, set[str]] = field(default_factory=dict)  # langue -> noms
    native_languages: list[str] = field(default_factory=list)  # QID (P1412, P103)
    note: str | None = None  # pourquoi non rattaché / ambigu
    confidence: float = 0.0
    candidates: list[str] = field(default_factory=list)  # homonymes à départager


def collect(names: list[tuple[Name, str | None]]) -> dict[str, Person]:
    """Regroupe les graphies identiques (sans casse ni accents)."""
    persons: dict[str, Person] = {}
    for n, lang in names:
        key = fold(n.name)
        if not key:
            continue
        p = persons.setdefault(key, Person(key))
        p.names.add(n.name)
        if lang:
            p.languages.add(lang.split("-")[0])
        p.birth = p.birth or n.birth
        p.death = p.death or n.death
        p.count += 1
    return persons


def _years(e: Entity) -> tuple[int | None, int | None]:
    return year_of(e.first("P569")), year_of(e.first("P570"))


def similarity(a: str, b: str) -> float:
    """name_similarity, plus le dernier mot comme nom de famille : en russe le
    mot le plus long est souvent le patronyme (« Александрович »)."""
    sim = name_similarity(a, b)
    ta, tb = a.replace(".", " ").split(), b.replace(".", " ").replace(",", " ").split()
    if not ta or not tb:
        return sim
    la = fold(ta[-1])
    # « Майков, Аполлон Николаевич » : le nom de famille vient en tête
    lb = {fold(tb[-1]), fold(b.split(",")[0])} if "," in b else {fold(tb[-1])}
    if len(la) > 3 and la in lb:
        sim = max(sim, 0.9)
    return sim


def personlike(e: Entity) -> bool:
    """Humain, groupe (frères Grimm) ou entité qui exerce un métier de l'écrit."""
    return bool(set(e.claims.get("P31", [])) & PERSONLIKE or set(e.claims.get("P106", [])) & WRITERS)


def _score(p: Person, e: Entity) -> tuple[float, bool] | None:
    """(score, dates concordantes) ; None si incompatible."""
    names = e.names()
    sim = max((similarity(n, c) for n in p.names for c in names), default=0.0)
    if sim < NAME_MIN or not any(initials_match(n, c) for n in p.names for c in names):
        return None
    birth, death = _years(e)
    dated = False
    for mine, theirs in ((p.birth, birth), (p.death, death)):
        if mine and theirs:
            if abs(mine - theirs) > 1:
                return None
            dated = True
    writer = bool(set(e.claims.get("P106", [])) & WRITERS)
    score = sim + (0.5 if dated else 0) + (0.2 if writer else 0) + 0.04 * notability(e)
    return score, dated


def initials_match(read: str, candidate: str) -> bool:
    """« M. A. Kuzmin » ≈ « Mikhail Kuzmin », pas « J. A. Kuzmin ». Un nom
    sans initiale s'accorde avec tout (la similarité suffit)."""
    initials = [t[0].casefold() for t in read.replace(".", ". ").split() if t.endswith(".") and len(t) <= 3]
    if not initials:
        return True
    tokens = candidate.replace(".", " ").split()
    if len(tokens) < 2:
        return False
    surname = max(read.replace(".", " ").split(), key=len)
    given = [t for t in tokens if fold(t) != fold(surname)]
    if len(given) == len(tokens):
        return False  # nom de famille absent : autre écriture, rien à comparer
    letters = [t[0].casefold() for t in given]
    n = min(len(initials), len(letters))
    return n > 0 and letters[:n] == initials[:n]


def _decide(p: Person, entities: list[Entity]) -> tuple[Entity | None, float, str]:
    """(personne retenue, score, sinon pourquoi rien)."""
    scored = [(s, dated, e) for e in entities if (r := _score(p, e)) for s, dated in [r]]
    scored.sort(key=lambda x: -x[0])
    if not scored:
        return None, 0.0, "aucune personne Wikidata correspondante"
    best_score, dated, best = scored[0]
    second = scored[1][0] if len(scored) > 1 else 0.0
    writer = bool(set(best.claims.get("P106", [])) & WRITERS)
    if not (dated or writer or len(scored) == 1) or (best_score - second < 0.1 and not dated):
        return (
            None,
            0.0,
            "homonymes : " + ", ".join(f"{e.id} {e.label('fr', 'en')}" for _, _, e in scored[:4]),
        )
    return best, best_score, ""


def _candidates(note: str) -> list[str]:
    return re.findall(r"\b(Q\d+)\b", note) if note.startswith("homonymes") else []


def _searches(wd: CachedWikidata, p: Person, name: str):
    """Recherches de la moins chère à la plus coûteuse ; on s'arrête dès
    qu'une personne est retenue."""
    langs = sorted(p.languages) or ["en"]
    yield lambda: wd.search(name, langs[0])
    for lang in [*langs[1:], "en"]:
        if lang != langs[0]:
            yield lambda lang=lang: wd.search(name, lang)
    initials = re.search(r"(^|\s)\w\.", name)
    surname = max(name.split(), key=len)
    if initials:
        # « П. Н. Краснов » : la recherche ne trouve pas les initiales, le nom seul si.
        yield lambda: wd.search(surname, langs[0])
    if initials or p.birth or p.death:
        # Plein texte parmi les humains : un nom courant noyé sous les homonymes
        yield lambda: wd.search_humans(surname if initials else name)


def resolve(
    wd: CachedWikidata,
    persons: dict[str, Person],
    progress: Callable[[int, int, str], None] | None = None,
) -> None:
    for i, p in enumerate(sorted(persons.values(), key=lambda x: -x.count)):
        if progress:
            progress(i, len(persons), next(iter(p.names)))
        name = max(p.names, key=len)
        seen: set[str] = set()
        entities: list[Entity] = []
        best, score, note = None, 0.0, "aucune personne Wikidata correspondante"
        try:
            for search in _searches(wd, p, name):
                new = [q for q in search() if q not in seen]
                if not new:
                    continue
                seen.update(new)
                entities += [e for e in wd.entities(new).values() if personlike(e)]
                best, score, note = _decide(p, entities)
                if best is not None:
                    break
        except WikidataError as e:
            p.note = f"Wikidata injoignable : {e}"
            continue
        if best is None:
            p.note = note
            p.candidates = _candidates(note)
            continue
        p.qid = best.id
        p.confidence = round(min(1.0, score / 1.5), 2)
        _apply(p, best)


def disambiguate(
    wd: CachedWikidata,
    persons: dict[str, Person],
    titles: dict[str, list[tuple[int, str]]],
    works_of: Callable[[list[str]], dict[str, dict[str, set[str]]]],
) -> dict[tuple[int, str], str]:
    """Homonymes (Dumas père et fils, Féval père et fils) : chaque édition va
    au candidat dont une œuvre Wikidata porte son titre ; les éditions sans
    titre reconnu vont au candidat largement majoritaire (≥ 3 titres et 80 %).
    titles : clé de personne -> [(id(édition), titre)].
    Renvoie (id(édition), clé lue) -> clé de la personne créée pour le candidat."""
    from swann_ingest.identify import ratio, title_key

    ambiguous = [p for p in persons.values() if p.candidates and titles.get(p.key)]
    qids = sorted({q for p in ambiguous for q in p.candidates})
    works = works_of(qids) if qids else {}
    entities = wd.entities(qids) if qids else {}
    out: dict[tuple[int, str], str] = {}
    for p in ambiguous:
        chosen: dict[int, str] = {}
        for ed, title in titles[p.key]:
            key = title_key(title)
            scores = [
                (max((ratio(key, title_key(lbl)) for lbl in labels), default=0.0), q)
                for q in p.candidates
                for labels in works.get(q, {}).values()
            ]
            best = max(scores, default=(0.0, ""))
            if best[0] >= 0.9:
                chosen[ed] = best[1]
        counts = {q: list(chosen.values()).count(q) for q in p.candidates}
        top = max(counts, key=lambda q: counts[q])
        dominant = counts[top] >= 3 and counts[top] >= 0.8 * len(chosen)
        moved = 0
        for ed, _ in titles[p.key]:
            q = chosen.get(ed) or (top if dominant else None)
            if q is None or q not in entities:
                continue
            key = f"{p.key}#{q}"
            if key not in persons:
                twin = Person(key, names=set(p.names), languages=set(p.languages), qid=q, confidence=0.8)
                twin.note = "homonyme départagé par les titres de ses œuvres"
                _apply(twin, entities[q])
                persons[key] = twin
            persons[key].count += 1
            out[(ed, p.key)] = key
            moved += 1
        p.count -= moved
    return out


def _apply(p: Person, e: Entity) -> None:
    birth, death = _years(e)
    p.birth, p.death = birth or p.birth, death or p.death
    p.native_languages = list(e.claims.get("P1412", [])) + list(e.claims.get("P103", []))
    for lang in NAME_LANGUAGES:
        values = {e.labels[lang]} if lang in e.labels else set()
        values |= set(e.aliases.get(lang, []))
        if values:
            p.variants[lang] = values


def display_names(persons: dict[str, Person], entities: dict[str, Entity]) -> None:
    """Nom d'usage : libellé Wikidata (fr, puis en, puis langue d'origine),
    sinon la graphie la plus fréquente lue dans les EPUB. Une même personne
    (même QID) a partout le même nom ; deux personnes distinctes ne partagent
    jamais un nom (suffixe des dates)."""
    for p in persons.values():
        e = entities.get(p.qid) if p.qid else None
        if e is not None:
            p.display = e.label(*DISPLAY_LANGUAGES) or max(p.names, key=len)
        else:
            p.display = max(p.names, key=len)
        p.sort_name = _sort_name(p.display)
    by_display: dict[str, set[str]] = {}
    for p in persons.values():
        by_display.setdefault(p.display, set()).add(p.qid or f"?{p.key}")
    for p in persons.values():
        if len(by_display[p.display]) > 1 and p.qid:
            dates = f"{p.birth or '?'}–{p.death or '?'}"
            p.display = f"{p.display} ({dates})"


def _sort_name(display: str) -> str:
    """« Fiodor Dostoïevski » → « Dostoïevski, Fiodor »."""
    parts = display.split()
    if len(parts) < 2:
        return display
    particles = {"de", "du", "des", "von", "van", "der", "da", "di", "del", "la", "le", "y"}
    i = len(parts) - 1
    while i > 1 and parts[i - 1].lower() in particles:
        i -= 1
    return f"{' '.join(parts[i:])}, {' '.join(parts[:i])}"
