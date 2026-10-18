# Console d'administration (console) — plan

> **État (2026-10-03)** : étapes 0 à 7 réalisées (voir §13 pour les écarts
> au plan). `docker compose up -d` lance la console (http://localhost:3001),
> l'API et le worker. Tests : 78 (API) + 46 (ingestion), parcours Playwright
> `console/e2e/smoke.mjs` sans erreur sur toutes les pages.

Application web réservée aux administrateurs du corpus : **déposer des
livres** (classés automatiquement dans la bonne œuvre), **superviser** les
bases et les ingestions, **contrôler la qualité** des éditions, **modifier
les fiches et la structure** des ouvrages, **travailler et vérifier les
alignements**. Comme la liseuse, elle passe **uniquement par la Corpus API** ;
c'est l'API (et le worker d'ingestion) qui écrivent dans Postgres, Qdrant et
MinIO.

## 1. Changements de principe

Par rapport à `docs/corpus-api.md` §1 (« ingestion et administration des index
restent dans la CLI ») :

- **Postgres est la référence** pour les œuvres, éditions, personnes et
  courants ; Qdrant en est l'index (son payload est recopié depuis Postgres
  à chaque modification). `books/` et les `work.toml` deviennent **une voie
  d'import parmi d'autres** : `swann extract` crée ce qui manque mais **ne
  réécrit plus une fiche existante** (option `--overwrite-metadata` pour le
  comportement actuel).
- **L'ingestion classe elle-même** un livre déposé : une traduction française
  de *Crime et Châtiment* est rattachée à l'œuvre existante, ou l'œuvre est
  créée si elle n'existe pas (§4).
- **L'API écrit** pour l'administration (rôle `corpus:admin`) et confie les
  traitements lourds à un **worker GPU permanent** via une file de tâches dans
  Postgres. La CLI `swann` reste utilisable et ses exécutions apparaissent
  dans la console.
- **Le texte n'est pas modifiable** dans la console : il vient de l'EPUB. Pour
  le corriger, on redépose un EPUB (nouvelle révision de la même édition).
  Fiches et structure sont modifiables.

## 2. Architecture

```
navigateur                         console/ (Next.js, serveur)
 └─ pages React  ───────────────►  ├─ /api/auth/*    Better Auth ──OIDC──► Keycloak (client swann-console)
                                   ├─ /api/corpus/*  proxy BFF  ──JWT───► corpus-api /v1 et /v1/admin
                                   └─ /api/stream    relais SSE ◄────────  corpus-api /v1/admin/stream

corpus-api ──► Postgres : jobs, ingestions, edition_quality, corpus_events (+ pg_notify)
           ──► MinIO    : bucket `inbox` (dépôts) puis `books`
           ──► Qdrant   : état des collections (lecture)

ingest-worker (swann worker, GPU, permanent)
   LISTEN jobs ─► identify → extract → quality → index → align ─► Postgres / Qdrant / MinIO
              └─► progression (jobs.progress) + pg_notify ─► API ─► SSE ─► console
```

- **BFF** comme la liseuse : jetons côté serveur, cookie `httpOnly`, pas de
  CORS sur l'API.
- **Dépôts** : le fichier passe par le BFF puis l'API, qui l'écrit dans le
  bucket `inbox` (EPUB de quelques Mo ; limite configurable, 100 Mo par
  défaut). Pas d'URL MinIO présignée : MinIO reste invisible du navigateur.
- **Temps réel** : `LISTEN jobs` / `LISTEN corpus_events` dans l'API → un flux
  SSE `/v1/admin/stream` → relayé par le BFF → invalidation des requêtes
  TanStack Query concernées.

## 3. File de tâches et worker

Pas de Redis ni de Celery : une table `jobs` dans Postgres, prise par le worker
avec `SELECT … FOR UPDATE SKIP LOCKED`, réveillé par `pg_notify('jobs', id)`.

```sql
CREATE TYPE job_kind AS ENUM (
  'ingest',        -- dépôt : identify → extract → quality → index → align
  'reprocess',     -- après modification de structure : quality → index → align
  'align',         -- réalignement d'une œuvre (option : à partir d'une section)
  'sync_payload',  -- recopie fiche → payload Qdrant (titre, droits…)
  'delete_edition',
  'import_books'   -- import en lot de books/ (équivalent de swann extract)
);
CREATE TYPE job_status AS ENUM (
  'queued', 'running', 'needs_review', 'succeeded', 'failed', 'cancelled'
);
CREATE TABLE jobs (
  id             UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  kind           job_kind NOT NULL,
  status         job_status NOT NULL DEFAULT 'queued',
  params         JSONB NOT NULL DEFAULT '{}',   -- objet inbox, work_id, options…
  progress       JSONB NOT NULL DEFAULT '{}',   -- {step, done, total, tokens_per_s}
  result         JSONB,                         -- edition_id, rapport d'identification…
  error          TEXT,
  parent_id      UUID REFERENCES jobs (id) ON DELETE CASCADE,  -- lot de dépôts
  created_by_sub TEXT,                          -- sub Keycloak, ou 'cli'
  created_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
  started_at     TIMESTAMPTZ,
  heartbeat_at   TIMESTAMPTZ,                   -- worker vivant ? (> 60 s = bloqué)
  finished_at    TIMESTAMPTZ,
  attempts       SMALLINT NOT NULL DEFAULT 0
);
ALTER TABLE ingestions ADD COLUMN job_id UUID REFERENCES jobs (id) ON DELETE SET NULL;
ALTER TABLE ingestions ADD COLUMN stage TEXT;  -- extract | index | align
```

- `ingestions` reste le journal par édition et par étape ; `jobs` porte ce
  que l'utilisateur a demandé et l'avancement d'un lot (un dépôt de 50 EPUB =
  un job parent et 50 enfants).
- **Worker** : `swann worker`, service compose `ingest-worker` permanent avec
  `devices: nvidia.com/gpu=all` (CDI : `hardware.nvidia-container-toolkit.enable
  = true;` dans la config NixOS). Il garde Qwen3-Embedding-0.6B et LaBSE
  chargés (~2,5 Go sur les 8 Go de la 3070) et traite **une tâche GPU à la
  fois**. Battement de cœur toutes les 10 s ; au démarrage, les tâches
  `running` dont le battement est vieux repassent en `queued`.
- **Annulation** : `cancelled` est lu entre deux étapes ; une édition à moitié
  écrite est annulée par transaction (extract) ou reprise (index, align : déjà
  idempotents).
- La CLI (`swann extract`, `index run`, `align`) crée aussi une ligne `jobs`
  (`created_by_sub = 'cli'`) pour que ses exécutions soient visibles.

## 4. Dépôt et classement automatique

### 4.1 Parcours

1. L'admin glisse un ou plusieurs EPUB (ou un dossier) dans la console. Il
   peut préciser, facultativement : œuvre cible, langue, traducteurs, droits
   `access` (défaut `restricted`).
2. L'API vérifie le fichier (zip, OPF, DRM, taille), calcule le sha256, refuse
   un doublon exact (« déjà présent : *Crime et Châtiment*, fr, Derély »),
   écrit l'objet dans `inbox/` et crée un job `ingest`.
3. Le worker **identifie** l'œuvre (4.2), puis enchaîne extract → quality →
   index → align.
4. En cas de doute, le job s'arrête en `needs_review` : la console montre les
   candidats et l'admin choisit (rattacher à X, créer une nouvelle œuvre,
   corriger la fiche) ; le job reprend.

### 4.2 Identification de l'œuvre

Indices, du moins coûteux au plus coûteux :

| # | Indice | Source |
| --- | --- | --- |
| 1 | Langue | `dc:language`, confirmée par la détection sur le texte (`text/language.py`) |
| 2 | Auteur | `dc:creator` (rôle `aut`) comparé à `persons` + `person_names` (toutes langues et translittérations : Dostoïevski / Dostoevsky / Достоевский), sans accents, trigrammes |
| 3 | Titre | `dc:title` comparé à `works.title` + `work_titles` des œuvres de cet auteur (trigrammes, articles et sous-titres retirés) |
| 4 | Wikidata | si 2 ou 3 échouent : recherche de l'auteur, puis de ses œuvres et de leurs libellés dans la langue de l'édition → QID de l'œuvre, titre et langue originaux, année ; compare à `works.wikidata_id` / `persons.wikidata_id`. Réponses mises en cache |
| 5 | Contenu | LaBSE sur les premiers paragraphes du corps et les titres de chapitres, comparés à une édition existante de l'œuvre candidate (même encodeur que l'alignement). Confirme qu'il s'agit bien d'une traduction du même texte et repère les adaptations |
| 6 | Traducteurs | `dc:creator` / `dc:contributor` de rôle `trl` → `edition_contributors` ; distingue deux traductions d'une même langue |

Décision (seuils réglables, `IDENTIFY_*` dans la config du worker) :

- **Rattachement automatique** : auteur et titre (ou QID) concordent, et le
  contenu confirme si une autre édition existe. `is_original` = langue de
  l'édition = `works.original_language`.
- **Même œuvre, même langue, mêmes traducteurs** : nouvelle édition ou
  remplacement du texte d'une édition existante (nouvelle révision) → toujours
  `needs_review`.
- **Aucune œuvre** et auteur identifié (ou trouvé sur Wikidata) : création de
  l'œuvre (titres multilingues, année, langue originale, QID repris de
  Wikidata), et de la personne si besoin.
- **Doute** (plusieurs candidats proches, contenu discordant, auteur
  inconnu, métadonnées vides) : `needs_review` avec le rapport d'identification
  (`jobs.result`) : indices, scores et candidats.

Le rapport reste consultable sur la fiche de l'édition (« rattachée
automatiquement à … par titre + Wikidata, contenu 0,82 »). Une erreur se
corrige après coup en **déplaçant l'édition** vers une autre œuvre (§5).

### 4.3 Identité des éditions déposées

`editions.source_file` vaut `inbox/<sha256>.epub` pour un dépôt (chemin dans
`books/` pour un import) ; l'EPUB est ensuite rangé dans le bucket `books`
comme aujourd'hui (`epub_object_key`). Le slug d'une œuvre créée est généré
(`auteur/titre-original`, translittéré).

## 5. Modification des fiches et de la structure

Chaque modification passe par l'API, est **journalisée dans `corpus_events`**
(type, avant/après, `sub` de l'admin), alimente `/v1/changes` (la liseuse se
met à jour) et lance si besoin un job. Concurrence optimiste : `If-Match` sur
`updated_at`, `412` si la fiche a changé entre-temps.

| Objet | Modifiable | Effets |
| --- | --- | --- |
| Œuvre | titre, titres par langue, auteurs (ordre), année, langue originale, courants, QID | `sync_payload` si un champ copié dans Qdrant change |
| | **fusionner deux œuvres** (doublon créé par le classement) | éditions déplacées, `align` de l'œuvre fusionnée |
| Édition | titre, langue, `is_original`, éditeur, année, traducteurs et contributeurs, droits `access` | `sync_payload` (droits, titre) |
| | **déplacer vers une autre œuvre** | `align` des deux œuvres |
| | supprimer | job `delete_edition` : Postgres, points Qdrant, objet MinIO |
| Personne | noms et variantes par langue, dates, QID ; **fusionner deux personnes** | variantes réutilisées par le classement (§4.2) |
| Courant | arbre (nom, parent), œuvres rattachées | |
| Structure | par section : `kind`, `matter` (front / body / back), titre, libellé, numéro ; déplacer dans l'arbre ; **scinder** une section à un segment, **fusionner** deux sections voisines ; origine d'une note (auteur, traducteur, éditeur) | job `reprocess` : le découpage ne garde que le corps et ne déborde pas d'une section, et `section_path` est dans le payload Qdrant → redécoupage, réindexation, puis réalignement |

Les segments ne sont pas touchés par une modification de structure : la
`revision` de l'édition ne change pas et les ancres des lecteurs
`(edition_id, revision, seq)` restent valables.

## 6. Atelier d'alignement

Réutilise les endpoints existants (`GET /alignment/sample`, verdicts,
`POST` / `DELETE /alignment/links`, `GET /editions/{id}/parallel`) et ajoute :

- **Liste des éditions** par statut (`pending`, `reliable`, `doubtful`,
  `rejected`), taux aligné, score moyen, part de scores faibles ; tri « à
  traiter d'abord ».
- **Carte d'une édition** : bande par section colorée selon le score (où
  l'alignement décroche), sections de référence appariées ou non.
- **Vue côte à côte éditable** : liens sous le seuil surlignés ; cliquer deux
  segments pour les lier, défaire un lien, lier plusieurs segments à une même
  unité (1↔n) ; navigation « lien douteux suivant » au clavier.
- **Vérification par échantillon** : série de liens tirés au hasard (verdicts
  `correct` / `incorrect` / `partial`, `is_sample`), avec la **précision
  estimée** par méthode et son intervalle (Wilson). C'est le chiffre qui
  permet de filtrer les analyses par confiance.
- **Actions** : réaligner l'œuvre ou à partir d'une section (job `align`,
  liens manuels conservés), changer l'édition de référence, fixer à la main le
  statut d'une édition (`reliable` / `rejected`).

Rôle : `corpus:review` suffit pour l'atelier ; `corpus:admin` pour réaligner
et changer la référence.

## 7. Qualité des ouvrages

Table `edition_quality` (une ligne par édition), en deux parties :

- **Mesuré au parsing** par `extract` (l'information n'existe plus ensuite) :
  `structure_method` (toc / headings), avertissements (`jsonb`), langue
  détectée, présence d'une table des matières, sections non résolues.
- **Calculé depuis la base** par l'étape `quality` du worker (aussi après un
  `reprocess`, ou en lot par `swann quality`) : nombre de chapitres, notes,
  appels de note orphelins, segments du corps, longueur moyenne et maximale des
  segments, part du texte hors corps, sections vides, pages, chunks par index.

Ces mesures donnent des **signaux** affichés en pastilles et filtrables :

| Signal | Condition (seuils réglables) |
| --- | --- |
| Pas de table des matières | `structure_method = 'headings'` |
| Langue incohérente | langue détectée ≠ langue de la fiche |
| Corps vide ou maigre | segments du corps < N, ou part hors corps > 30 % |
| Segments anormaux | segment > 5 000 caractères (paragraphes collés) ou médiane < 40 (vers coupés, OCR) |
| Notes cassées | appels sans note, ou notes jamais appelées |
| Chapitres suspects | 0 ou 1 chapitre pour un roman, sections vides |
| Pas indexée / pas alignée | absente de l'index actif ; alignement `doubtful` ou < 50 % |
| Incohérence de stockage | EPUB absent de MinIO, points Qdrant ≠ chunks Postgres |

Un **score** de 0 à 100 résume les signaux pour trier ; un signal se marque
« vu, accepté » (table `quality_acks`) pour ne plus remonter.

## 8. Supervision

- **Santé** : Postgres (taille, connexions, version des migrations dbmate),
  Qdrant (collections, points, statut, alias actif), MinIO (objets, taille),
  API (encodeur), worker (dernier battement, tâche en cours, GPU visible).
- **Entonnoir** : déposés → extraits → indexés (index actif) → alignés
  (reliable), en éditions et en œuvres ; par langue.
- **Tâches** : file en cours en direct (étape, `done/total`, tokens/s, temps
  restant), historique filtrable, échecs avec erreur, relancer, annuler.
- **Index** : les `vector_indexes`, l'avancement de chacun, activer un index
  (bascule d'alias) depuis la console.

## 9. API : `/v1/admin`

Tous en `corpus:admin`, sauf mention ; documentés dans `docs/corpus-api.md`
une fois réalisés.

| Méthode | Chemin | Objet |
| --- | --- | --- |
| GET | `/admin/overview` | santé, volumes, entonnoir |
| GET | `/admin/stream` | SSE : jobs, ingestions, corpus_events |
| POST | `/admin/uploads` | dépôt multipart (un ou plusieurs EPUB + options) → jobs |
| GET / POST | `/admin/jobs`, `/admin/jobs/{id}` | liste, détail, `cancel`, `retry` |
| POST | `/admin/jobs/{id}/resolve` | choix de l'admin pour un `needs_review` |
| GET | `/admin/quality` | éditions + signaux, filtres et tri |
| POST | `/admin/quality/{edition_id}/ack` | accepter un signal |
| GET | `/admin/consistency` | écarts Postgres / Qdrant / MinIO |
| PATCH / DELETE | `/admin/works/{id}`, `/admin/editions/{id}`, `/admin/persons/{id}`, `/admin/movements/{id}` | fiches |
| POST | `/admin/works/{id}/merge`, `/admin/persons/{id}/merge`, `/admin/editions/{id}/move` | fusions, déplacement |
| PATCH / POST | `/admin/editions/{id}/sections/{section_id}`, `…/split`, `…/merge` | structure |
| GET | `/admin/alignment/editions` | liste pour l'atelier (`corpus:review`) |
| POST | `/admin/works/{id}/align` | réalignement (option `from_section`) |
| GET / POST | `/admin/indexes`, `/admin/indexes/{collection}/activate` | index vectoriels |

Les tests d'écriture tournent sur la base jetable de
`corpus-api/tests/conftest.py`, jamais sur la vraie base.

## 10. Application `console/`

Même pile que `reader/` (Next.js 16, TypeScript strict, Tailwind v4,
shadcn/ui, TanStack Query, openapi-fetch généré, Better Auth + Keycloak,
Biome, Vitest, Playwright), plus :

- **TanStack Table** pour les grandes listes (tri, filtres, pagination côté
  serveur) ;
- **graphiques shadcn** (Recharts) pour l'entonnoir, les histogrammes de
  scores, la précision par méthode ;
- glisser-déposer natif pour les dépôts, progression par fichier.

Pas de PWA ni de hors ligne : outil de bureau, mais lisible sur téléphone
(supervision). Port `${CONSOLE_PORT:-3001}`, client Keycloak `swann-console`
(confidentiel, redirection `http://localhost:3001/*`), accès refusé sans
`corpus:admin` ou `corpus:review`.

Pages :

| Chemin | Contenu |
| --- | --- |
| `/` | vue d'ensemble : santé, entonnoir, tâches en cours, derniers échecs, signaux qualité les plus fréquents |
| `/upload` | dépôt, puis suivi des fichiers déposés |
| `/jobs`, `/jobs/[id]` | file, historique ; détail avec étapes, journal, rapport d'identification, résolution d'un `needs_review` |
| `/quality` | tableau des éditions avec pastilles et score |
| `/works`, `/works/[id]` | liste, fiche éditable, éditions, fusion |
| `/editions/[id]` | fiche, qualité, structure éditable (arbre + aperçu du texte), stockage, historique |
| `/persons`, `/persons/[id]`, `/movements` | fiches, fusion de personnes, arbre des courants |
| `/alignment`, `/alignment/[editionId]`, `/alignment/review` | atelier (§6) |
| `/indexes` | index vectoriels |

## 11. Étapes

| # | Étape | Livrable vérifiable |
| --- | --- | --- |
| 0 | **Migration** : `jobs`, `ingestions.job_id/stage`, `edition_quality`, `quality_acks` ; `extract` ne réécrit plus les fiches existantes | migrations appliquées ; `swann extract` sur books/ ne change rien |
| 1 | **Worker** : `swann worker` (file, battement, annulation), étapes extract / quality / index / align ; jobs créés par la CLI ; service `ingest-worker` GPU (CDI activé sur l'hôte) | un job `import_books` traite books/ ; `docker compose up` lance le worker sur GPU |
| 2 | **Qualité** : mesures au parsing + étape `quality` + `swann quality` | signaux calculés pour les 36 éditions |
| 3 | **API admin, lecture** : overview, jobs, quality, consistency, stream SSE | tests sur base jetable ; flux SSE visible avec `curl -N` |
| 4 | **Console, socle et supervision** : `console/`, auth `swann-console`, vue d'ensemble, tâches, qualité, index | suivre en direct un import lancé par la CLI |
| 5 | **Dépôt et classement** : `/admin/uploads`, identification (§4.2), `needs_review` et résolution | déposer une traduction d'une œuvre existante (rattachée) et un livre inconnu (œuvre créée) |
| 6 | **Édition** : fiches, fusions, déplacement, structure + `reprocess`, historique | corriger une section mal typée → réindexée, visible dans la liseuse |
| 7 | **Atelier d'alignement** : liste, carte, côte à côte éditable, échantillons et précision, réalignement | corriger un passage en↔ru de notes-from-underground |
| 8 | **Finitions** : E2E Playwright, tests de charge du dépôt (100 EPUB), doc API | tests verts |

## 12. Décisions (2026-10-03)

- **Wikidata** : autorisé pour le classement des dépôts (réponses en cache
  90 jours dans `wikidata_cache`).
- **Validation** : chaque dépôt attend la validation d'un admin
  (`IDENTIFY_AUTO=false`), même quand les indices concordent ; à passer à
  `true` une fois les seuils éprouvés.
- **Formats** : EPUB seulement.
- **Suppression** : corbeille ; purge définitive (base, Qdrant, EPUB) après
  `TRASH_DAYS` (30) jours, ou à la demande depuis la corbeille.

## 13. État de la réalisation

### Ce qui s'écarte du plan

- **Révision et structure** : une modification de structure incrémente la
  révision de l'édition et émet `edition.text_replaced` (`reason: structure`).
  Sans cela, les applications garderaient en cache (immuable) une table des
  matières périmée ; le texte et les `seq` ne changeant pas, la liseuse
  relocalise ses ancres sans perte.
- **Wikidata** : pas de SPARQL (le point d'accès est souvent saturé, 429) ;
  recherche par l'API (`wbsearchentities` + `wbgetentities`) : titre dans la
  langue de l'édition → œuvre (P50 = auteur, sans P629 = édition), langue
  originale par P364/P407 → P218.
- **Contenu** (§4.2, indice 5) : part des paragraphes échantillonnés du dépôt
  dont la traduction est retrouvée (LaBSE ≥ 0,70) au même endroit du livre
  (± 4 %) dans une édition existante. Calibré sur le corpus : même œuvre
  0,44–0,56, autre œuvre du même auteur 0 ; adaptations et recueils
  réordonnés tombent bas (0,06–0,13) et passent en validation manuelle.
- **Langue détectée** : vote sur trois extraits du corps (un extrait plein de
  noms propres trompait lingua) ; aussi utilisé par `swann extract`.
- **Console** : pas de TanStack Table ni de Recharts (le tri, les filtres et
  la pagination sont faits par l'API ; barres en CSS). Rôle relecteur : seul
  l'atelier d'alignement est accessible.
- **Index** : la création d'un index reste dans la CLI (`swann index create`) ;
  la console montre l'avancement et active un index.
- **Changement de l'édition de référence** d'une œuvre : pas encore (la
  référence reste l'originale, sinon la plus longue).

### Restent ouverts

- Le worker conteneurisé tourne sur **CPU** tant que le CDI NVIDIA n'est pas
  activé sur l'hôte (`hardware.nvidia-container-toolkit.enable = true;`) :
  classement et indexation d'un dépôt prennent alors quelques minutes.
- Tests de bout en bout Playwright intégrés à une suite (aujourd'hui : script
  de parcours `pnpm smoke`).
