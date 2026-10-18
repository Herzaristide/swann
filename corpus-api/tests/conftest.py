"""Tests d'intégration : une copie jetable de la base du corpus.

La base `SWANN_TEST_TEMPLATE` (défaut : swann) est copiée dans `swann_api_test`
au début de la session puis supprimée : les tests peuvent écrire sans toucher
aux vraies données. Qdrant et S3 sont lus tels quels (l'API n'y écrit pas).
Tests ignorés si Postgres est injoignable ou si la base modèle est en cours
d'utilisation (CREATE DATABASE … TEMPLATE exige qu'aucune session n'y soit ouverte).
"""

from __future__ import annotations

import os
from urllib.parse import urlsplit, urlunsplit

import psycopg
import pytest
from fastapi.testclient import TestClient

from swann_api.auth import ADMIN, READ, REVIEW, Principal, current_principal
from swann_api.config import Settings
from swann_api.main import create_app

TEST_DB = "swann_api_test"


def _with_db(url: str, name: str) -> str:
    parts = urlsplit(url)
    return urlunsplit(parts._replace(path=f"/{name}"))


@pytest.fixture(scope="session")
def test_database_url():
    base = Settings()
    template = os.environ.get("SWANN_TEST_TEMPLATE", "swann")
    admin_url = _with_db(base.database_url, "postgres")
    try:
        admin = psycopg.connect(admin_url, autocommit=True, connect_timeout=3)
    except psycopg.OperationalError as e:
        pytest.skip(f"Postgres injoignable : {e}")
    with admin:
        admin.execute(f"DROP DATABASE IF EXISTS {TEST_DB} WITH (FORCE)")
        try:
            admin.execute(f"CREATE DATABASE {TEST_DB} TEMPLATE {template}")
        except psycopg.errors.ObjectInUse:
            pytest.skip(f"Base modèle {template} en cours d'utilisation (fermer l'API / psql)")
    yield _with_db(base.database_url, TEST_DB)
    with psycopg.connect(admin_url, autocommit=True) as admin:
        admin.execute(f"DROP DATABASE IF EXISTS {TEST_DB} WITH (FORCE)")


@pytest.fixture(scope="session")
def db(test_database_url):
    with psycopg.connect(test_database_url, autocommit=True, row_factory=psycopg.rows.dict_row) as conn:
        yield conn


@pytest.fixture(scope="session")
def app(test_database_url):
    settings = Settings(database_url=test_database_url, auth_disabled=False, load_encoder=False)
    return create_app(settings)


@pytest.fixture(scope="session")
def client(app):
    with TestClient(app) as c:
        yield c


def as_principal(app, *roles: str, is_user: bool = True, sub: str = "test-user"):
    app.dependency_overrides[current_principal] = lambda: Principal(sub, frozenset(roles), is_user=is_user)


@pytest.fixture
def admin(app):
    as_principal(app, ADMIN)
    yield
    app.dependency_overrides.clear()


@pytest.fixture
def reader(app):
    as_principal(app, READ, is_user=False)
    yield
    app.dependency_overrides.clear()


@pytest.fixture
def reviewer(app):
    as_principal(app, REVIEW, sub="relecteur-1")
    yield
    app.dependency_overrides.clear()
