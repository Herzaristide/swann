# Image du pipeline d'ingestion (swann check / extract / index / align / search).
# Contexte de build = racine du dépôt (espace de travail uv : core + ingest).
# Le GPU est accessible si le conteneur est lancé avec docker-compose.gpu.yml
# (NVIDIA Container Toolkit / CDI côté hôte) ou docker-compose.amd.yml (ROCm).
FROM python:3.12-slim

# Variante de torch : `cuda` (roues du lock, GPU NVIDIA) ou `rocm` (GPU AMD).
# En rocm, torch et la pile CUDA du lock ne sont pas installés : torch est pris,
# à la version du lock, sur l'index ROCm de PyTorch.
ARG TORCH_BACKEND=cuda
ARG ROCM_INDEX=https://download.pytorch.org/whl/rocm7.2

# torch ROCm charge libatomic, absente de l'image slim.
RUN if [ "$TORCH_BACKEND" = rocm ]; then \
      apt-get update && apt-get install -y --no-install-recommends libatomic1 && rm -rf /var/lib/apt/lists/*; \
    fi

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
# Options de uv sync gardées dans /opt/uv-sync-args pour le second sync.
RUN --mount=type=cache,target=/root/.cache/uv \
    if [ "$TORCH_BACKEND" = rocm ]; then \
      sed -n 's/^name = "\(torch\|triton\|nvidia-.*\|cuda-.*\)"$/--no-install-package \1/p' uv.lock > /opt/uv-sync-args; \
      echo --inexact >> /opt/uv-sync-args; \
    else : > /opt/uv-sync-args; fi; \
    uv sync --frozen --no-dev --package swann-ingest --no-install-workspace $(cat /opt/uv-sync-args) && \
    if [ "$TORCH_BACKEND" = rocm ]; then \
      v=$(sed -n '/^name = "torch"$/{n;s/^version = "\(.*\)"$/\1/p}' uv.lock); \
      uv pip install --python /opt/venv/bin/python --index-url "$ROCM_INDEX" "torch==$v+${ROCM_INDEX##*/}"; \
    fi

COPY core/swann_core core/swann_core
COPY ingest/swann_ingest ingest/swann_ingest
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --package swann-ingest $(cat /opt/uv-sync-args)

ENTRYPOINT ["swann"]
CMD ["--help"]
