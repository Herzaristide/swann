"""Authentification (jetons JWT Keycloak) et droits.

Rôles (rôles du client Keycloak `corpus-api`, ou rôles du realm) :
  corpus:read    catalogue, texte des éditions accessibles, recherche
  corpus:review  verdicts et corrections d'alignement (jeton utilisateur)
  corpus:admin   tout, y compris les éditions `restricted`

Droits sur le texte d'une édition (`editions.access`) :
  open        métadonnées, passages, texte intégral, EPUB
  excerpt     métadonnées, passages courts dans la recherche
  restricted  rien (sauf corpus:admin)
"""

from __future__ import annotations

import json
import urllib.request
from dataclasses import dataclass
from functools import cached_property
from typing import Annotated

import anyio
import anyio.to_thread
import jwt
from fastapi import Depends, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from swann_api.config import Settings
from swann_api.errors import Problem, forbidden, unauthorized

READ = "corpus:read"
REVIEW = "corpus:review"
ADMIN = "corpus:admin"
ALL_ROLES = frozenset({READ, REVIEW, ADMIN})

ALL_ACCESS = ("open", "excerpt", "restricted")
PUBLIC_ACCESS = ("open", "excerpt")
EXCERPT_MAX_CHARS = 1000


@dataclass(frozen=True)
class Principal:
    sub: str
    roles: frozenset[str]
    is_user: bool  # False = compte de service d'une application
    client_id: str | None = None

    def has(self, role: str) -> bool:
        if ADMIN in self.roles:
            return True
        if role == READ:
            return bool(self.roles & {READ, REVIEW})
        return role in self.roles

    @property
    def is_admin(self) -> bool:
        return ADMIN in self.roles

    # ---------------------------------------------------- droits sur le texte
    @property
    def visible_access(self) -> tuple[str, ...]:
        """Éditions dont les métadonnées et les passages sont visibles."""
        return ALL_ACCESS if self.is_admin else PUBLIC_ACCESS

    def can_see(self, access: str) -> bool:
        return access in self.visible_access

    def can_read_text(self, access: str) -> bool:
        return self.is_admin or access == "open"


DEV_PRINCIPAL = Principal(sub="dev", roles=ALL_ROLES, is_user=True, client_id="dev")


class TokenVerifier:
    """Vérifie les jetons avec les clés publiques du realm (JWKS), découvertes
    via la configuration OpenID et mises en cache par PyJWT."""

    def __init__(self, settings: Settings) -> None:
        self.issuer = settings.oidc_issuer.rstrip("/")
        self.discovery_url = (settings.oidc_discovery_url or self.issuer).rstrip("/")
        self.audience = settings.oidc_audience
        self.roles_client = settings.oidc_roles_client

    @cached_property
    def jwks(self) -> jwt.PyJWKClient:
        with urllib.request.urlopen(
            f"{self.discovery_url}/.well-known/openid-configuration", timeout=10
        ) as r:
            jwks_uri = json.load(r)["jwks_uri"]
        return jwt.PyJWKClient(jwks_uri, cache_keys=True, lifespan=3600)

    def verify(self, token: str) -> Principal:
        try:
            key = self.jwks.get_signing_key_from_jwt(token)
            claims = jwt.decode(
                token,
                key.key,
                algorithms=["RS256", "RS384", "RS512", "ES256", "ES384", "PS256"],
                audience=self.audience,
                issuer=self.issuer,
                options={"require": ["exp", "iat", "sub"]},
            )
        except jwt.PyJWTError as e:
            raise unauthorized(f"Jeton invalide : {e}") from e
        except OSError as e:  # Keycloak injoignable
            raise Problem(503, "auth-unavailable", f"Clés de vérification indisponibles : {e}") from e
        return principal_from_claims(claims, self.roles_client)


def principal_from_claims(claims: dict, roles_client: str) -> Principal:
    roles = set(claims.get("realm_access", {}).get("roles", []))
    roles |= set(claims.get("resource_access", {}).get(roles_client, {}).get("roles", []))
    username = claims.get("preferred_username", "")
    return Principal(
        sub=claims["sub"],
        roles=frozenset(roles & ALL_ROLES),
        is_user=not username.startswith("service-account-"),
        client_id=claims.get("azp"),
    )


_bearer = HTTPBearer(auto_error=False)


async def current_principal(
    request: Request, credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer)]
) -> Principal:
    settings: Settings = request.app.state.settings
    if settings.auth_disabled:
        return DEV_PRINCIPAL
    if credentials is None:
        raise unauthorized("Jeton Bearer requis.")
    verifier: TokenVerifier = request.app.state.verifier
    # Le premier appel télécharge les clés : hors de la boucle d'événements.
    return await anyio.to_thread.run_sync(verifier.verify, credentials.credentials)


def require(role: str, *, user: bool = False):
    async def dependency(principal: Annotated[Principal, Depends(current_principal)]) -> Principal:
        if not principal.has(role):
            raise forbidden(f"Rôle {role} requis.")
        if user and not principal.is_user:
            raise forbidden("Jeton utilisateur requis (pas un compte de service).")
        return principal

    return dependency


Reader = Annotated[Principal, Depends(require(READ))]
Reviewer = Annotated[Principal, Depends(require(REVIEW, user=True))]
Admin = Annotated[Principal, Depends(require(ADMIN))]
