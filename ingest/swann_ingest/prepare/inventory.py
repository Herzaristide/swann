"""Inventaire : ce que dit chaque EPUB de lui-même (OPF, page de licence) et
ce qu'on lit dedans (texte, langue). Fonction pure par fichier, exécutée en
parallèle ; résultat mis en cache par sha256."""

from __future__ import annotations

import os
import zipfile
from collections.abc import Callable
from concurrent.futures import ProcessPoolExecutor, as_completed
from pathlib import Path

from lxml import etree

from swann_ingest.epub.container import NS, XML_PARSER, sha256_file
from swann_ingest.prepare.cache import Cache

# À incrémenter quand le contenu d'une fiche d'inventaire change.
VERSION = 5
SAMPLE = 12  # paragraphes du corps conservés (répartis dans le livre)
SAMPLE_CHARS = 500
HEAD = 4  # premiers paragraphes du corps (repérage des doublons)


def _texts(meta: etree._Element, tag: str) -> list[str]:
    out = []
    for el in meta.iterfind(f"dc:{tag}", NS):
        text = " ".join("".join(el.itertext()).split())
        if text:
            out.append(text)
    return out


def read_opf(path: Path) -> dict:
    """Métadonnées brutes de l'OPF, rôles et file-as (EPUB 2 et 3) compris."""
    with zipfile.ZipFile(path) as zf:
        container = etree.fromstring(zf.read("META-INF/container.xml"), XML_PARSER)
        opf_path = container.find(".//container:rootfile", NS).get("full-path")
        opf = etree.fromstring(zf.read(opf_path), XML_PARSER)
        names = zf.namelist()
        last = next((n for n in names if n.rsplit("/", 1)[-1] == "last.xhtml"), None)
        last_text = (
            " ".join("".join(etree.fromstring(zf.read(last), XML_PARSER).itertext()).split())
            if last
            else None
        )
    meta = opf.find("opf:metadata", NS)
    if meta is None:
        meta = opf.find(".//{*}metadata")
    refines: dict[str, dict[str, str]] = {}
    generator = None
    for m in meta.iterfind("{*}meta"):
        if m.get("refines"):
            refines.setdefault(m.get("refines").lstrip("#"), {})[m.get("property")] = (m.text or "").strip()
        if m.get("name") == "generator":
            generator = m.get("content")
    people = []
    for tag in ("creator", "contributor"):
        for el in meta.iterfind(f"dc:{tag}", NS):
            name = " ".join("".join(el.itertext()).split())
            if not name:
                continue
            ref = refines.get(el.get("id", ""), {})
            people.append(
                {
                    "tag": tag,
                    "name": name,
                    "role": el.get(f"{{{NS['opf']}}}role") or ref.get("role") or None,
                    "file_as": el.get(f"{{{NS['opf']}}}file-as") or ref.get("file-as") or None,
                }
            )
    return {
        "identifiers": _texts(meta, "identifier"),
        "sources": _texts(meta, "source"),
        "publishers": _texts(meta, "publisher"),
        "rights": _texts(meta, "rights"),
        "titles": _texts(meta, "title"),
        "languages": _texts(meta, "language"),
        "dates": _texts(meta, "date"),
        "subjects": _texts(meta, "subject")[:10],
        "people": people,
        "generator": generator,
        "last_page": last_text[:4000] if last_text else None,
    }


def read_text(path: Path) -> dict:
    """Lecture par le parseur de Swann : longueur, langue détectée (votes),
    extraits du corps."""
    from swann_ingest.parse import parse_epub
    from swann_ingest.text.language import detect_language

    try:
        parsed = parse_epub(path)
    except Exception as e:  # noqa: BLE001 - toute erreur de lecture est rapportée
        return {"error": f"{type(e).__name__}: {e}"[:300]}
    # Tout le corps hors titres : les poèmes sont en vers, pas en paragraphes.
    body = [
        s.text
        for s in parsed.text.segments
        if s.kind != "heading" and parsed.text.matter.get(s.section_id) == "body"
    ]
    votes = []
    for frac in (0.25, 0.5, 0.75):
        i = int(len(body) * frac)
        sample, j = [], i
        while j < len(body) and sum(map(len, sample)) < 3000:
            sample.append(body[j])
            j += 1
        votes.append(detect_language("\n".join(sample)))
    from swann_ingest.text.language import detect_majority

    step = max(1, len(body) // SAMPLE)
    stats = parsed.stats()
    return {
        "error": None,
        "method": parsed.structure_method,
        "warnings": parsed.warnings,
        "body_chars": stats["body_chars"],
        "body_paragraphs": len(body),
        "chapters": sum(1 for s in parsed.text.sections if s.kind == "chapter"),
        "votes": votes,
        # Langue que `swann extract` détectera (paragraphes du corps seulement)
        "extract_language": detect_majority(parsed.body_paragraphs()),
        "head": [p[:SAMPLE_CHARS] for p in body[:HEAD]],
        "sample": [p[:SAMPLE_CHARS] for p in body[::step][:SAMPLE]],
    }


def _inventory_one(path: Path, sha: str) -> dict:
    record: dict = {"sha": sha, "size": path.stat().st_size}
    try:
        record["opf"] = read_opf(path)
    except Exception as e:  # noqa: BLE001
        record["opf"] = None
        record["opf_error"] = f"{type(e).__name__}: {e}"[:300]
    record["text"] = read_text(path)
    return record


def _sha(path: Path) -> str:
    return sha256_file(path)


def inventory(
    src: Path, cache: Cache, workers: int, progress: Callable[[int, int, str], None] | None = None
) -> dict[str, dict]:
    """Nom de fichier -> fiche d'inventaire, pour tous les EPUB à la racine de src."""
    files = sorted(p for p in src.iterdir() if p.suffix.lower() == ".epub" and p.is_file())
    out: dict[str, dict] = {}
    todo: list[tuple[Path, str]] = []
    unknown: list[Path] = []
    for p in files:
        st = p.stat()
        sha = cache.file_sha(p.name, st.st_size, st.st_mtime)
        if sha is None:
            unknown.append(p)
            continue
        if (record := cache.book(sha, VERSION)) is not None:
            out[p.name] = record
        else:
            todo.append((p, sha))

    total, done = len(files), len(out)
    with ProcessPoolExecutor(max_workers=max(1, workers)) as pool:
        futures = {pool.submit(_sha, p): p for p in unknown}
        for f in as_completed(futures):
            p = futures[f]
            st = p.stat()
            sha = f.result()
            cache.put_file(p.name, st.st_size, st.st_mtime, sha)
            if (record := cache.book(sha, VERSION)) is not None:
                out[p.name] = record
                done += 1
            else:
                todo.append((p, sha))
        if progress:
            progress(done, total, "")
        futures = {pool.submit(_inventory_one, p, sha): p for p, sha in todo}
        for f in as_completed(futures):
            p = futures[f]
            record = f.result()
            cache.put_book(record["sha"], VERSION, record)
            out[p.name] = record
            done += 1
            if progress:
                progress(done, total, p.name)
    return out


def default_workers() -> int:
    return os.cpu_count() or 4
