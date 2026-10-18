"""Configuration lue depuis l'environnement (et le .env à la racine du dépôt)."""

from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

REPO_ROOT = Path(__file__).resolve().parents[2]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=REPO_ROOT / ".env", env_file_encoding="utf-8", extra="ignore")

    database_url: str = "postgresql://swann:swann@localhost:5432/swann"
    db_pool_min: int = 1
    db_pool_max: int = 10

    qdrant_url: str = "http://localhost:6333"
    qdrant_api_key: str | None = None
    # Alias Qdrant de l'index actif (voir `swann index activate`).
    qdrant_collection: str = "chunks"

    minio_endpoint: str | None = "http://localhost:9000"
    minio_root_user: str = "swann"
    minio_root_password: str = "change-me"
    minio_books_bucket: str = "books"
    # Dépôts de la console en attente du worker (docs/console.md §4).
    minio_inbox_bucket: str = "inbox"
    # Taille maximale d'un EPUB déposé (octets).
    upload_max_bytes: int = 100 * 1024 * 1024

    # Keycloak : émetteur des jetons (URL du realm) et audience attendue.
    oidc_issuer: str = "http://localhost:8080/realms/swann"
    # URL du realm où lire la configuration OpenID et les clés, si l'émetteur
    # public n'est pas joignable d'ici (conteneur : http://keycloak:8080/realms/swann).
    oidc_discovery_url: str | None = None
    oidc_audience: str = "corpus-api"
    # Client Keycloak qui porte les rôles corpus:* (resource_access.<client>.roles).
    oidc_roles_client: str = "corpus-api"
    # DÉVELOPPEMENT UNIQUEMENT : aucune vérification de jeton, tous les rôles.
    auth_disabled: bool = False

    # Encodeur de requêtes : "cuda", "cpu" ou vide = cuda si disponible.
    # Qwen3-Embedding-0.6B : ~50 ms par requête sur GPU, ~650 ms sur CPU.
    api_device: str | None = None
    # False = pas de modèle chargé (tests, déploiement sans recherche sémantique).
    load_encoder: bool = True

    cors_origins: list[str] = []


@lru_cache
def get_settings() -> Settings:
    return Settings()
