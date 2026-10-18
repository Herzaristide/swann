# Corpus API — design (v1)

> **État** : implémentée dans `corpus-api/` (FastAPI). Ce document est le
> contrat de référence ; le détail des paramètres et des réponses est publié
> par l'API elle-même (`/v1/docs`, `/v1/openapi.json`).

API HTTP au-dessus du corpus (Postgres + Qdrant + MinIO). **Seule porte
d'entrée** des applications (liseuse, relecture des alignements, analyses…)
vers les données littéraires. Le schéma des bases reste un détail
d'implémentation : on peut le faire évoluer, re-chunker ou changer de modèle
d'embedding sans casser les applications.

```
apps (Next.js, scripts…) ──HTTPS + JWT Keycloak──► corpus-api (FastAPI)
                                                     ├─► Postgres corpus  (métadonnées, texte, alignements)
                                                     ├─► Qdrant           (alias `chunks`)
                                                     └─► MinIO            (EPUB)
ingest (CLI swann) ──────────── écrit directement ────► Postgres / Qdrant / MinIO
```

## 1. Principes

- **Lecture d'abord.** L'API lit le corpus. Elle n'écrit que pour des actions
  humaines ciblées (verdicts et corrections d'alignement). L'ingestion et
  l'administration des index restent dans la CLI `swann`.
- **Aucune donnée utilisateur.** L'API ne connaît d'un utilisateur que le
  `sub` de son jeton. Favoris, progression et collections vivent dans la base
  de chaque application.
- **Versionnée** sous `/v1`. Un changement incompatible = `/v2`, avec `/v1`
  maintenu le temps de migrer les apps.
- **Ressources du domaine**, pas des tables : œuvre, édition, table des
  matières, passage, résultat de recherche, alignement.
- **Identifiants publics** : UUID des œuvres, éditions, personnes, courants et
  sections. Les segments sont désignés par **`(edition_id, revision, seq)`**,
  jamais par leur UUID (voir §5).
- JSON, `snake_case`, dates ISO 8601, langues BCP 47.
- Contrat décrit en OpenAPI (`/v1/openapi.json`), généré par FastAPI ; les
  clients TypeScript et Python des apps sont générés à partir de ce contrat.

## 2. Authentification et droits

Jetons **JWT émis par Keycloak** (realm `swann`), vérifiés localement avec les
clés publiques (JWKS) du realm. Audience attendue : `corpus-api`.

| Appelant | Flux OAuth | Exemple |
| --- | --- | --- |
| Backend d'une app | `client_credentials` (compte de service) | le serveur Next.js de la liseuse |
| Utilisateur via une app | jeton utilisateur transmis par l'app | un relecteur qui corrige un alignement |
| Script d'analyse | `client_credentials` | notebook, job batch |

Rôles (rôles de client Keycloak sur `corpus-api`) :

| Rôle | Autorise |
| --- | --- |
| `corpus:read` | catalogue, texte des éditions accessibles, recherche, alignements |
| `corpus:review` | verdicts et corrections d'alignement (jeton **utilisateur** obligatoire : son `sub` est enregistré) |
| `corpus:admin` | lecture des éditions `restricted`, suivi des ingestions et des index |

**Droits sur les textes** (nouveau champ `editions.access`, voir §9) :

| `access` | Métadonnées | Passages dans la recherche | Texte intégral, EPUB |
| --- | --- | --- | --- |
| `open` (domaine public, licence libre) | ✓ | ✓ | ✓ |
| `excerpt` (sous droits) | ✓ | ✓ (≤ 1 000 caractères) | ✗ `403` |
| `restricted` | `corpus:admin` uniquement | ✗ | ✗ |

Une édition invisible répond `404` (comme une édition absente) ; une édition
visible dont le texte n'est pas accessible répond `403`. Les rôles `corpus:review`
exigent un jeton **utilisateur** : un compte de service (`service-account-…`)
est refusé. En développement, `AUTH_DISABLED=true` accorde tous les droits
sans jeton.

## 3. Conventions transverses

**Langue d'affichage** : `?lang=fr` (sinon `Accept-Language`). Elle choisit
le titre de l'œuvre (`work_titles`), le nom des personnes (`person_names`) et
le libellé des courants (`movement_labels`). Ordre de repli : langue demandée,
puis langue originale, puis la valeur de référence (`works.title`,
`persons.display_name`, `movements.slug`). Le champ `title_language` indique
la langue effectivement servie.

**Pagination** par curseur opaque :

```json
{ "items": [ … ], "next_cursor": "eyJ0IjoiQ3JpbWUi…" }
```

`?limit=` (défaut 20, max 100), `?cursor=` pour la page suivante.

**Erreurs** au format RFC 9457 (`application/problem+json`) :

```json
{ "type": "https://swann/errors/revision-mismatch", "title": "Révision obsolète",
  "status": 409, "detail": "L'édition est en révision 4, la requête vise la 3.",
  "current_revision": 4 }
```

**Cache** : toutes les réponses de texte portent `ETag: "<edition_id>:<revision>"`.
Une requête qui précise `?rev=` est servie avec
`Cache-Control: private, max-age=31536000, immutable`, car le contenu d'une
révision ne change jamais. Si `rev` n'est plus la révision courante → `409`.

## 4. Endpoints

### Catalogue

| Méthode | Chemin | Rôle |
| --- | --- | --- |
| GET | `/v1/works` | liste filtrée et triée des œuvres |
| GET | `/v1/works/{work_id}` | fiche d'une œuvre et de ses éditions |
| GET | `/v1/persons` | auteurs, traducteurs… (`?role=author\|translator`, `?q=`) |
| GET | `/v1/persons/{person_id}` | fiche d'une personne, œuvres écrites et éditions traduites |
| GET | `/v1/movements` | arbre des courants avec leur nombre d'œuvres |
| GET | `/v1/movements/{movement_id}` | un courant, ses sous-courants |
| GET | `/v1/languages` | langues présentes dans le corpus, avec leur nombre d'éditions |
| GET | `/v1/suggest?q=` | autocomplétion : œuvres et personnes (titres et noms dans toutes les langues) |

Filtres de `/v1/works` : `q` (titre, toutes langues), `author_id`,
`movement_id`, `language` (au moins une édition dans cette langue),
`original_language`, `year_min`, `year_max`, `slug`.
Tris : `sort=title|-title|year|-year|author`.

Fiche d'œuvre :

```json
{
  "id": "6f1c2b0e-…",
  "slug": "dostoievski-fiodor/crime-et-chatiment",
  "title": "Crime et Châtiment",
  "title_language": "fr",
  "original_title": "Преступление и наказание",
  "original_language": "ru",
  "first_published_year": 1866,
  "wikidata_id": "Q160891",
  "authors": [
    { "id": "…", "name": "Fiodor Dostoïevski", "birth_year": 1821, "death_year": 1881 }
  ],
  "movements": [ { "id": "…", "slug": "realism", "label": "Réalisme" } ],
  "editions": [
    {
      "id": "a3e9…",
      "title": "Crime et Châtiment",
      "language": "fr",
      "is_original": false,
      "translators": [ { "id": "…", "name": "André Markowicz" } ],
      "publisher": "Actes Sud",
      "year": 1996,
      "access": "excerpt",
      "revision": 2,
      "alignment": { "status": "reliable", "aligned_ratio": 0.97 }
    }
  ]
}
```

### Éditions et texte

| Méthode | Chemin | Rôle |
| --- | --- | --- |
| GET | `/v1/editions/{edition_id}` | fiche détaillée d'une édition |
| GET | `/v1/editions/{edition_id}/toc` | table des matières (arbre des sections) ; `?matter=body` (répétable), `?include_notes=true` |
| GET | `/v1/editions/{edition_id}/segments` | texte : `?from_seq=&to_seq=&limit=` (défaut 100, max 500), `?include=notes` |
| GET | `/v1/editions/{edition_id}/position` | où se trouve `?seq=&offset=` : section, chemin, page, progression |
| GET | `/v1/editions/{edition_id}/notes/{note_id}` | contenu d'une note |
| GET | `/v1/editions/{edition_id}/find?q=` | recherche dans le livre (mots exacts, insensible à la casse et aux accents) |
| GET, HEAD | `/v1/editions/{edition_id}/epub` | fichier EPUB brut, si `access = open` (voir « EPUB bruts » ci-dessous) |
| POST | `/v1/editions/{edition_id}/anchors:resolve` | recaler des positions d'une ancienne révision (§5) |

Fiche d'édition : celle de la liste ci-dessus, plus `work_id`,
`contributors` (avec rôles), `n_pages`, `n_segments`, `char_length`,
`has_page_breaks`, `reference_edition_id`, `epub_available`.

Table des matières (`?matter=body` pour ne garder que le corps) :

```json
{
  "edition_id": "a3e9…",
  "revision": 2,
  "sections": [
    {
      "id": "…", "kind": "part", "matter": "body",
      "label": "Première partie", "title": null, "number": 1,
      "seq_start": 12, "seq_end": 840,
      "children": [
        { "id": "…", "kind": "chapter", "matter": "body", "label": "I", "title": null,
          "number": 1, "seq_start": 13, "seq_end": 95, "children": [] }
      ]
    }
  ]
}
```

`seq_start` / `seq_end` couvrent la section et ses descendants. La liseuse
charge un chapitre avec `/segments?from_seq=13&to_seq=95`.

Plage de segments (`?include=notes` pour joindre directement le contenu des
notes appelées) :

```json
{
  "edition_id": "a3e9…",
  "revision": 2,
  "total_segments": 4210,
  "segments": [
    {
      "seq": 120,
      "section_id": "…",
      "kind": "speech",
      "speaker": "RASKOLNIKOV",
      "char_start": 53210,
      "char_end": 53902,
      "text": "Je ne l'ai pas tuée pour…",
      "markup": "Je ne l'ai pas <em>tuée</em> pour…",
      "notes": [ { "offset": 118, "label": "3", "note_id": "…", "origin": "translator" } ],
      "pages": [ { "offset": 402, "label": "57" } ]
    }
  ],
  "next_from_seq": 121
}
```

Tous les `offset` sont **relatifs au segment** et comptés dans `text`, pas dans
`markup`. L'API convertit les `page_breaks.char_offset`, qui sont globaux à
l'édition.

### EPUB bruts

`GET /v1/editions/{edition_id}/epub` renvoie le fichier tel qu'il a été
ingéré, **en flux depuis MinIO à travers l'API**. MinIO n'est jamais exposé
aux applications : on pourra changer de stockage sans toucher aux clients.

- `Content-Type: application/epub+zip`, `Content-Length`,
  `Content-Disposition: attachment; filename="<auteur>-<œuvre>-<fichier>.epub"`
  (ex. `dostoevsky-fyodor-crime-and-punishment-fr--markowicz.epub`)
  (`?inline=true` pour `inline`, utile à une liseuse EPUB tierce).
- `ETag: "<sha256>"` : `If-None-Match` répond `304`. Le fichier d'une
  révision ne change jamais : `Cache-Control: private, max-age=31536000, immutable`
  quand `?rev=` est précisé.
- `Range` pris en charge (`206 Partial Content`), relayé tel quel à MinIO.
- `404` si l'EPUB n'est pas stocké (`epub_available = false`), `403` si
  `access` ≠ `open`.

Stockage : l'ingestion (`swann extract`) envoie chaque EPUB dans le bucket
`books` sous la clé `editions/<sha256>.epub` (un fichier remplacé = un nouvel
objet, l'ancien est supprimé) et renseigne `editions.epub_object_key`.

Position :

```json
{ "seq": 120, "section_id": "…", "path": ["Première partie", "Chapitre III"],
  "page_label": "57", "progress": 0.127 }
```

### Recherche

| Méthode | Chemin | Rôle |
| --- | --- | --- |
| POST | `/v1/search` | recherche dans tout le corpus : thème, citation, mots |
| POST | `/v1/similar` | passages proches par le sens d'un passage donné (« plus comme ceci ») |

```json
POST /v1/search
{
  "q": "un homme qui se demande s'il a le droit de tuer",
  "mode": "theme",
  "filters": {
    "languages": ["fr", "ru"],
    "original_only": false,
    "year": { "min": 1800, "max": 1900 },
    "author_ids": [], "movement_ids": [], "work_ids": [], "edition_ids": []
  },
  "group_by": "work",
  "show_languages": ["fr"],
  "limit": 20
}
```

- `mode` : `theme` (dense), `quote` (dense + BM25, fusion RRF, puis contrôle de
  la phrase exacte dans Postgres : les correspondances exactes remontent en
  tête avec `exact: true`), `words` (BM25 seul).
- `group_by` : `work` (un passage par œuvre, défaut), `edition`, ou `none`.
- `show_languages` : joint à chaque passage sa correspondance dans ces
  langues, via l'alignement (édition originale de préférence, sinon la mieux
  alignée).
- `limit` max 50, **pas de pagination** : le regroupement Qdrant ne propose
  pas de curseur fiable, et au-delà de 50 il vaut mieux affiner la requête.

```json
{
  "index": { "collection": "chunks_qwen3-0.6b_v1", "dense_model": "Qwen/Qwen3-Embedding-0.6B" },
  "took_ms": 84,
  "hits": [
    {
      "score": 0.83,
      "exact": false,
      "work": { "id": "…", "title": "Crime et Châtiment", "authors": [ { "id": "…", "name": "Fiodor Dostoïevski" } ],
                "first_published_year": 1866 },
      "edition": { "id": "…", "title": "Преступление и наказание", "language": "ru",
                   "is_original": true, "revision": 1 },
      "passage": {
        "seq_start": 1201, "seq_end": 1206,
        "section_path": ["Часть третья", "V"],
        "page_label": null,
        "text": "…",
        "highlights": [ [120, 152] ]
      },
      "translations": {
        "fr": { "edition_id": "…", "seq_start": 1188, "seq_end": 1192, "text": "…" }
      }
    }
  ]
}
```

`highlights` = plages `[début, fin[` dans `passage.text` (modes `quote` et
`words`). Pour une édition `excerpt`, `text` est tronqué autour de la
correspondance.

`POST /v1/similar` prend `{ "edition_id", "seq_start", "seq_end", "filters", "limit" }`,
retrouve les chunks qui couvrent ce passage et interroge Qdrant avec leurs
vecteurs déjà stockés : aucun calcul d'embedding.

### Alignement et lecture parallèle

| Méthode | Chemin | Rôle |
| --- | --- | --- |
| GET | `/v1/works/{work_id}/alignment` | qualité de l'alignement de chaque édition |
| GET | `/v1/editions/{edition_id}/counterpart` | position correspondante dans une autre édition : `?seq=&target=<edition_id>` ou `?seq=&lang=fr` |
| GET | `/v1/editions/{edition_id}/parallel` | lecture côte à côte : `?target=&from_seq=&to_seq=` |
| GET | `/v1/alignment/sample` | un lien tiré au hasard à vérifier (`?method=&min_score=&max_score=`) |
| POST | `/v1/alignment/reviews` | verdict sur un lien (`corpus:review`) |
| POST | `/v1/alignment/links` | lien manuel (correction, `corpus:review`) |
| DELETE | `/v1/alignment/links` | suppression d'un lien jugé faux : `?edition_id=&seq=&unit_id=` (`corpus:review`) |

`counterpart` sert à changer de traduction sans perdre sa place :

```json
{ "edition_id": "…", "revision": 1, "seq_start": 1188, "seq_end": 1189,
  "via": "segment", "score": 0.91 }
```

`via` = `segment` (alignement des paragraphes) ou `section` (repli au début du
chapitre correspondant, quand le paragraphe n'est pas aligné ou que
l'édition est `doubtful`).

`parallel` renvoie des paires de groupes de segments (relations n-n) :

```json
{
  "source": { "edition_id": "…", "revision": 2 },
  "target": { "edition_id": "…", "revision": 1 },
  "quality": "reliable",
  "pairs": [
    { "source_seqs": [120], "target_seqs": [118, 119], "score": 0.91, "method": "dp-labse" },
    { "source_seqs": [121], "target_seqs": [], "score": null, "method": null }
  ]
}
```

Les segments eux-mêmes se lisent avec `/segments` sur les deux éditions : la
liseuse a déjà ces données en cache.

Verdict :

```json
POST /v1/alignment/reviews
{ "source": { "edition_id": "…", "seq": 120 }, "unit_id": "…",
  "verdict": "incorrect", "is_sample": true, "comment": "décalage d'un paragraphe" }
```

L'API enregistre le `sub` du jeton comme relecteur, sans jamais toucher à des
comptes utilisateurs.

### Suivi des changements

| Méthode | Chemin | Rôle |
| --- | --- | --- |
| GET | `/v1/changes?after=<cursor>&limit=&type=` | flux ordonné des changements ; `type=work.` garde tous les `work.*` |

```json
{
  "items": [
    { "cursor": "1842", "at": "2026-10-02T18:04:11Z", "type": "edition.text_replaced",
      "work_id": "…", "edition_id": "…", "data": { "old_revision": 2, "new_revision": 3 } }
  ],
  "next_cursor": "1842"
}
```

| Type | `data` |
| --- | --- |
| `work.created` | — |
| `work.updated` | `fields` : champs modifiés (`title`, `authors`, `movements`…) |
| `work.alignment_changed` | `reference_edition_id`, statut par édition ; ou `reason` |
| `edition.created` | — |
| `edition.updated` | `fields` : champs modifiés (`access`, `publisher`, `translators`, `epub_object_key`…) |
| `edition.text_replaced` | `old_revision`, `new_revision` |
| `index.activated` | `collection`, `dense_model`, `chunker_version` |

`work.deleted` et `edition.deleted` sont réservés : l'ingestion ne supprime
encore rien.
Chaque app interroge ce flux à son rythme et garde son dernier curseur. Par
exemple, la liseuse recale les positions de ses lecteurs après un
`edition.text_replaced`. Le flux est alimenté par l'ingestion, dans la même
transaction que la modification (table `corpus_events`).

### Service

| Méthode | Chemin | Rôle |
| --- | --- | --- |
| GET | `/v1/health` | santé (Postgres, Qdrant, modèle chargé) — sans jeton |
| GET | `/v1/meta` | version de l'API, index actif, modèle dense, découpage |

## 5. Positions dans le texte : révisions et ancres

Le texte d'une édition peut être remplacé (nouveau fichier EPUB, extracteur
amélioré). Les segments sont alors recréés et les `seq` peuvent changer.
Pour qu'une app ne perde pas les positions de ses utilisateurs :

1. **Chaque édition a une `revision`** (entier), incrémentée à chaque
   remplacement du texte. Toutes les réponses de texte l'indiquent.
2. **Les apps stockent des ancres complètes** : position et extrait du texte
   (modèle *TextQuoteSelector* du W3C Web Annotation) :

   ```json
   { "edition_id": "…", "revision": 2, "seq": 120, "offset": 37,
     "quote": { "exact": "Je ne l'ai pas tuée", "prefix": "— Non ! ", "suffix": " pour aider ma mère" } }
   ```

3. **`POST /v1/editions/{id}/anchors:resolve`** recale un lot d'ancres :

   ```json
   { "anchors": [ { "key": "progress:42", "revision": 2, "seq": 120, "offset": 37, "quote": { … } } ] }
   → { "revision": 3, "results": [
         { "key": "progress:42", "status": "relocated", "seq": 118, "offset": 37 } ] }
   ```

   `status` : `unchanged` (même révision), `relocated` (extrait retrouvé dans
   la nouvelle révision, en partant de la position relative de l'ancienne),
   `approximate` (extrait introuvable, position proportionnelle) ou `lost`.

## 6. Recherche : implémentation

- Reprend `search.py` : alias Qdrant `chunks`, `query_points_groups`, filtres
  sur le payload, fusion RRF.
- Le modèle dense est **chargé une seule fois au démarrage**, puis « chauffé »
  par une requête factice. Mesuré avec Qwen3-Embedding-0.6B : **~50 ms par
  requête sur GPU** (RTX 3070), **~650 ms sur CPU**. Le GPU est donc
  conseillé pour l'API dès que la recherche par le sens compte. Le mode
  `words` (BM25) n'utilise pas le modèle (~60 ms).
- `HF_HUB_OFFLINE=1` une fois le modèle en cache : sinon le chargement
  interroge le Hub Hugging Face (~40 s de plus au démarrage).
- Connexions Postgres avec `jit=off` : la compilation JIT coûtait ~300 ms
  sur les requêtes récursives (courants, sections) pour un gain nul.
- Le texte des passages est lu dans Postgres **en une seule requête** pour
  tous les résultats, et non résultat par résultat comme dans la CLI.
- Contrôle de la phrase exacte (`quote`) : sur les candidats seulement, dans
  Postgres (`unaccent` + `lower`), donc pas d'index plein texte global sur
  les segments.
- `find` (recherche dans un livre) parcourt les segments d'une seule édition
  via l'index `(edition_id, seq)`, soit quelques milliers de lignes : rapide
  sans index dédié, et tenable même pour plusieurs centaines de milliers
  d'œuvres.
- `suggest` : `pg_trgm` sur `work_titles.title`, `works.title`,
  `persons.display_name`, `person_names.name`.

## 7. Performances

| Endpoint | Visé (p95) | Mesuré (36 éditions, GPU, à chaud) |
| --- | --- | --- |
| catalogue, fiche, toc, segments | < 50 ms | 5–40 ms |
| `find` dans un livre | < 150 ms | ~40 ms |
| `search` `words` | < 200 ms | ~60 ms |
| `search` `theme` | < 200 ms | 80–300 ms (premières requêtes d'une forme : jusqu'à 600 ms) |
| `search` `quote` + `show_languages` | < 350 ms | ~430 ms |
| `anchors:resolve` (texte changé) | — | ~100 ms |

Mesures sur un corpus de test ; à refaire à l'échelle visée (centaines de
milliers d'œuvres), en particulier `works?q=` et `suggest` (index
trigrammes) et le tri par titre affiché (calculé à la volée).

## 8. Implémentation

```
swann/
├── core/swann_core/   # partagé : embed (Qwen3, BM25), qdrant.py, s3.py
├── ingest/           # dépend de swann_core
└── corpus-api/       # FastAPI, dépend de swann_core
    └── swann_api/
        ├── main.py       # application, cycle de vie (pool psycopg, Qdrant, S3, encodeur)
        ├── auth.py       # vérification JWT Keycloak (JWKS), rôles, droits `access`
        ├── engine.py     # index actif (alias Qdrant) et encodeur de requêtes
        ├── sql.py        # fragments SQL : libellés selon la langue, visibilité
        ├── text.py       # normalisation, recherche dans le texte, ancres
        ├── schemas.py    # modèles Pydantic = contrat OpenAPI
        └── routers/      # catalog, editions, search, alignment, changes, meta
```

Le SQL reste écrit à la main (psycopg, pool asynchrone), dans le style de
l'ingestion. Les modèles Pydantic sont la source du contrat OpenAPI.

Le payload Qdrant porte `access` : la recherche filtre les éditions
`restricted` côté Qdrant. Quand une fiche change de droits, `swann extract`
met à jour le payload sans réindexer (`swann index sync-payload` pour
rattraper un index existant).

## 9. Changements de schéma préalables

Faits (migration `db/migrations/20261002120000_corpus_api.sql` et ingestion) :

| # | Changement | Raison |
| --- | --- | --- |
| 1 | Supprimer `11_users.sql`. Dans `12_alignment_reviews.sql` : `segment_alignments.created_by` → `created_by_sub TEXT`, `alignment_reviews.reviewer_id` → `reviewer_sub TEXT`, sans clé étrangère | les comptes sont dans Keycloak (§2) |
| 2 | `editions.access` (`open` / `excerpt` / `restricted`, défaut `restricted`), renseigné depuis `work.toml` (`access = "open"` par édition) | droits sur les textes (§2) ; par défaut, rien n'est publié par erreur |
| 3 | `editions.revision INTEGER NOT NULL DEFAULT 1`, incrémenté par l'ingestion à chaque remplacement du texte | ancres et cache (§3, §5) |
| 4 | Table `corpus_events (id BIGSERIAL, at, type, work_id, edition_id, data JSONB)`, alimentée par l'ingestion | flux `/v1/changes` |
| 5 | Fonction `section_ranges(edition_id)` (`seq_start` / `seq_end` d'une section, descendants compris) | `toc` |
| 6 | Extensions `pg_trgm` et `unaccent`, index trigrammes sur titres et noms | `suggest`, `find`, contrôle des citations |
| 7 | Outil de migration (dbmate) à la place de `docker-entrypoint-initdb.d` | faire évoluer une base qui contient des données qu'on ne peut plus effacer |
| 8 | `swann extract` envoie les EPUB dans MinIO ; bucket `avatars` supprimé | EPUB bruts servis par l'API ; les avatars relèvent des apps |

## 10. Hors périmètre de la v1

- Création des index vectoriels : CLI `swann index create`.

L'administration du corpus (supervision, dépôts, fiches, structure,
corbeille, atelier d'alignement) est sous `/v1/admin` (rôle `corpus:admin`,
`corpus:review` pour l'atelier) : voir `docs/console.md` §9.
- Toute donnée utilisateur (progression, favoris, collections, surlignages) :
  bases des applications.
- Recherche fédérée hors du corpus.
