# Image du pipeline d'ingestion (swann check / extract / index / align / search).
# Contexte de build = racine du dépôt (espace de travail uv : core + ingest).
# Le GPU est accessible si le conteneur est lancé avec docker-compose.gpu.yml
# (NVIDIA Container Toolkit / CDI côté hôte).
FROM python:3.12-slim

COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /bin/

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PROJECT_ENVIRONMENT=/opt/venv \
    PATH=/opt/venv/bin:$PATH \
    HF_HOME=/cache/huggingface \
    BOOKS_DIR=/books

WORKDIR /app

# Dépendances d'abord (couche mise en cache tant que uv.lock ne change pas).
COPY pyproject.toml uv.lock ./
COPY core/pyproject.toml core/
COPY ingest/pyproject.toml ingest/
COPY corpus-api/pyproject.toml corpus-api/
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --package swann-ingest --no-install-workspace

COPY core/swann_core core/swann_core
COPY ingest/swann_ingest ingest/swann_ingest
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --package swann-ingest

ENTRYPOINT ["swann"]
CMD ["--help"]
