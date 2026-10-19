---
title: Déploiement
group: exploitation
summary: La pile Docker Compose — services, ports, ordre de démarrage, volumes, GPU (NVIDIA CDI), migrations et vérifications.
links: [architecture, developpement, worker, authentification, donnees]
---

# Déploiement (Docker Compose)

Toute la pile démarre avec une commande :

```bash
cp .env.example .env          # ajuster les mots de passe
docker compose up -d --build
```

## Services

| Service | Adresse | Rôle |
| --- | --- | --- |
| `postgres` | :5432 | bases `swann` (corpus), `keycloak`, `reader`, `console` |
| `migrate` | — | applique `db/migrations/` (dbmate), puis s'arrête |
| `databases` | — | crée les bases `keycloak`, `reader`, `console` (idempotent), puis s'arrête |
| `keycloak` | :8080/admin | identité, realm `swann` |
| `qdrant` | :6333 / :6334 | index vectoriels (REST / gRPC) |
| `minio` | :9000 / :9001 | EPUB (API S3 / console web) |
| `api` | :8000/v1/docs | Corpus API |
| `ingest-worker` | — | `swann worker`, file de tâches, permanent |
| `reader` | :3000 | liseuse |
| `reader-worker` | — | suit `/v1/changes`, recale les progressions |
| `pgadmin` | :5050 | visualisation du schéma (ERD Tool) |
| `ingest` | — | profil `ingest` : commande à la demande (`docker compose run --rm ingest …`) |

La console (`console/`, port 3001) se lance pour l'instant hors compose
(`pnpm dev`).

## Ordre de démarrage

```mermaid
flowchart TB
  pg["postgres<br/>(sain : pg_isready)"] --> mig["migrate<br/>dbmate up"]
  pg --> dbs["databases<br/>CREATE DATABASE …"]
  dbs --> kc["keycloak<br/>(sain : /health/ready)"]
  qd["qdrant (sain)"]
  mn["minio (sain)"]
  mig --> api["api<br/>(sain : /v1/health,<br/>une fois le modèle chargé)"]
  qd --> api
  mn --> api
  mig --> iw["ingest-worker"]
  qd --> iw
  mn --> iw
  dbs --> reader["reader<br/>migrations Drizzle au démarrage"]
  kc --> reader
  api --> reader
  reader --> rw["reader-worker"]
  pg --> pga["pgadmin"]
```

Les `depends_on` utilisent `service_healthy` et
`service_completed_successfully` : un service ne démarre que lorsque ses
dépendances sont prêtes.

## Volumes

| Volume | Contenu |
| --- | --- |
| `postgres_data` | toutes les bases Postgres |
| `qdrant_data` | collections Qdrant |
| `minio_data` | buckets `books`, `inbox` |
| `hf_cache` | modèles Hugging Face (Qwen3, LaBSE), partagés par `api`, `ingest`, `ingest-worker` |
| `pgadmin_data` | configuration pgAdmin |

Pour réutiliser les modèles déjà présents sur l'hôte :

```bash
docker run --rm -v swann_hf_cache:/c -v ~/.cache/huggingface/hub:/src:ro alpine \
  sh -c 'mkdir -p /c/hub && cp -a /src/. /c/hub/'
```

puis `HF_HUB_OFFLINE=1` dans `.env` (démarrage plus rapide).

## GPU

```bash
docker compose -f docker-compose.yml -f docker-compose.gpu.yml up -d
```

`docker-compose.gpu.yml` donne `nvidia.com/gpu=all` à `ingest`,
`ingest-worker` et `api` (NVIDIA Container Toolkit en mode **CDI** ; NixOS :
`hardware.nvidia-container-toolkit.enable = true;`). Recherche « thème » :
~50 ms par requête au lieu de ~650–900 ms sur CPU.

### GPU AMD (ROCm)

```bash
docker compose -f docker-compose.yml -f docker-compose.amd.yml build ingest
docker compose -f docker-compose.yml -f docker-compose.amd.yml run --rm ingest index run <collection>
```

`docker-compose.amd.yml` construit `swann-ingest:rocm` (`TORCH_BACKEND=rocm` :
torch pris sur l'index ROCm de PyTorch, à la version du lock, sans la pile
CUDA) et donne `/dev/kfd` et `/dev/dri` à `ingest` et `ingest-worker` ; l'API
reste sur CPU. Image ~24 Go. RX 7600 XT (gfx1102) : `HSA_OVERRIDE_GFX_VERSION=11.0.0`
et `HIP_VISIBLE_DEVICES=0` (écarte l'iGPU), valeurs par défaut du fichier.
Mesuré : ~8 600 tokens/s pour Qwen3-Embedding-0.6B.

## Réseau : URL publiques et internes

```mermaid
flowchart LR
  nav["Navigateur"] -- "localhost:8080" --> kc["keycloak"]
  nav -- "localhost:3000" --> reader["reader"]
  reader -- "http://keycloak:8080<br/>(discovery, échange de code)" --> kc
  reader -- "http://api:8000" --> api["api"]
  api -- "http://keycloak:8080<br/>(JWKS)" --> kc
```

Les jetons portent toujours l'**émetteur public** (`KEYCLOAK_URL`,
`http://localhost:8080`), tandis que les conteneurs joignent Keycloak par le
réseau interne (`KC_HOSTNAME_BACKCHANNEL_DYNAMIC`). Le client `swann-reader`
n'accepte que `READER_URL` comme redirection.

## Vérifications

```bash
docker compose ps
curl http://localhost:8000/v1/health                  # API
curl http://localhost:6333/healthz                    # Qdrant
docker compose exec postgres pg_isready -U swann       # Postgres
curl http://localhost:9000/minio/health/live          # MinIO
```

> MinIO ne publie plus d'images Docker : le compose utilise
> `minio/minio:latest` déjà présente localement. Une installation neuve
> devra passer à un autre stockage S3 (Garage, SeaweedFS…) ; seuls
> `MINIO_ENDPOINT` et les identifiants changent.
