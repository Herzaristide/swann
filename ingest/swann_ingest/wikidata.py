"""Client Wikidata minimal pour le classement des dépôts (docs/console.md §4.2).

Réponses mises en cache dans `wikidata_cache` (clé = requête) : une même
recherche n'interroge Wikidata qu'une fois. Toute erreur réseau lève
`WikidataError` : le classement continue sans Wikidata (et le signale).
"""

from __future__ import annotations

import json
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field

import psycopg
from psycopg.types.json import Jsonb

API = "https://www.wikidata.org/w/api.php"
TIMEOUT = 15
RETRIES = 3
MIN_INTERVAL = 0.5  # secondes entre deux appels
HUMAN = "Q5"


class WikidataError(Exception):
    pass


@dataclass
class Entity:
    id: str
    labels: dict[str, str] = field(default_factory=dict)  # langue -> libellé
    aliases: dict[str, list[str]] = field(default_factory=dict)
    descriptions: dict[str, str] = field(default_factory=dict)
    claims: dict[str, list] = field(default_factory=dict)  # propriété -> valeurs simplifiées

    def names(self) -> set[str]:
        out = set(self.labels.values())
        for values in self.aliases.values():
            out.update(values)
        return out

    def label(self, *languages: str) -> str | None:
        for lang in (*languages, "en", "mul"):
            if lang in self.labels:
                return self.labels[lang]
        return next(iter(self.labels.values()), None)

    def first(self, prop: str):
        values = self.claims.get(prop) or []
        return values[0] if values else None


@dataclass
class WorkHit:
    id: str
    labels: set[str]
    year: int | None
    language: str | None  # code ISO 639-1 de la langue originale
    authors: set[str] = field(default_factory=set)  # QID des auteurs (P50)


class Wikidata:
    def __init__(self, conn: psycopg.Connection | None, user_agent: str, enabled: bool = True) -> None:
        self.conn = conn
        self.user_agent = user_agent
        self.enabled = enabled
        self.min_interval = MIN_INTERVAL
        self._last_call = 0.0

    # -------------------------------------------------------------- transport
    def _cached(self, key: str) -> dict | None:
        if self.conn is None:
            return None
        row = self.conn.execute(
            "SELECT value FROM wikidata_cache WHERE key = %s AND fetched_at > now() - interval '90 days'",
            (key,),
        ).fetchone()
        return row["value"] if row else None

    def _store(self, key: str, value: dict) -> None:
        if self.conn is not None:
            self.conn.execute(
                "INSERT INTO wikidata_cache (key, value) VALUES (%s, %s) "
                "ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, fetched_at = now()",
                (key, Jsonb(value)),
            )

    def _get(self, url: str, params: dict, accept: str = "application/json") -> dict:
        if not self.enabled:
            raise WikidataError("Wikidata désactivé (WIKIDATA_ENABLED=false)")
        key = f"{url}?{urllib.parse.urlencode(sorted(params.items()))}"
        cached = self._cached(key)
        if cached is not None:
            return cached
        request = urllib.request.Request(
            f"{url}?{urllib.parse.urlencode(params)}",
            headers={"User-Agent": self.user_agent, "Accept": accept},
        )
        for attempt in range(RETRIES + 1):
            # Wikidata limite les clients trop pressés (429) : on espace les appels.
            wait = self.min_interval - (time.monotonic() - self._last_call)
            if wait > 0:
                time.sleep(wait)
            self._last_call = time.monotonic()
            try:
                with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
                    value = json.load(response)
                break
            except urllib.error.HTTPError as e:
                if e.code in (429, 503) and attempt < RETRIES:
                    retry_after = e.headers.get("Retry-After", "")
                    time.sleep(min(float(retry_after) if retry_after.isdigit() else 2.0 * (attempt + 1), 10))
                    continue
                raise WikidataError(f"Wikidata injoignable : {e}") from e
            except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, OSError) as e:
                raise WikidataError(f"Wikidata injoignable : {e}") from e
        self._store(key, value)
        return value

    # ------------------------------------------------------------------ API
    def search(self, text: str, language: str, limit: int = 7) -> list[str]:
        """Identifiants des entités dont le libellé ou un alias correspond."""
        lang = language.split("-")[0]
        data = self._get(
            API,
            {
                "action": "wbsearchentities",
                "search": text[:250],
                "language": lang,
                "uselang": lang,
                "type": "item",
                "limit": str(limit),
                "format": "json",
            },
        )
        return [hit["id"] for hit in data.get("search", [])]

    def entities(self, ids: list[str]) -> dict[str, Entity]:
        out: dict[str, Entity] = {}
        for i in range(0, len(ids), 50):
            batch = ids[i : i + 50]
            data = self._get(
                API,
                {
                    "action": "wbgetentities",
                    "ids": "|".join(batch),
                    "props": "labels|aliases|descriptions|claims",
                    "format": "json",
                },
            )
            for qid, raw in data.get("entities", {}).items():
                if "missing" in raw:
                    continue
                out[qid] = _entity(qid, raw)
        return out

    def humans(self, name: str, language: str) -> list[Entity]:
        """Personnes (instance de Q5) correspondant au nom."""
        ids = self.search(name, language)
        if not ids:
            return []
        return [e for e in self.entities(ids).values() if HUMAN in e.claims.get("P31", [])]

    def find_works(self, title: str, language: str, author_qids: set[str] | None = None) -> list[WorkHit]:
        """Œuvres dont un libellé correspond au titre (API de recherche, pas
        SPARQL : le point SPARQL est souvent saturé). Garde les œuvres d'un des
        auteurs donnés (P50) et écarte les éditions (P629)."""
        ids = self.search(title, language, limit=10)
        if not ids:
            return []
        entities = self.entities(ids)
        hits = []
        for e in entities.values():
            if e.claims.get("P629"):
                continue
            authors = set(e.claims.get("P50", []))
            if author_qids and not authors & author_qids:
                continue
            if not author_qids and not authors:
                continue
            language_qid = e.first("P364") or e.first("P407")
            hits.append(
                WorkHit(
                    e.id,
                    e.names(),
                    year_of(e.first("P577")),
                    self.language_code(language_qid) if language_qid else None,
                    authors,
                )
            )
        return hits

    def language_code(self, qid: str) -> str | None:
        """Code ISO 639-1 (P218) d'une langue Wikidata."""
        entity = self.entities([qid]).get(qid)
        return entity.first("P218") if entity else None


def _entity(qid: str, raw: dict) -> Entity:
    claims: dict[str, list] = {}
    for prop, statements in raw.get("claims", {}).items():
        values = []
        for st in statements:
            snak = st.get("mainsnak", {})
            if snak.get("snaktype") != "value":
                continue
            value = snak.get("datavalue", {}).get("value")
            if isinstance(value, dict) and "id" in value:
                values.append(value["id"])
            elif isinstance(value, dict) and "time" in value:
                values.append(value["time"])
            else:
                values.append(value)
        claims[prop] = values
    return Entity(
        id=qid,
        labels={k: v["value"] for k, v in raw.get("labels", {}).items()},
        aliases={k: [a["value"] for a in v] for k, v in raw.get("aliases", {}).items()},
        descriptions={k: v["value"] for k, v in raw.get("descriptions", {}).items()},
        claims=claims,
    )


def year_of(time_value: str | None) -> int | None:
    """« +1821-11-11T00:00:00Z » → 1821."""
    if not time_value:
        return None
    m = re.match(r"^\+?(-?\d{1,4})", time_value)
    return int(m.group(1)) if m else None
