---
title: Console d'administration
group: applications
summary: L'application des administrateurs — dépôt et classement automatique des EPUB, supervision en direct, qualité, fiches et structure, atelier d'alignement.
links: [worker, corpus-api, authentification, alignement, ingestion, architecture]
---

# Console d'administration (`console/`)

Application web réservée aux administrateurs du corpus. Comme la liseuse,
elle passe **uniquement par la Corpus API** ; c'est l'API (et le
[worker](#worker)) qui écrivent dans Postgres, Qdrant et MinIO.

> État : côté serveur réalisé et testé (migrations, worker, qualité,
> classement, `/v1/admin/*`). Application : vue d'ensemble, dépôt, tâches,
> qualité, œuvres, index, historique, corbeille ; le reste est en cours.

## Architecture

```mermaid
flowchart LR
  nav["Navigateur"] --> pages["Pages React"]
  subgraph console["console/ (Next.js, serveur)"]
    pages
    a["/api/auth/*<br/>Better Auth"]
    p["/api/corpus/*<br/>proxy BFF"]
  end
  a -- "OIDC (swann-console)" --> kc["Keycloak"]
  p -- "JWT" --> api["corpus-api<br/>/v1 et /v1/admin"]
  api -- "SSE /v1/admin/stream" --> p
  api --> pg[("Postgres<br/>jobs, qualité, corpus_events")]
  api --> inbox[("MinIO inbox")]
  pg -. "pg_notify" .-> api
  w["ingest-worker"] -. "LISTEN jobs" .-> pg
```

- **Temps réel** : l'API écoute `jobs` et `corpus_events` (`LISTEN`), publie
  un flux SSE `/v1/admin/stream`, relayé par le BFF ; la console invalide les
  requêtes TanStack Query concernées.
- **Dépôts** : le fichier passe par le BFF puis l'API, qui l'écrit dans le
  bucket `inbox` (pas d'URL présignée : MinIO reste invisible).

## Dépôt et classement automatique

```mermaid
sequenceDiagram
  autonumber
  actor Ad as Admin
  participant C as Console
  participant A as Corpus API
  participant S as MinIO inbox
  participant P as Postgres
  participant W as Worker (GPU)
  Ad->>C: glisse des EPUB (+ options : œuvre, langue, access)
  C->>A: POST /v1/admin/uploads (multipart)
  A->>A: zip, OPF, DRM, taille, sha256
  alt doublon exact
    A-->>C: déjà présent
  else
    A->>S: inbox/sha256.epub
    A->>P: INSERT jobs (kind ingest) → pg_notify
    P-->>W: réveil
    W->>W: identify
    alt indices concordants
      W->>W: extract → quality → index → align
      W->>P: succeeded
    else doute
      W->>P: needs_review + rapport (candidats, scores)
      P-->>C: SSE
      Ad->>C: choisit (rattacher à X, nouvelle œuvre)
      C->>A: POST /v1/admin/jobs/{id}/resolve
      A->>P: job repris
    end
  end
```

Par décision, **chaque dépôt est validé à la main** pour l'instant
(`IDENTIFY_AUTO=false`).

### Identification de l'œuvre

```mermaid
flowchart TB
  e["EPUB déposé"] --> lang["1 · Langue<br/>dc:language + détection sur le texte"]
  lang --> aut["2 · Auteur<br/>dc:creator vs persons + person_names<br/>(translittérations, trigrammes)"]
  aut --> tit["3 · Titre<br/>vs titres des œuvres de l'auteur"]
  tit --> wd{"auteur ou titre<br/>inconnu ?"}
  wd -- "oui" --> wiki["4 · Wikidata<br/>QID, titre et langue originaux (cache)"]
  wd -- "non" --> cont
  wiki --> cont["5 · Contenu<br/>LaBSE : paragraphes du dépôt vs édition existante"]
  cont --> trl["6 · Traducteurs<br/>distinguer deux traductions d'une langue"]
  trl --> dec{"décision"}
  dec --> att["rattacher à l'œuvre"]
  dec --> new["créer l'œuvre (+ personne)"]
  dec --> rev["needs_review"]
```

## Modifier les fiches et la structure

| Objet | Modifiable | Effet |
| --- | --- | --- |
| Œuvre | titres, auteurs, année, langue, courants, QID ; fusion ; corbeille | `sync_payload` si un champ copié dans Qdrant change |
| Édition | titre, langue, original, éditeur, traducteurs, `access` ; déplacement vers une autre œuvre ; corbeille, purge | `sync_payload`, `align` des œuvres concernées |
| Personne | noms par langue, dates, QID ; fusion | variantes réutilisées par le classement |
| Courant | arbre, œuvres rattachées | — |
| Structure | type, matter, titre, numéro ; déplacer, scinder, fusionner des sections | job `reprocess` : redécoupage, réindexation, réalignement |

Le **texte** n'est pas modifiable : il vient de l'EPUB ; pour le corriger, on
redépose un EPUB (nouvelle révision de la même édition).

## Qualité des ouvrages

`edition_quality` combine des mesures **au parsing** (méthode de structure,
avertissements, langue détectée) et **calculées depuis la base** (chapitres,
notes orphelines, longueur des segments, part hors corps, indexation,
alignement). Elles produisent des **signaux** et un score de 0 à 100 :

| Signal | Condition |
| --- | --- |
| Pas de table des matières | structure déduite des titres |
| Langue incohérente | détectée ≠ fiche |
| Corps vide ou maigre | peu de segments, ou > 30 % hors corps |
| Segments anormaux | > 5 000 caractères, ou médiane < 40 |
| Notes cassées | appels sans note, notes jamais appelées |
| Chapitres suspects | 0 ou 1 chapitre, sections vides |
| Pas indexée / pas alignée | absente de l'index actif, alignement douteux |
| Incohérence de stockage | EPUB absent, points Qdrant ≠ chunks |

Un signal peut être marqué « vu, accepté » (`quality_acks`).

## Pages

| Chemin | Contenu |
| --- | --- |
| `/` | santé, entonnoir (déposés → extraits → indexés → alignés), tâches, échecs |
| `/upload` | dépôt et suivi |
| `/jobs`, `/jobs/[id]` | file, historique, détail, résolution d'un `needs_review` |
| `/quality` | éditions, pastilles, score |
| `/works`, `/works/[id]` | fiches éditables, éditions, fusion |
| `/indexes` | index vectoriels, activation |
| `/events` | historique des modifications |
| `/trash` | corbeille (purge après 30 jours) |
| `/alignment/*` | atelier d'alignement (en cours) |
