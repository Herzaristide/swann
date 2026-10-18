---
title: Architecture
group: architecture
summary: Les composants de Swann, leurs dépendances, qui écrit où, et le modèle BFF des applications web.
links: [index, donnees, corpus-api, authentification, liseuse, console, worker, deploiement]
---

# Architecture

Swann est découpé en trois couches : le **corpus** (trois bases de données),
la **Corpus API** qui l'expose, et les **applications** qui ne passent que
par l'API. Les traitements lourds (EPUB, embeddings, alignement) sont faits
par l'**ingestion**, en ligne de commande ou par le worker.

## Vue des conteneurs

```mermaid
flowchart TB
  subgraph clients["Navigateurs"]
    b1["Lecteur<br/>(PWA, IndexedDB)"]
    b2["Administrateur"]
  end

  subgraph apps["Applications (Next.js 16, BFF)"]
    reader["reader :3000<br/>liseuse"]
    rworker["reader-worker<br/>suit /v1/changes"]
    console["console :3001<br/>administration"]
  end

  subgraph identity["Identité"]
    kc["Keycloak :8080<br/>realm swann"]
  end

  subgraph core["Corpus"]
    api["corpus-api :8000<br/>FastAPI /v1 et /v1/admin"]
    pg[("Postgres :5432<br/>base swann")]
    qd[("Qdrant :6333<br/>alias chunks")]
    s3[("MinIO :9000<br/>buckets books, inbox")]
  end

  subgraph processing["Traitements"]
    cli["CLI swann<br/>(à la demande)"]
    iw["ingest-worker<br/>swann worker, GPU"]
  end

  subgraph appdb["Bases des applications"]
    rdb[("Postgres<br/>base reader")]
    cdb[("Postgres<br/>base console")]
  end

  b1 -- "cookie de session" --> reader
  b2 -- "cookie de session" --> console
  reader -- "JWT" --> api
  rworker -- "JWT" --> api
  console -- "JWT + SSE" --> api
  reader --> rdb
  rworker --> rdb
  console --> cdb
  reader -. "OIDC" .-> kc
  console -. "OIDC" .-> kc
  api -. "JWKS" .-> kc

  api --> pg
  api --> qd
  api --> s3
  cli --> pg & qd & s3
  iw --> pg & qd & s3
  iw -. "LISTEN jobs" .-> pg
```

## Qui écrit où

| Composant | Postgres `swann` | Qdrant | MinIO | Base propre |
| --- | --- | --- | --- | --- |
| CLI `swann` / worker | œuvres, éditions, texte, chunks, alignements, qualité, `corpus_events` | points des chunks, payload | EPUB (`books/`) | — |
| Corpus API | verdicts d'alignement, fiches et structure (admin), file `jobs` | lecture (+ activation d'alias) | dépôts dans `inbox` ; lecture des EPUB | — |
| Liseuse | — | — | — | `reader` : préférences, progression, favoris, collections |
| Console | — | — | — | `console` : sessions |
| Keycloak | — | — | — | `keycloak` : comptes, realm |

Les quatre bases Postgres (`swann`, `reader`, `console`, `keycloak`) vivent sur
le **même serveur** Postgres mais restent indépendantes : aucune jointure
entre elles, chaque application n'accède qu'à la sienne.

## Le corpus, trois stockages complémentaires

```mermaid
flowchart LR
  subgraph pg["Postgres — source de vérité"]
    meta["métadonnées<br/>œuvres, éditions, personnes, courants"]
    text["texte canonique<br/>sections, segments, notes, pages"]
    chunks["chunks<br/>fenêtres de segments, sans texte dupliqué"]
    align["alignements<br/>work_units, segment_alignments"]
    ops["exploitation<br/>jobs, ingestions, qualité, corpus_events"]
  end
  subgraph qd["Qdrant — index"]
    coll["une collection par modèle<br/>dense Qwen3 + sparse BM25"]
    alias["alias chunks → index actif"]
  end
  subgraph s3["MinIO — fichiers"]
    bk["books/editions/sha256.epub"]
    ib["inbox/ (dépôts en attente)"]
  end
  chunks -- "chunks.id = id du point" --> coll
  alias --> coll
  meta -- "epub_object_key" --> bk
```

- **Postgres** est la référence : le texte n'est stocké qu'une fois
  (`segments`), les chunks ne sont que des intervalles de `seq`.
- **Qdrant** est un index reconstructible : chaque point porte l'id du chunk
  et un payload recopié de Postgres (langue, œuvre, droits `access`…).
- **MinIO** garde l'EPUB d'origine, servi en flux par l'API ; il n'est
  jamais exposé aux navigateurs.

## Le patron BFF des applications

La liseuse et la console suivent le même modèle *Backend For Frontend* : le
navigateur ne parle **qu'au serveur Next.js**, qui détient les jetons.

```mermaid
sequenceDiagram
  autonumber
  participant N as Navigateur
  participant B as Serveur Next.js (BFF)
  participant K as Keycloak
  participant A as Corpus API
  N->>B: GET /api/corpus/v1/works (cookie httpOnly)
  B->>B: session Better Auth → jeton d'accès
  alt jeton expiré
    B->>K: refresh_token
    K-->>B: nouveau jeton
  end
  B->>A: GET /v1/works (Authorization: Bearer …)
  A->>A: vérifie signature (JWKS), audience corpus-api, rôles
  A-->>B: 200 JSON
  B-->>N: 200 JSON
```

Conséquences : pas de CORS à ouvrir sur l'API, aucun jeton dans le
navigateur, proxy en **liste blanche** des chemins autorisés.

## Paquets du dépôt

```mermaid
flowchart BT
  core["core/swann_core<br/>embed (Qwen3, BM25), qdrant.py, s3.py"]
  ingest["ingest/swann_ingest<br/>CLI swann, worker"]
  api["corpus-api/swann_api<br/>FastAPI"]
  reader["reader/<br/>Next.js"]
  console["console/<br/>Next.js"]
  ingest --> core
  api --> core
  reader -. "client généré depuis /v1/openapi.json" .-> api
  console -. "client généré depuis /v1/openapi.json" .-> api
```

Les paquets Python forment un **espace de travail uv** (un seul `.venv` à la
racine) ; `swann_core` est partagé pour que l'ingestion et l'API encodent les
textes exactement de la même façon. Les applications TypeScript génèrent leur
client typé (`openapi-typescript` + `openapi-fetch`) depuis le contrat
OpenAPI de l'API.

Voir aussi : [Données](#donnees), [Corpus API](#corpus-api),
[Authentification](#authentification), [Déploiement](#deploiement).
