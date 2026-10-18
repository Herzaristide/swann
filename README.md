# Swann

Deuxième version (réécriture) de `book-ingest` : bibliothèque de livres avec
recherche vectorielle et RAG.

- **PostgreSQL** — source de vérité des métadonnées (œuvres, éditions,
  personnes, courants, texte canonique, chunks, alignements, ingestions).
- **Qdrant** — base vectorielle des embeddings de chunks.
- **MinIO** — stockage des fichiers EPUB des éditions.

Ces trois bases forment le **corpus**. Les applications (liseuse, relecture
des alignements…) n'y accèdent que par la Corpus API (design :
[`docs/corpus-api.md`](docs/corpus-api.md)) ; les comptes utilisateurs sont
gérés par Keycloak et les données de chaque application dans sa propre base.

## Démarrage

```bash
cp .env.example .env          # ajuster les mots de passe
docker compose up -d --build  # toute la pile, voir ci-dessous
```

| Service | Adresse | Rôle |
| --- | --- | --- |
| `reader` | http://localhost:3000 | liseuse (utilisateur de dev : `lecteur` / `lecteur`) |
| `reader-worker` | — | suit `/v1/changes`, recale les progressions |
| `api` | http://localhost:8000/v1/docs | Corpus API (sur CPU par défaut) |
| `keycloak` | http://localhost:8080/admin | identité (realm `swann`) |
| `postgres` | localhost:5432 | bases `swann` (corpus), `keycloak`, `reader` |
| `qdrant` / `minio` | :6333 / :9000-9001 | index vectoriels / EPUB |
| `pgadmin` | http://localhost:5050 | visualisation du schéma |
| `migrate`, `databases` | — | migrations du corpus, création des bases (puis s'arrêtent) |

Ordre de démarrage géré par les `depends_on` : Postgres → migrations et
bases → Keycloak, Qdrant, MinIO → API (saine une fois le modèle chargé) →
liseuse (applique ses migrations) → worker. Seule l'ingestion reste une
commande à la demande (`docker compose run --rm ingest …`).

Modèles (Qwen3, LaBSE) : volume `hf_cache`, téléchargés au premier
démarrage ; pour réutiliser ceux de l'hôte :
`docker run --rm -v swann_hf_cache:/c -v ~/.cache/huggingface/hub:/src:ro alpine sh -c 'mkdir -p /c/hub && cp -a /src/. /c/hub/'`
puis `HF_HUB_OFFLINE=1` dans `.env` (démarrage plus rapide).

GPU (recherche « thème » ~50 ms au lieu de ~650–900 ms) :
`docker compose -f docker-compose.yml -f docker-compose.gpu.yml up -d`,
qui demande le NVIDIA Container Toolkit en mode CDI (NixOS :
`hardware.nvidia-container-toolkit.enable = true;`).

Vérifications :
ééé
```bash
docker compose ps
curl http://localhost:6333/healthz                    # Qdrant
docker compose exec postgres pg_isready -U swann       # Postgres
curl http://localhost:9000/minio/health/live          # MinIO
```

## Ingestion des livres

Le pipeline (`ingest/`, Python) se déroule en trois étapes indépendantes,
relançables sans doublon et qui reprennent là où elles se sont arrêtées :

```
books/ ──► check ──► extract ──────────► index ─────────────────► align
           (valide)  EPUB → Postgres     Postgres → Qdrant         Postgres → Postgres
                     CPU, parallèle      GPU : Qwen3 + BM25 (CPU)  GPU : LaBSE
                     1× par livre        1× par livre et par modèle  1× par œuvre
```

### Ranger les livres

Un dossier par œuvre, toutes ses éditions dedans, et une fiche `work.toml` :

```
books/
└── dostoievski-fiodor/
    └── crime-et-chatiment/
        ├── work.toml
        ├── ru.epub                  ← original
        ├── fr--markowicz.epub       ← <langue>--<traducteur>.epub
        └── en--garnett.epub
```

```toml
[work]
title = "Преступление и наказание"
original_language = "ru"
first_published_year = 1866
authors = ["Fiodor Dostoïevski"]   # même graphie dans toutes les fiches
movements = ["realism"]
wikidata = "Q160891"               # optionnel

[[editions]]
file = "ru.epub"
language = "ru"
original = true
access = "open"                    # domaine public : texte et EPUB servis par l'API

[[editions]]
file = "fr--markowicz.epub"
language = "fr"
translators = ["André Markowicz"]
publisher = "Actes Sud"
year = 1996
```

Noms de dossiers en minuscules sans accents, codes de langue BCP 47 (`fr`,
`en`, `pt-BR`…), un EPUB = une édition complète. `books/` est ignoré par git.

`access` = droits sur le texte de l'édition : `open` (texte intégral et EPUB
servis par l'API), `excerpt` (extraits courts dans la recherche) ou
`restricted` (métadonnées seulement, **défaut** : une traduction sous droits
n'est jamais publiée par oubli). Modifier une fiche puis relancer
`swann extract` met à jour l'édition sans relire l'EPUB.

### Environnement

**Avec Nix** (recommandé en développement, GPU utilisable directement) :

```bash
nix develop                       # python 3.12, uv, psql, dbmate, ruff + libs système
uv sync --all-packages            # un seul .venv à la racine (core + ingest + corpus-api)
uv run swann --help
uv run pytest ingest/tests        # tests (EPUB synthétiques, sans base)
```

Les paquets Python forment un espace de travail uv (`pyproject.toml` et
`uv.lock` à la racine) : `core/` (encodeurs, Qdrant, S3 : `swann_core`) est
partagé par `ingest/` et `corpus-api/`.

**Avec Docker** (image `swann-ingest`, service `ingest` du compose) :

```bash
docker compose run --rm ingest check
# GPU : NVIDIA Container Toolkit requis
# (NixOS : hardware.nvidia-container-toolkit.enable = true;)
docker compose -f docker-compose.yml -f docker-compose.gpu.yml run --rm ingest index run <collection>
```

### Commandes

```bash
swann prepare SRC OUT             # range un lot d'EPUB en vrac en OUT/books/ (work.toml,
                                 # persons.toml, Wikidata, droits, rapport des cas à vérifier)
swann check [--dump DIR]          # valide books/, lit chaque EPUB, vérifie la langue ;
                                 # --dump écrit le texte extrait (.txt) pour relecture
swann extract [--workers N]       # EPUB → œuvres, éditions, sections, segments, notes, pages
swann index create                # nouvel index (EMBEDDING_MODEL, découpage v1)
swann index run <collection>      # vectorise les éditions pas encore indexées
swann index activate <collection> # bascule l'alias de recherche vers cet index
swann index sync-payload          # recopie dans Qdrant les champs modifiables (droits `access`)
swann index list
swann align [--work auteur/oeuvre] # aligne les traductions (≥ 2 éditions)
swann status
swann search "un meurtre" [--mode theme|quote|words] [--lang fr] [--show en]
```

Modes de recherche : `theme` = sens seul (dense), `quote` = citation (sens +
mots exacts, fusion RRF), `words` = mots exacts seuls (BM25). `--show en`
affiche aussi le passage correspondant de l'édition anglaise, via
l'alignement.

Changer de modèle d'embedding : `swann index create --dense-model … --dim …`,
`swann index run …`, comparer avec `swann search --collection …`, puis
`swann index activate …`. L'ancien index reste interrogeable jusqu'à la bascule.

### Ce que fait l'extraction

- ordre de lecture = spine de l'OPF ; structure = table des matières (nav ou
  NCX), à défaut les titres `<h1>`–`<h6>` ;
- types de sections (partie, livre, chapitre, préface…) et numéros reconnus
  en plusieurs langues (« Chapitre III », « ЧАСТЬ ПЕРВАЯ », « Erstes Kapitel ») ;
  tables des matières aplaties de Gutenberg ré-imbriquées ;
- préliminaires / corps / annexes : landmarks, `epub:type`, puis position
  par rapport au premier chapitre ; seul le corps est vectorisé ;
- notes sorties du fil du texte, conservées en annexe et reliées à leur appel
  (balisage EPUB 3, Gutenberg, Calibre) ; auteur de la note deviné
  (« N.d.T. » → traducteur) ;
- italique / gras / exposants conservés dans `segments.markup`, vers avec
  leurs retours à la ligne, numéros de page papier (`pagebreak`) ;
- licence Project Gutenberg retirée ; EPUB protégés par DRM refusés.

## Corpus API

API HTTP en lecture sur le corpus (FastAPI, `corpus-api/`), seule porte
d'entrée des applications : catalogue, texte des éditions, EPUB bruts,
recherche (thème, citation, mots), alignements, flux des changements. Design
et contrat : [`docs/corpus-api.md`](docs/corpus-api.md) ; documentation
interactive sur `/v1/docs`, contrat OpenAPI sur `/v1/openapi.json`.

```bash
# développement : sans Keycloak (AUTH_DISABLED : tous les droits, aucun jeton vérifié)
AUTH_DISABLED=true HF_HUB_OFFLINE=1 uv run uvicorn swann_api.main:create_app --factory --reload
curl localhost:8000/v1/health
curl -X POST localhost:8000/v1/search -H 'content-type: application/json' \
     -d '{"q": "un homme qui se demande s'"'"'il a le droit de tuer", "mode": "theme"}'

uv run pytest corpus-api/tests    # unitaires + intégration sur une copie jetable de la base

# conteneur (démarré avec la pile ; GPU conseillé : ~50 ms par requête contre ~650 ms sur CPU)
docker compose -f docker-compose.yml -f docker-compose.gpu.yml up -d api
```

- **Authentification** : jetons JWT Keycloak (`OIDC_ISSUER`, audience
  `corpus-api`), rôles `corpus:read`, `corpus:review`, `corpus:admin` (rôles
  du client `corpus-api` ou du realm). Le client Keycloak doit ajouter
  l'audience `corpus-api` aux jetons (mapper « Audience »). En conteneur,
  `OIDC_DISCOVERY_URL` (`http://keycloak:8080/realms/swann`) sert à lire les
  clés, l'émetteur attendu restant l'URL publique.
- **Droits sur les textes** : une édition `restricted` (défaut) n'est visible
  que par `corpus:admin` ; `excerpt` = extraits courts dans la recherche ;
  `open` = texte intégral et EPUB.
- **Encodeur** : le modèle de l'index actif est chargé au démarrage (GPU si
  disponible, `API_DEVICE`) ; si `swann index activate` change de modèle, l'API
  le recharge d'elle-même (vérification toutes les 30 s).
- Les tests d'intégration copient la base `swann` dans `swann_api_test`
  (`CREATE DATABASE … TEMPLATE`) : aucune session ne doit être ouverte sur
  `swann` pendant les tests, sinon ils sont ignorés.

## Keycloak (identité)

```bash
docker compose up -d keycloak     # crée les bases keycloak et reader, importe le realm
# console : http://localhost:8080/admin (KEYCLOAK_ADMIN / KEYCLOAK_ADMIN_PASSWORD)
```

Le realm `swann` est importé au premier démarrage depuis
`keycloak/import/swann-realm.json`, puis ignoré s'il existe déjà : pour
réappliquer le fichier, supprimer le realm dans la console (ou la base
`keycloak`) et redémarrer.

| Élément | Rôle |
| --- | --- |
| client `corpus-api` | porte les rôles `corpus:read`, `corpus:review`, `corpus:admin` ; audience des jetons |
| client `swann-reader` | liseuse (BFF Next.js) : code + PKCE pour les lecteurs, compte de service `corpus:read` ; secret `READER_CLIENT_SECRET` |
| rôle par défaut | tout nouvel inscrit reçoit `corpus:read` |
| utilisateur `lecteur` | **développement** : mot de passe `KEYCLOAK_DEV_USER_PASSWORD`, tous les rôles `corpus:*` |

Les jetons portent l'émetteur public `KEYCLOAK_URL` ; les conteneurs
joignent Keycloak par `http://keycloak:8080` (backchannel dynamique).
Plan et état de la liseuse : [`docs/reader.md`](docs/reader.md).

Thème de connexion : `keycloak/themes/swann` (couleurs de la liseuse), monté
dans le conteneur et activé par `loginTheme` dans le realm. Sur un realm déjà
importé : `kcadm.sh update realms/swann -s loginTheme=swann`.

## Liseuse (reader/)

Application Next.js 16 (PWA) qui lit le corpus **uniquement via la Corpus
API** et garde les données de ses lecteurs (préférences, progression,
favoris, collections) dans sa propre base `reader`. Connexion par Keycloak
(Better Auth), jetons gardés côté serveur (BFF).

```bash
nix develop                        # Node 22, pnpm, navigateurs Playwright
cd reader
cp .env.example .env.local         # valeurs de développement
pnpm install
pnpm db:migrate                    # base `reader` (Drizzle)
pnpm dev                           # http://localhost:3000 (Corpus API sur :8000)

pnpm gen:api                       # régénère lib/api/schema.d.ts depuis /v1/openapi.json
pnpm lint && pnpm typecheck && pnpm test
pnpm build && pnpm test:e2e        # bout en bout : build de production, base jetable reader_e2e
pnpm worker --once                 # synchronisation /v1/changes (recalage des progressions)
```

En conteneur, la liseuse démarre avec le reste de la pile (`docker compose
up -d`) ; ses migrations s'appliquent au démarrage, le worker est le service
`reader-worker`. Après une modification du code : `docker compose up -d --build reader reader-worker`.

Le client Keycloak `swann-reader` n'accepte que `READER_URL`
(`http://localhost:3000` par défaut) : les tests de bout en bout ont besoin
du port 3000 libre (`docker compose stop reader` avant `pnpm dev` ou `pnpm test:e2e`).

## Console d'administration (console/)

Supervision, dépôt d'EPUB (classés automatiquement dans leur œuvre, puis
validés), qualité des éditions, fiches et structure, corbeille, atelier
d'alignement. Plan et décisions : `docs/console.md`.

```bash
docker compose up -d                 # console → http://localhost:3001 (utilisateur de dév. : lecteur / lecteur)
docker compose logs -f ingest-worker # worker : dépôts, retraitements, alignements, corbeille
# GPU pour le worker (CDI NVIDIA requis) :
docker compose -f docker-compose.yml -f docker-compose.gpu.yml up -d ingest-worker

# Sans Docker
swann worker                          # même worker, sur l'hôte (GPU direct)
swann quality --reparse               # (re)calcule la qualité de toutes les éditions
cd console && pnpm dev               # console en développement (port 3001)
```

La base est la référence des fiches : `swann extract` ne réécrit plus une
fiche déjà en base (`--overwrite-metadata` pour forcer le `work.toml`).

## Schéma Postgres

Le schéma évolue par **migrations** ([dbmate](https://github.com/amacneil/dbmate)),
dans `db/migrations/`, appliquées dans l'ordre des noms de fichiers. Le
service `migrate` du compose les applique à chaque `docker compose up`, puis
s'arrête. En développement (`nix develop`) :

```bash
dbmate status                    # migrations appliquées / en attente
dbmate up                        # applique les migrations en attente
dbmate new <nom>                 # crée db/migrations/<horodatage>_<nom>.sql
```

Une base n'est jamais recréée pour changer le schéma : toute modification
passe par une nouvelle migration (`-- migrate:up` / `-- migrate:down`). Les
fichiers `00`–`12` sont le schéma initial ; ils ne se modifient plus.

| Fichier | Rôle |
| --- | --- |
| `00_extensions.sql` | Extension `pgcrypto` + fonction `set_updated_at()` |
| `01_persons.sql` | Personnes (auteurs, traducteurs…) + variantes de nom par langue |
| `02_works.sql` | Œuvres : entité abstraite, indépendante de la langue + titres par langue |
| `03_work_authors.sql` | Association n-n œuvres ↔ auteurs |
| `04_movements.sql` | Courants littéraires (hiérarchiques, libellés par langue) ↔ œuvres |
| `05_editions.sql` | Éditions : version concrète d'une œuvre (fichier, langue, éditeur, EPUB) + contributeurs (traducteurs…) |
| `06_sections_segments.sql` | Structure (parties, chapitres, actes, poèmes… en arbre) + texte canonique (paragraphes typés : prose, vers, répliques…) + notes + pagination papier + vues `edition_texts` et `section_paths` |
| `07_chunks.sql` | Fenêtres de segments, versionnées par découpage (`chunks.id` = id du point Qdrant) + vue `chunk_texts` |
| `08_alignments.sql` | Alignement des traductions d'une même œuvre (pivot `work_units`) + qualité par édition (`edition_alignments`) |
| `09_vector_indexes.sql` | Registre des collections Qdrant (modèle, découpage, statut) + avancement par édition |
| `10_ingestions.sql` | Suivi des exécutions du pipeline (par édition et par index) |
| `11_users.sql` | Utilisateurs (supprimés par la migration suivante) |
| `12_alignment_reviews.sql` | Vérifications et corrections humaines des alignements (taux d'erreur, historique) |
| `20261002120000_corpus_api.sql` | Préparation de la Corpus API : plus de comptes (relecteur = `sub` Keycloak), droits `editions.access`, `editions.revision`, journal `corpus_events`, `section_ranges()`, recherche approchée (`pg_trgm`, `unaccent`, `search_key()`) |

### Œuvres vs éditions

Un même livre peut exister en plusieurs langues/traductions (ex: "Le Petit
Prince" en français et en anglais). Ces versions ne doivent **pas** être des
livres distincts : elles partagent la même **œuvre** (`works`), mais chacune a
sa propre **édition** (`editions`) — son propre fichier, son propre `sha256`,
sa propre langue, son propre traducteur, son propre EPUB.

La recherche se fait **sur les œuvres** : toutes les éditions sont indexées
dans Qdrant (indispensable pour retrouver une citation dans la langue où elle
est collée), et les résultats sont regroupés par `work_id`.

```
persons ──< work_authors >── works ──< work_movements >── movements
   │                          │  │
   │                          │  └──< work_units ──< segment_alignments
   │                          │                              │
   └──< edition_contributors ─┴──< editions ──< segments ────┘
                                     │  │          ▲
                                     │  └──< sections
                                     ├──< chunks (fenêtres de segments = points Qdrant)
                                     ├──< edition_indexings >── vector_indexes
                                     └──< ingestions
```

- `works` : titre de référence, langue originale, date de première
  publication (tri chronologique), courants.
- `editions` : langue, original ou traduction (`is_original`), date de
  l'édition, traducteurs via `edition_contributors`.
- `sections` : arbre de la structure (tome > partie > chapitre, acte >
  scène, recueil > poème…) avec type, étiquette ("Chapitre III"), titre,
  préliminaires / corps / annexes, et chapitre correspondant dans l'édition
  de référence. Vue `section_paths` : "Première partie > Chapitre III".
- `segments` : **source de vérité du texte**. Paragraphes ordonnés sans
  chevauchement, typés (titre, prose, vers, réplique + personnage,
  didascalie, épigraphe, citation, note), en texte brut et avec la mise en
  forme en ligne (`markup` : italique…) ; le livre complet = vue
  `edition_texts`. Les notes sont rangées en annexe et reliées à leur appel
  par `note_refs` (auteur, traducteur ou éditeur).
- `chunks` : fenêtres (chevauchantes) de segments, sans texte dupliqué (vue
  `chunk_texts`) : on peut re-chunker et ré-indexer Qdrant sans toucher au
  texte. Plusieurs découpages (`chunker_version`) coexistent.
- `vector_indexes` : une collection Qdrant par modèle d'embedding. Changer de
  modèle = créer un index `building`, le remplir (backfill suivi par
  `edition_indexings`), basculer l'alias Qdrant `chunks`, puis passer le
  nouvel index en `active` et l'ancien en `retired` (un seul `active` à la
  fois, dans la même transaction).
- `work_units` + `segment_alignments` : passer d'un passage à sa traduction
  (segments FR → unités de l'œuvre → segments EN). Calculé à l'ingestion par
  un aligneur (Bertalign / Vecalign), chapitre par chapitre, sur une édition
  de référence (l'originale de préférence).
- `edition_alignments` : qualité de l'alignement de chaque édition (part de
  segments alignés, score moyen, statut `reliable` / `doubtful` / `rejected`),
  pour écarter les traductions abrégées ou douteuses des analyses.
- `alignment_reviews` : verdicts humains. Ceux tirés au hasard
  (`is_sample`) mesurent le taux d'erreur de l'aligneur ; une correction
  remplace le lien par un lien `manual` (auteur dans `created_by_sub`, le
  `sub` Keycloak) et le verdict reste en historique.
- `editions.revision` : incrémentée à chaque remplacement du texte (les
  segments sont recréés, leurs `seq` peuvent changer) ; les applications
  désignent une position par `(edition_id, revision, seq)`.
- `corpus_events` : journal des changements (œuvre créée, texte remplacé,
  alignement refait, index activé…), écrit par l'ingestion via
  `corpus_emit()` dans la même transaction ; servi aux applications par
  `/v1/changes`.

> Base créée avant dbmate (schéma appliqué par l'ancien `db/init/`) : marquer
> une fois le schéma initial comme appliqué, puis migrer normalement :
> ```bash
> psql "$DATABASE_URL" -c "CREATE TABLE IF NOT EXISTS schema_migrations (version VARCHAR(128) PRIMARY KEY);
>   INSERT INTO schema_migrations VALUES ('00'),('01'),('02'),('03'),('04'),('05'),('06'),
>   ('07'),('08'),('09'),('10'),('11'),('12') ON CONFLICT DO NOTHING;"
> dbmate up
> ```

## Visualiser le schéma (pgAdmin)

Un service `pgadmin` est inclus pour explorer et visualiser le schéma
(diagramme ER via l'**ERD Tool**) :

```bash
docker compose up -d pgadmin
```

Puis ouvrir http://localhost:5050 (identifiants `PGADMIN_DEFAULT_EMAIL` /
`PGADMIN_DEFAULT_PASSWORD` du `.env`). Le serveur **Swann** est préconfiguré
(host `postgres`) ; il suffit de saisir le mot de passe Postgres
(`POSTGRES_PASSWORD`) à la première connexion. Ensuite, clic droit sur la
base `swann` → **Generate ERD** pour afficher le diagramme des tables.

## Stockage de fichiers (MinIO)

Le service `minio` fournit un stockage compatible S3 pour les fichiers
**EPUB** des éditions, dans le bucket `${MINIO_BOOKS_BUCKET:-books}`.
`swann extract` crée le bucket au besoin, y envoie chaque EPUB sous la clé
`editions/<sha256>.epub` et la note dans `editions.epub_object_key` ; un
fichier remplacé donne un nouvel objet et l'ancien est supprimé. Les éditions
déjà extraites sans EPUB stocké sont complétées au prochain `swann extract`.
`MINIO_ENDPOINT` vide = pas d'envoi.

Console web sur http://localhost:9001 (identifiants `MINIO_ROOT_USER` /
`MINIO_ROOT_PASSWORD`).

> MinIO ne publie plus d'images Docker (ni Docker Hub, ni quay.io) : le
> compose utilise l'image `minio/minio:latest` déjà présente localement. Une
> installation neuve devra passer à un autre stockage S3 (Garage, SeaweedFS…)
> — seul `MINIO_ENDPOINT` et les identifiants changent pour l'ingestion.

## Structure

```
swann/
├── flake.nix                # environnement de développement (nix develop)
├── docker-compose.yml       # postgres, migrations, qdrant, minio, keycloak, api, reader, console, ingest-worker
├── docker-compose.gpu.yml   # accès GPU (ingestion, worker, API)
├── pyproject.toml / uv.lock # espace de travail uv (core, ingest, corpus-api)
├── .env.example
├── docs/
│   ├── corpus-api.md        # design de l'API du corpus
│   ├── console.md           # plan, décisions et état de la console d'administration
│   └── reader.md            # plan et état de la liseuse
├── documentation/           # site de documentation (Barjavel, Mermaid) → GitHub Pages
├── books/                   # corpus local (ignoré par git)
├── core/swann_core/          # partagé : embed/ (Qwen3, BM25), qdrant.py, s3.py (EPUB)
├── ingest/                  # pipeline d'ingestion (Python, CLI `swann`)
│   ├── Dockerfile
│   ├── tests/
│   └── swann_ingest/
│       ├── catalog.py       # books/ et work.toml
│       ├── epub/            # conteneur, OPF, table des matières, blocs, notes
│       ├── text/            # normalisation, titres, structure, segments, langue
│       ├── chunking/        # segments -> chunks (v1)
│       ├── align/           # alignement monotone (programmation dynamique)
│       ├── store/           # Postgres (écriture du corpus, journal des changements)
│       ├── pipeline/        # extract, index, align, steps (étapes du worker)
│       ├── jobs.py          # file de tâches (table jobs)
│       ├── worker.py        # swann worker
│       ├── quality.py       # qualité des éditions (signaux, score)
│       ├── identify.py      # classement d'un dépôt (auteurs, titres, Wikidata, contenu)
│       ├── wikidata.py      # client Wikidata (cache en base)
│       ├── upload.py        # traitement d'un EPUB déposé
│       ├── search.py        # recherche en ligne de commande
│       └── cli.py
├── corpus-api/              # Corpus API (FastAPI)
│   ├── Dockerfile
│   ├── tests/
│   └── swann_api/
│       ├── main.py          # application, cycle de vie (pool, Qdrant, S3, encodeur)
│       ├── auth.py          # jetons Keycloak, rôles, droits `access`
│       ├── engine.py        # index actif et encodeur de requêtes
│       ├── sql.py           # fragments SQL (libellés selon la langue, droits)
│       ├── text.py          # recherche insensible aux accents, ancres, extraits
│       ├── schemas.py       # modèles = contrat OpenAPI
│       └── routers/         # catalog, editions, search, alignment, changes, meta, admin*
├── reader/                  # liseuse (Next.js 16, PWA) — voir « Liseuse »
├── console/                 # console d'administration (Next.js 16) — voir « Console »
├── keycloak/                # realm importé, thème de connexion « swann »
├── pgadmin/
│   └── servers.json         # préconfiguration du serveur pgAdmin
└── db/
    └── migrations/          # dbmate
        ├── 00_extensions.sql
        ├── 01_persons.sql
        ├── 02_works.sql
        ├── 03_work_authors.sql
        ├── 04_movements.sql
        ├── 05_editions.sql
        ├── 06_sections_segments.sql
        ├── 07_chunks.sql
        ├── 08_alignments.sql
        ├── 09_vector_indexes.sql
        ├── 10_ingestions.sql
        ├── 11_users.sql
        ├── 12_alignment_reviews.sql
        ├── 20261002120000_corpus_api.sql
        ├── 20261003120000_console.sql
        └── 20261003130000_workers.sql
```

