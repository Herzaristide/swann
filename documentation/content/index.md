---
title: Accueil
group: introduction
summary: Swann, bibliothèque multilingue d'œuvres littéraires avec recherche par le sens, alignement des traductions, liseuse et console d'administration.
links: [architecture, donnees, ingestion, corpus-api, liseuse, console, deploiement, developpement]
---

# Swann

Swann est une **bibliothèque d'œuvres littéraires multilingue**. Un même
livre existe en plusieurs langues et traductions (le *Crime et Châtiment*
russe, la traduction française de Markowicz, l'anglaise de Garnett…) : Swann
les range sous une seule **œuvre**, les rend **cherchables par le sens**
dans toutes les langues, et **aligne** leurs paragraphes pour passer d'une
traduction à l'autre sans perdre sa place.

## Ce que fait le système

| Brique | Rôle |
| --- | --- |
| **Ingestion** (`ingest/`, CLI `swann`) | lit les EPUB, en extrait la structure et le texte canonique, vectorise, aligne les traductions |
| **Corpus** (Postgres + Qdrant + MinIO) | métadonnées et texte, index vectoriels, fichiers EPUB |
| **Corpus API** (`corpus-api/`, FastAPI) | seule porte d'entrée des applications : catalogue, texte, recherche, alignements, flux des changements, administration |
| **Keycloak** | identité, rôles `corpus:read` / `corpus:review` / `corpus:admin` |
| **Liseuse** (`reader/`, Next.js PWA) | lire, chercher, lecture parallèle, hors ligne |
| **Console** (`console/`, Next.js) | déposer des livres, superviser, qualité, fiches, atelier d'alignement |
| **Worker** (`swann worker`, GPU) | exécute la file de tâches : dépôts, retraitements, alignements, corbeille |

## Vue d'ensemble

```mermaid
flowchart LR
  books[("books/ + work.toml<br/>ou dépôt d'EPUB")] --> ingest["Ingestion<br/>CLI swann / worker GPU"]
  ingest --> corpus[("Corpus<br/>Postgres · Qdrant · MinIO")]
  corpus --> api["Corpus API<br/>FastAPI /v1"]
  api --> reader["Liseuse<br/>Next.js PWA"]
  api --> console["Console<br/>Next.js"]
  kc["Keycloak"] -. "jetons JWT" .-> api
  kc -. "OIDC" .-> reader
  kc -. "OIDC" .-> console
  console -- "jobs" --> api
```

## Principes structurants

- **Œuvre ≠ édition** : la recherche se fait sur les œuvres, chaque
  traduction est une édition avec son texte, sa langue, ses traducteurs.
- **Corpus découplé** : les applications ne voient jamais Postgres, Qdrant
  ou MinIO, uniquement l'API versionnée `/v1`. On peut re-chunker ou changer
  de modèle d'embedding sans casser les clients.
- **Aucune donnée utilisateur dans le corpus** : l'API ne connaît que le
  `sub` Keycloak ; progression, favoris, collections vivent dans la base de
  chaque application.
- **Positions stables** : une position dans un texte est une ancre
  `(edition_id, revision, seq, offset, quote)`, recalée quand le texte change.
- **Droits par édition** : `open`, `excerpt` ou `restricted` (défaut) — une
  traduction sous droits n'est jamais publiée par oubli.

## Par où commencer

- Comprendre les briques et leurs échanges → [Architecture](#architecture).
- Le modèle de données → [Données](#donnees).
- Du fichier EPUB à l'index → [Ingestion](#ingestion).
- L'API et ses contrats → [Corpus API](#corpus-api).
- Lancer la pile → [Déploiement](#deploiement) et [Développement](#developpement).

> Astuce : `⌘K` (ou `Ctrl+K`) pour chercher dans toute la documentation.
