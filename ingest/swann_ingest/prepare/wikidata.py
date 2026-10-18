"""Wikidata (et Wikisource) en masse pour `swann prepare` : le client de
l'ingestion, avec un cache SQLite à la place de la table wikidata_cache, plus
SPARQL et les pages Wikisource."""

from __future__ import annotations

import math

from swann_ingest.prepare.cache import Cache
from swann_ingest.wikidata import API, Entity, Wikidata, WikidataError, _entity

SPARQL = "https://query.wikidata.org/sparql"
# Instances acceptées comme « personne » : humain, groupe, duo, fratrie (frères Grimm), pseudonyme collectif.
PERSONLIKE = {"Q5", "Q16334295", "Q10648343", "Q14073567", "Q16017119", "Q61002", "Q125258836"}
EDITION = "Q3331189"  # version, édition ou traduction


class CachedWikidata(Wikidata):
    def __init__(self, cache: Cache, user_agent: str, enabled: bool = True) -> None:
        super().__init__(None, user_agent, enabled)
        self.cache = cache
        # Appels en série (jamais en parallèle), 5 par seconde au plus : le
        # rythme du classement (2/s) prendrait des heures sur un gros lot.
        self.min_interval = 0.2

    def _cached(self, key: str) -> dict | None:
        return self.cache.http(key)

    def _store(self, key: str, value: dict) -> None:
        self.cache.put_http(key, value)

    # ------------------------------------------------------------- entités
    def entities(self, ids: list[str]) -> dict[str, Entity]:
        """Comme Wikidata.entities, plus le nombre de liens interwikis
        (claims["_sitelinks"]) : départage les homonymes par notoriété."""
        out: dict[str, Entity] = {}
        ids = sorted(set(ids))
        for i in range(0, len(ids), 50):
            data = self._get(
                API,
                {
                    "action": "wbgetentities",
                    "ids": "|".join(ids[i : i + 50]),
                    "props": "labels|aliases|claims|sitelinks",
                    "format": "json",
                },
            )
            for qid, raw in data.get("entities", {}).items():
                if "missing" in raw:
                    continue
                entity = _entity(qid, raw)
                entity.claims["_sitelinks"] = [len(raw.get("sitelinks", {}))]
                out[qid] = entity
        return out

    def search_humans(self, text: str, limit: int = 20) -> list[str]:
        """Recherche plein texte restreinte aux humains : retrouve un nom de
        famille courant que wbsearchentities noie sous les homonymes."""
        data = self._get(
            API,
            {
                "action": "query",
                "list": "search",
                "srsearch": f"{text[:200]} haswbstatement:P31=Q5",
                "srlimit": str(limit),
                "srnamespace": "0",
                "format": "json",
            },
        )
        return [hit["title"] for hit in data.get("query", {}).get("search", [])]

    def sparql(self, query: str) -> list[dict[str, str]]:
        data = self._get(SPARQL, {"query": query, "format": "json"}, accept="application/sparql-results+json")
        return [{k: v["value"] for k, v in row.items()} for row in data["results"]["bindings"]]

    # ---------------------------------------------------------- Wikisource
    def wikisource_items(self, lang: str, titles: list[str]) -> dict[str, str]:
        """Titre de page -> QID de l'élément Wikidata lié (s'il existe)."""
        out: dict[str, str] = {}
        api = f"https://{lang}.wikisource.org/w/api.php"
        titles = sorted(set(titles))
        for i in range(0, len(titles), 15):
            batch = titles[i : i + 15]
            try:
                data = self._get(
                    api,
                    {
                        "action": "query",
                        "prop": "pageprops",
                        "ppprop": "wikibase_item",
                        "redirects": "1",
                        "titles": "|".join(batch),
                        "format": "json",
                    },
                )
            except WikidataError:
                continue
            query = data.get("query", {})
            alias: dict[str, str] = {}
            for step in ("normalized", "redirects"):
                for r in query.get(step, []):
                    alias[r["to"]] = alias.get(r["from"], r["from"])
            for page in query.get("pages", {}).values():
                qid = page.get("pageprops", {}).get("wikibase_item")
                if qid:
                    out[alias.get(page["title"], page["title"])] = qid
        return out


def sitelinks(entity: Entity) -> int:
    return int((entity.claims.get("_sitelinks") or [0])[0])


def notability(entity: Entity) -> float:
    return math.log1p(sitelinks(entity))
