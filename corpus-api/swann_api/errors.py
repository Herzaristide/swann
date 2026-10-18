"""Erreurs au format RFC 9457 (application/problem+json)."""

from __future__ import annotations

from typing import Any

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

PROBLEM_JSON = "application/problem+json"
TYPE_BASE = "https://swann/errors/"

TITLES = {
    400: "Requête invalide",
    401: "Authentification requise",
    403: "Accès refusé",
    404: "Introuvable",
    409: "Conflit",
    416: "Plage invalide",
    422: "Paramètres invalides",
    503: "Service indisponible",
}


class Problem(Exception):
    def __init__(self, status: int, type_: str, detail: str, title: str | None = None, **extra: Any):
        self.status = status
        self.type = type_
        self.title = title or TITLES.get(status, "Erreur")
        self.detail = detail
        self.extra = extra
        self.headers: dict[str, str] = {}

    def response(self) -> JSONResponse:
        body = {
            "type": TYPE_BASE + self.type,
            "title": self.title,
            "status": self.status,
            "detail": self.detail,
            **self.extra,
        }
        return JSONResponse(body, status_code=self.status, media_type=PROBLEM_JSON, headers=self.headers)


def not_found(what: str) -> Problem:
    return Problem(404, "not-found", f"{what} introuvable.")


def forbidden(detail: str) -> Problem:
    return Problem(403, "forbidden", detail)


def unauthorized(detail: str) -> Problem:
    p = Problem(401, "unauthorized", detail)
    p.headers["WWW-Authenticate"] = 'Bearer realm="corpus-api"'
    return p


def install(app: FastAPI) -> None:
    @app.exception_handler(Problem)
    async def _problem(_: Request, exc: Problem) -> JSONResponse:
        return exc.response()

    @app.exception_handler(RequestValidationError)
    async def _validation(_: Request, exc: RequestValidationError) -> JSONResponse:
        errors = [
            {"loc": [str(p) for p in e["loc"]], "msg": e["msg"], "type": e["type"]} for e in exc.errors()
        ]
        return Problem(422, "validation", "Paramètres invalides.", errors=errors).response()

    @app.exception_handler(StarletteHTTPException)
    async def _http(_: Request, exc: StarletteHTTPException) -> JSONResponse:
        p = Problem(exc.status_code, "http", str(exc.detail))
        p.headers.update(exc.headers or {})
        return p.response()
