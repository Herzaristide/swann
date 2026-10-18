"""Enchaînement de `swann prepare`."""

from __future__ import annotations

import shutil
from collections import Counter, defaultdict
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from swann_ingest.prepare import inventory as inv
from swann_ingest.prepare import layout
from swann_ingest.prepare import persons as pers
from swann_ingest.prepare import works as wk
from swann_ingest.prepare.cache import Cache
from swann_ingest.prepare.sources import Draft, fold, non_literary, normalize
from swann_ingest.prepare.wikidata import CachedWikidata
from swann_ingest.text.language import LANGUAGE_CHECK_MIN_CHARS

Log = Callable[[str], None]
Progress = Callable[[str, int, int, str], None]


@dataclass
class Report:
    editions: list[dict] = field(default_factory=list)
    review: list[dict] = field(default_factory=list)
    persons: list[dict] = field(default_factory=list)
    quarantine: Counter = field(default_factory=Counter)
    access: Counter = field(default_factory=Counter)
    works: int = 0
    works_qid: int = 0
    multi: int = 0  # œuvres à plusieurs éditions

    def flag(self, kind: str, file: str, detail: str) -> None:
        self.review.append({"type": kind, "fichier": file, "détail": detail})


def _language(d: Draft, record: dict, report: Report, known: set[str]) -> str | None:
    """Langue retenue : la déclarée, sauf si la détection la contredit
    unanimement sur un texte assez long, vers une langue présente dans le lot
    (pas de « sesotho » pour du moyen haut allemand). None : à vérifier."""
    chosen = _detected_language(d, record, report, known)
    # Le contrôle de `swann extract` doit passer, sinon l'édition échouerait
    extract = record["text"].get("extract_language")
    long_enough = (record["text"].get("body_chars") or 0) >= LANGUAGE_CHECK_MIN_CHARS
    if chosen and extract and long_enough and extract != chosen.split("-")[0]:
        return None
    return chosen


def _detected_language(d: Draft, record: dict, report: Report, known: set[str]) -> str | None:
    votes = [v for v in record["text"].get("votes") or [] if v]
    declared = d.language
    if not votes:
        return declared
    top, n = Counter(votes).most_common(1)[0]
    if declared and declared.split("-")[0] == top:
        return declared
    # La détection n'est fiable que sur un texte assez long (trois extraits distincts).
    long_enough = (record["text"].get("body_chars") or 0) >= LANGUAGE_CHECK_MIN_CHARS
    if n == len(votes) and n >= 2 and long_enough and top in known:
        if declared:
            report.flag("langue corrigée", d.file, f"déclarée {declared!r}, détectée {top!r}")
        return top
    return None


def _encoder(cache: Cache, model_name: str) -> Callable:
    """Encodeur LaBSE (chargé au premier besoin), vecteurs mis en cache."""
    import hashlib

    import numpy as np

    model = None

    def encode(texts: list[str]):
        nonlocal model
        keys = [hashlib.sha256(f"{model_name}\n{t}".encode()).hexdigest() for t in texts]
        known = cache.vectors(keys)
        todo = [i for i, k in enumerate(keys) if k not in known]
        if todo:
            if model is None:
                from swann_ingest.pipeline.align import AlignEncoder

                model = AlignEncoder(model_name)
            for start in range(0, len(todo), 2048):
                batch = todo[start : start + 2048]
                vectors = model.encode([texts[i] for i in batch])
                cache.put_vectors({keys[i]: v for i, v in zip(batch, vectors, strict=True)})
                known.update({keys[i]: v for i, v in zip(batch, vectors, strict=True)})
        return np.stack([known[k] for k in keys])

    return encode


def prepare(
    src: Path,
    out: Path,
    *,
    workers: int,
    user_agent: str,
    wikidata: bool,
    content: bool = True,
    align_model: str = "sentence-transformers/LaBSE",
    log: Log,
    progress: Progress,
) -> Report:
    cache = Cache(out / "cache.sqlite")
    report = Report()

    # 1. Inventaire
    records = inv.inventory(src, cache, workers, lambda d, t, c: progress("Inventaire", d, t, c))
    log(f"{len(records)} EPUB inventoriés")

    # 2. Normalisation, quarantaine
    quarantine: dict[str, str] = {}
    editions: list[wk.Edition] = []
    # Langues plausibles : celles que déclarent au moins 20 livres du lot
    declared = Counter(
        x.split("-")[0].lower() for r in records.values() if r.get("opf") for x in r["opf"]["languages"][:1]
    )
    known = {lang for lang, n in declared.items() if n >= 20}
    for file, record in sorted(records.items()):
        if record.get("opf") is None:
            quarantine[file] = "illisible"
            report.flag("illisible", file, record.get("opf_error") or "")
            continue
        if record["text"].get("error") or not record["text"].get("body_chars"):
            quarantine[file] = "illisible"
            report.flag("illisible", file, record["text"].get("error") or "aucun texte dans le corps")
            continue
        d = normalize(file, record)
        if why := non_literary(d):
            quarantine[file] = "non-litteraire"
            report.flag("non littéraire", file, why)
            continue
        language = _language(d, record, report, known)
        if language is None:
            quarantine[file] = "langue-incertaine"
            votes = record["text"].get("votes")
            report.flag("langue incertaine", file, f"déclarée {d.language!r}, détection {votes}")
            continue
        for f in d.flags:
            report.flag("métadonnées", file, f)
        editions.append(
            wk.Edition(
                draft=d,
                record=record,
                language=language,
                author_keys=[k for a in d.authors if (k := fold(a.name))],
                translator_keys=[k for t in d.translators if (k := fold(t.name))],
            )
        )
    log(f"{len(editions)} éditions retenues, {len(quarantine)} en quarantaine")

    # 3. Personnes
    wd = CachedWikidata(cache, user_agent, enabled=wikidata)
    names = [(n, e.language) for e in editions for n in (*e.draft.authors, *e.draft.translators)]
    persons = pers.collect(names)
    pers.resolve(wd, persons, lambda d, t, c: progress("Personnes (Wikidata)", d, t, c))
    if wikidata:
        titles: dict[str, list[tuple[int, str]]] = defaultdict(list)
        for e in editions:
            for k in e.author_keys:
                titles[k].append((id(e), e.draft.title))
        moved = pers.disambiguate(wd, persons, titles, lambda qids: wk.author_works(wd, qids))
        for e in editions:
            e.author_keys = [moved.get((id(e), k), k) for k in e.author_keys]
        log(f"{len(moved)} éditions d'homonymes attribuées par les titres")
    resolved = sum(1 for p in persons.values() if p.qid)
    log(f"{len(persons)} personnes, {resolved} rattachées à Wikidata")
    person_entities = wd.entities([p.qid for p in persons.values() if p.qid]) if wikidata else {}
    pers.display_names(persons, person_entities)
    for p in persons.values():
        if p.note and p.count >= 1:
            report.flag("personne", next(iter(p.names)), f"{p.note} ({p.count} édition(s))")

    # 4. Œuvres
    if wikidata:
        wk.link_items(wd, editions, log)
        person_qid = {k: p.qid for k, p in persons.items() if p.qid}
        author_qids = sorted({person_qid[k] for e in editions for k in e.author_keys if k in person_qid})
        progress("Œuvres des auteurs (Wikidata)", 0, len(author_qids), "")
        by_author = wk.author_works(wd, author_qids)
        wk.match_titles(editions, by_author, person_qid)
    identity = {k: p.qid or k for k, p in persons.items()}
    works = wk.group(editions, identity)
    if content:
        encode = _encoder(cache, align_model)
        merged = wk.match_content(works, identity, encode, log)
        log(f"{len(merged)} traductions réunies par le contenu")
        for keep, drop, share in merged:
            report.flag(
                "rapprochement par le contenu",
                drop.editions[0].draft.file,
                f"réuni à {keep.editions[0].draft.file} ({share:.0%} des extraits retrouvés)",
            )
    work_entities = wd.entities([w.qid for w in works if w.qid]) if wikidata else {}
    lang_qids = {q for e in work_entities.values() for p in ("P407", "P364") for q in e.claims.get(p, [])}
    lang_qids |= {q for p in persons.values() for q in p.native_languages}
    lang_codes = wk.language_codes(wd, lang_qids) if wikidata and lang_qids else {}
    native = {k: [lang_codes[q] for q in p.native_languages if q in lang_codes] for k, p in persons.items()}
    wk.finish(works, work_entities, native, lang_codes, identity)

    # 5. Droits et noms
    namer = layout.Namer()
    staging = out / "books.next"
    if staging.exists():
        shutil.rmtree(staging)
    used: set[str] = set()
    for w in sorted(works, key=lambda x: (x.author_keys, x.title)):
        report.works += 1
        report.works_qid += bool(w.qid)
        live = [e for e in w.editions if e.duplicate_of is None]
        report.multi += len(live) > 1
        rights = {id(e): layout.access(e, w, persons) for e in live}
        files = layout.edition_files(w, persons)
        author_dir = namer.author_dir(w, persons)
        work_dir = staging / author_dir / namer.work_dir(author_dir, w)
        used.update(w.author_keys)
        for e in live:
            used.update(e.translator_keys)
            layout.link(src / e.draft.file, work_dir / files[id(e)])
            level, why = rights[id(e)]
            report.access[level] += 1
            report.editions.append(
                {
                    "fichier": e.draft.file,
                    "destination": f"{author_dir}/{work_dir.name}/{files[id(e)]}",
                    "source": e.draft.source,
                    "œuvre": w.title,
                    "wikidata": w.qid or "",
                    "rattachement": e.how or ("titre identique" if len(w.editions) > 1 else ""),
                    "langue": e.language,
                    "original": "oui" if e.original else "",
                    "accès": level,
                    "motif": why,
                }
            )
        (work_dir / "work.toml").write_text(layout.work_toml(w, files, persons, rights), "utf-8")
        for note in w.notes:
            report.flag("œuvre", f"{author_dir}/{work_dir.name}", note)
        if w.editions and not any(e.original for e in live) and len(live) > 1:
            report.flag(
                "œuvre", f"{author_dir}/{work_dir.name}", "pas d'édition originale parmi les éditions"
            )
        for e in w.editions:
            if e.duplicate_of is not None:
                quarantine[e.draft.file] = "doublons"
                report.flag("doublon", e.draft.file, f"même texte que {e.duplicate_of.draft.file} (gardé)")
        if any(e.draft.volume for e in live):
            report.flag("tome", f"{author_dir}/{work_dir.name}", "un tome d'une œuvre en plusieurs volumes")
    (staging / "persons.toml").write_text(layout.persons_toml(persons, used), "utf-8")

    # 6. Bascule : books.next -> books (les liens durs ne coûtent rien)
    books = out / "books"
    if books.exists():
        shutil.rmtree(books)
    staging.rename(books)
    qdir = out / "quarantaine"
    if qdir.exists():
        shutil.rmtree(qdir)
    for file, reason in quarantine.items():
        layout.link(src / file, qdir / reason / file)
        report.quarantine[reason] += 1
    report.persons = [
        {
            "nom d'usage": p.display,
            "graphies lues": " | ".join(sorted(p.names)),
            "wikidata": p.qid or "",
            "naissance": p.birth or "",
            "mort": p.death or "",
            "éditions": p.count,
            "remarque": p.note or "",
        }
        for p in sorted(persons.values(), key=lambda x: -x.count)
    ]
    write_report(out / "rapport", report)
    return report


def write_report(folder: Path, report: Report) -> None:
    if folder.exists():
        shutil.rmtree(folder)
    folder.mkdir(parents=True)
    (folder / "editions.csv").write_text(wk.csv_rows(report.editions), "utf-8")
    (folder / "a-verifier.csv").write_text(
        wk.csv_rows(sorted(report.review, key=lambda r: (r["type"], r["fichier"]))), "utf-8"
    )
    (folder / "personnes.csv").write_text(wk.csv_rows(report.persons), "utf-8")
    kinds = Counter(r["type"] for r in report.review)
    lines = [
        "# Préparation des livres",
        "",
        f"- éditions rangées : {len(report.editions)}",
        f"- œuvres : {report.works} (dont {report.works_qid} rattachées à Wikidata, "
        f"{report.multi} à plusieurs éditions)",
        f"- personnes : {len(report.persons)} "
        f"(dont {sum(1 for p in report.persons if p['wikidata'])} rattachées à Wikidata)",
        "",
        "## Accès",
        *[f"- {k} : {v}" for k, v in report.access.most_common()],
        "",
        "## Quarantaine",
        *[f"- {k} : {v}" for k, v in report.quarantine.most_common()],
        "",
        "## À vérifier (a-verifier.csv)",
        *[f"- {k} : {v}" for k, v in kinds.most_common()],
    ]
    (folder / "resume.md").write_text("\n".join(lines) + "\n", "utf-8")
