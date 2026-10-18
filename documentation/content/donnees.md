---
title: Modèle de données
group: architecture
summary: Schéma Postgres du corpus — œuvres, éditions, texte canonique, chunks, alignements, index vectoriels, exploitation — et migrations dbmate.
links: [architecture, ingestion, alignement, recherche, revisions, worker]
---

# Modèle de données

Le schéma de la base `swann` évolue par **migrations dbmate**
(`db/migrations/`), appliquées par le service `migrate` à chaque démarrage.
Une base n'est jamais recréée : toute évolution passe par une nouvelle
migration (`-- migrate:up` / `-- migrate:down`).

## Œuvres et éditions

Une **œuvre** (`works`) est l'entité abstraite, indépendante de la langue.
Une **édition** (`editions`) en est une version concrète : un fichier, une
langue, un traducteur, un EPUB. Les traductions d'un même livre sont donc
plusieurs éditions d'une même œuvre.

```mermaid
erDiagram
  persons ||--o{ person_names : "variantes par langue"
  persons ||--o{ work_authors : ""
  works ||--o{ work_authors : ""
  works ||--o{ work_titles : "titres par langue"
  works ||--o{ work_movements : ""
  movements ||--o{ work_movements : ""
  movements ||--o{ movement_labels : ""
  movements |o--o{ movements : "parent"
  works ||--o{ editions : ""
  editions ||--o{ edition_contributors : ""
  persons ||--o{ edition_contributors : "traducteur, éditeur…"

  works {
    uuid id PK
    text slug
    text title
    text original_language
    int first_published_year
    text wikidata_id
    timestamptz deleted_at
  }
  editions {
    uuid id PK
    uuid work_id FK
    text language
    bool is_original
    text access "open | excerpt | restricted"
    int revision
    text sha256
    text epub_object_key
    timestamptz deleted_at
  }
  persons {
    uuid id PK
    text display_name
    int birth_year
    int death_year
    text wikidata_id
  }
```

## Texte canonique

Le texte d'une édition est stocké **une seule fois**, en `segments` :
paragraphes ordonnés par `seq`, sans chevauchement, typés (titre, prose,
vers, réplique + personnage, didascalie, épigraphe, citation, note). La
structure est un arbre de `sections`.

```mermaid
erDiagram
  editions ||--o{ sections : "arbre"
  sections |o--o{ sections : "parent"
  editions ||--o{ segments : ""
  sections ||--o{ segments : ""
  segments ||--o{ note_refs : "appel de note"
  editions ||--o{ page_breaks : "pagination papier"
  editions ||--o{ chunks : ""
  editions ||--o{ edition_indexings : ""
  vector_indexes ||--o{ edition_indexings : ""

  sections {
    uuid id PK
    uuid parent_id FK
    text kind "part, chapter, act, poem…"
    text matter "front | body | back"
    text label
    int number
  }
  segments {
    uuid id PK
    uuid edition_id FK
    int seq
    text kind "prose, verse, speech…"
    text speaker
    text text
    text markup "italique, gras…"
    int char_start
  }
  chunks {
    uuid id PK "= id du point Qdrant"
    uuid edition_id FK
    text chunker_version
    int segment_start_seq
    int segment_end_seq
  }
  vector_indexes {
    uuid id PK
    text collection
    text dense_model
    text sparse_model
    text chunker_version
    text status "building | active | retired"
  }
```

- `chunks` ne contient **aucun texte** : ce sont des fenêtres
  `[segment_start_seq, segment_end_seq]` (vue `chunk_texts`). Plusieurs découpages
  (`chunker_version`) coexistent : on peut re-chunker sans toucher au texte.
- `vector_indexes` : une collection Qdrant par modèle d'embedding, une seule
  `active` à la fois. `edition_indexings` suit le remplissage édition par
  édition.
- Vues utiles : `edition_texts` (le livre complet), `section_paths`
  (« Première partie > Chapitre III »), fonction `section_ranges()`.

## Alignements

```mermaid
erDiagram
  works ||--o{ work_units : "pivot"
  work_units ||--o{ segment_alignments : ""
  segments ||--o{ segment_alignments : ""
  editions ||--|| edition_alignments : "qualité"
  segment_alignments ||--o{ alignment_reviews : "verdicts humains"

  work_units {
    uuid id PK
    uuid work_id FK
    int seq
  }
  segment_alignments {
    uuid segment_id FK
    uuid unit_id FK
    real score
    text method "swann-labse-v1 | manual"
    text created_by_sub
  }
  edition_alignments {
    uuid edition_id PK
    real aligned_ratio
    real mean_score
    text status "pending | reliable | doubtful | rejected"
  }
  alignment_reviews {
    uuid id PK
    text verdict "correct | incorrect | partial"
    bool is_sample
    text reviewer_sub
  }
```

Les `work_units` sont le **pivot** : une unité par segment du corps de
l'édition de référence. Passer du français à l'anglais =
segments FR → unités → segments EN. Détails : [Alignement](#alignement).

## Exploitation

```mermaid
erDiagram
  jobs |o--o{ jobs : "parent (lot)"
  jobs ||--o{ ingestions : ""
  editions ||--o{ ingestions : ""
  editions ||--o| edition_quality : ""
  editions ||--o{ quality_acks : ""
  workers |o--o| jobs : "tâche en cours"

  jobs {
    uuid id PK
    job_kind kind
    job_status status
    jsonb params
    jsonb progress
    jsonb result
    text created_by_sub
    timestamptz heartbeat_at
  }
  corpus_events {
    bigserial id PK
    text type
    uuid work_id
    uuid edition_id
    jsonb data
  }
  edition_quality {
    uuid edition_id PK
    text[] signals
    int score
  }
  workers {
    text id PK "hôte:pid"
    text device
    text gpu_name
    timestamptz seen_at
  }
```

- `corpus_events` : journal des changements, écrit par `corpus_emit()` **dans
  la même transaction** que la modification ; servi par `/v1/changes`.
- `jobs` / `workers` : file de tâches et présence des workers
  (voir [Worker et tâches](#worker)).
- `edition_quality` / `quality_acks` : signaux de qualité par édition
  (voir [Console](#console)).
- `wikidata_cache` : réponses Wikidata du classement automatique.

## Migrations

| Fichier | Contenu |
| --- | --- |
| `00`–`10` | schéma initial : extensions, personnes, œuvres, courants, éditions, sections et segments, chunks, alignements, index vectoriels, ingestions |
| `11_users`, `12_alignment_reviews` | relectures humaines (les comptes sont ensuite retirés au profit de Keycloak) |
| `20261002120000_corpus_api` | `editions.access`, `editions.revision`, `corpus_events`, `section_ranges()`, `pg_trgm` + `unaccent` |
| `20261003120000_console` | `jobs` (+ `pg_notify`), `ingestions.job_id/stage`, `edition_quality`, `quality_acks`, corbeille (`deleted_at`), `wikidata_cache` |
| `20261003130000_workers` | table `workers` (présence, GPU, modèles chargés) |

Les fichiers `00`–`12` sont figés : ils ne se modifient plus.
