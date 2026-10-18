"""Tests sans base de données."""

from __future__ import annotations

import time

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa

from swann_api.auth import ADMIN, READ, REVIEW, Principal, TokenVerifier, principal_from_claims
from swann_api.config import Settings
from swann_api.deps import decode_cursor, encode_cursor
from swann_api.errors import Problem
from swann_api.routers.alignment import group_pairs, insert_orphans
from swann_api.routers.editions import parse_range
from swann_api.routers.search import expand_languages, term_spans
from swann_api.text import excerpt, find_all, locate_quote, normalize, quick_normalize, snippet


# ---------------------------------------------------------------------- texte
@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("Œuvre complète", "oeuvre complete"),
        ("ÉLÈVE", "eleve"),
        ("Straße", "strasse"),
        ("Достоевский", "достоевскии"),
    ],
)
def test_normalize(text, expected):
    assert normalize(text) == expected
    assert quick_normalize(text) == expected


def test_find_all_maps_back_to_original_positions():
    text = "L'œuvre de Hugo. Une ŒUVRE immense."
    spans = find_all(text, "oeuvre")
    assert [text[a:b] for a, b in spans] == ["œuvre", "ŒUVRE"]
    assert find_all(text, "   ") == []


def test_snippet_and_excerpt():
    text = "mot " * 100 + "CIBLE" + " mot" * 100
    start = text.index("CIBLE")
    s, at = snippet(text, start, start + 5, context=20)
    assert s[at : at + 5] == "CIBLE" and s.startswith("…") and s.endswith("…")
    short, shift = excerpt(text, 50, (start, start + 5))
    assert len(short) == 50 and "CIBLE" in short and text[shift : shift + 50] == short
    assert excerpt("court", 50) == ("court", 0)


def test_locate_quote_prefers_context_then_proximity():
    segments = [(1, "Il pleut. Il pleut encore."), (5, "Rien."), (9, "Le soir, il pleut sur la ville.")]
    found = locate_quote(segments, "il pleut", "Le soir, ", " sur la ville", near_seq=1)
    assert (found.seq, found.offset) == (9, 9)
    found = locate_quote(segments, "pleut", "", "", near_seq=8)
    assert found.seq == 9  # même contexte (vide) : la plus proche
    # une occurrence à l'identique (casse comprise) passe devant une approchée
    assert locate_quote(segments, "Il pleut", "", "", near_seq=9).seq == 1
    assert locate_quote(segments, "absent", "", "", near_seq=1) is None
    # graphie modifiée entre deux révisions : repli normalisé
    assert locate_quote([(3, "Une ŒUVRE")], "une oeuvre", "", "", 0).seq == 3


# ------------------------------------------------------------------ plages HTTP
@pytest.mark.parametrize(
    ("header", "expected"),
    [
        (None, None),
        ("bytes=0-99", (0, 99)),
        ("bytes=900-", (900, 999)),
        ("bytes=-10", (990, 999)),
        ("bytes=990-5000", (990, 999)),
    ],
)
def test_parse_range(header, expected):
    assert parse_range(header, 1000) == expected


@pytest.mark.parametrize(
    "header", ["bytes=1000-", "bytes=5-2", "bytes=-0", "bytes=-", "items=0-1", "bytes=0-1,4-5"]
)
def test_parse_range_invalid(header):
    with pytest.raises(ValueError):
        parse_range(header, 1000)


# ---------------------------------------------------------- lecture parallèle
def test_group_pairs_many_to_many_and_orphans():
    links = [
        {"s_seq": 1, "t_seq": 1, "score": 0.9, "method": "m", "s_score": None},
        {"s_seq": 2, "t_seq": 2, "score": 0.8, "method": "m", "s_score": None},
        {"s_seq": 2, "t_seq": 3, "score": 0.6, "method": "m", "s_score": None},  # scindé en deux
        {"s_seq": 3, "t_seq": 4, "score": 0.7, "method": "m", "s_score": None},
        {"s_seq": 4, "t_seq": 4, "score": 0.7, "method": "m", "s_score": None},  # fusionnés
        {"s_seq": 5, "t_seq": None, "score": None, "method": None, "s_score": None},
    ]
    pairs = group_pairs(links)
    assert [(p["source_seqs"], p["target_seqs"]) for p in pairs] == [
        ([1], [1]),
        ([2], [2, 3]),
        ([3, 4], [4]),
        ([5], []),
    ]
    assert pairs[1]["score"] == 0.7
    with_orphans = insert_orphans(pairs, [0, 5])
    assert [p["target_seqs"] for p in with_orphans] == [[0], [1], [2, 3], [4], [], [5]]


# ------------------------------------------------------------------- recherche
def test_expand_languages():
    available = ["fr", "fr-CA", "en", "pt-BR"]
    assert expand_languages(["fr"], available) == ["fr", "fr-CA"]
    assert expand_languages(["fr-CA"], available) == ["fr-CA"]
    assert expand_languages(["pt"], available) == ["pt", "pt-BR"]
    assert expand_languages([], available) == []


def test_term_spans_match_stems():
    text = "He runs; running is fun; he ran."
    assert [text[a:b] for a, b in term_spans(text, "run", "en")] == ["runs", "running"]


def test_cursor_roundtrip():
    c = encode_cursor({"s": "title", "k": "les miserables", "id": "x"})
    assert decode_cursor(c) == {"s": "title", "k": "les miserables", "id": "x"}
    assert decode_cursor(None) is None
    with pytest.raises(Problem):
        decode_cursor("@@pas-un-curseur@@")


# ------------------------------------------------------------------------ auth
def test_principal_roles_and_access():
    reader = Principal("u", frozenset({READ}), is_user=True)
    assert reader.has(READ) and not reader.has(REVIEW) and not reader.has(ADMIN)
    assert reader.visible_access == ("open", "excerpt")
    assert reader.can_read_text("open") and not reader.can_read_text("excerpt")
    reviewer = Principal("u", frozenset({REVIEW}), is_user=True)
    assert reviewer.has(READ) and reviewer.has(REVIEW)
    admin = Principal("u", frozenset({ADMIN}), is_user=False)
    assert admin.has(REVIEW) and admin.can_read_text("restricted") and admin.can_see("restricted")


def test_principal_from_keycloak_claims():
    claims = {
        "sub": "abc",
        "azp": "reader-web",
        "preferred_username": "service-account-reader-web",
        "realm_access": {"roles": ["offline_access", "corpus:read"]},
        "resource_access": {"corpus-api": {"roles": ["corpus:review"]}, "account": {"roles": ["x"]}},
    }
    p = principal_from_claims(claims, "corpus-api")
    assert p.roles == {READ, REVIEW} and p.client_id == "reader-web" and not p.is_user


class FakeJWKS:
    def __init__(self, key):
        self.key = key

    def get_signing_key_from_jwt(self, token):
        return type("K", (), {"key": self.key})()


@pytest.fixture
def verifier():
    private = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    v = TokenVerifier(Settings(oidc_issuer="https://kc/realms/swann", oidc_audience="corpus-api"))
    v.__dict__["jwks"] = FakeJWKS(private.public_key())  # remplace la découverte OIDC
    return v, private


def make_token(private, **overrides):
    now = int(time.time())
    claims = {
        "sub": "u1",
        "iss": "https://kc/realms/swann",
        "aud": "corpus-api",
        "iat": now,
        "exp": now + 60,
        "resource_access": {"corpus-api": {"roles": ["corpus:read"]}},
        **overrides,
    }
    return jwt.encode(claims, private, algorithm="RS256")


def test_token_verifier_accepts_valid_token(verifier):
    v, private = verifier
    p = v.verify(make_token(private))
    assert p.sub == "u1" and p.roles == {READ} and p.is_user


@pytest.mark.parametrize(
    "overrides",
    [{"aud": "autre-api"}, {"iss": "https://pirate/realms/swann"}, {"exp": int(time.time()) - 10}],
)
def test_token_verifier_rejects(verifier, overrides):
    v, private = verifier
    with pytest.raises(Problem) as e:
        v.verify(make_token(private, **overrides))
    assert e.value.status == 401


def test_token_verifier_rejects_other_key(verifier):
    v, _ = verifier
    other = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    with pytest.raises(Problem):
        v.verify(make_token(other))
