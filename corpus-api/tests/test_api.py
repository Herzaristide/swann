"""Tests d'intégration sur une copie de la base (voir conftest.py)."""

from __future__ import annotations

import pytest
from conftest import as_principal

from swann_api.auth import READ, REVIEW


def first_edition(client):
    work = client.get("/v1/works", params={"limit": 1}).json()["items"][0]
    return client.get(f"/v1/works/{work['id']}").json()["editions"][0]


@pytest.fixture
def open_edition(db, client, admin):
    """Une édition passée en `open` dans la copie de test, avec ses points Qdrant
    inchangés (la recherche n'est pas testée sur elle)."""
    edition = first_edition(client)
    db.execute("UPDATE editions SET access = 'open' WHERE id = %s", (edition["id"],))
    yield edition
    db.execute("UPDATE editions SET access = 'restricted' WHERE id = %s", (edition["id"],))


# ---------------------------------------------------------------- authentification
def test_health_without_token(client):
    r = client.get("/v1/health")
    assert r.status_code == 200 and r.json()["checks"]["postgres"] == "ok"


def test_token_required(client):
    r = client.get("/v1/works")
    assert r.status_code == 401
    assert r.headers["content-type"] == "application/problem+json"
    assert r.headers["www-authenticate"].startswith("Bearer")


def test_openapi_published(client):
    spec = client.get("/v1/openapi.json").json()
    assert "/v1/search" in spec["paths"] and "/v1/editions/{edition_id}/segments" in spec["paths"]


# ------------------------------------------------------------------- droits
def test_reader_does_not_see_restricted(client, reader, db):
    restricted = db.execute("SELECT count(*) AS n FROM editions WHERE access = 'restricted'").fetchone()["n"]
    total = db.execute("SELECT count(*) AS n FROM editions").fetchone()["n"]
    assert restricted == total  # état du corpus de test : rien n'est encore ouvert
    assert client.get("/v1/works").json()["items"] == []
    assert client.get("/v1/languages").json() == []


def test_reader_sees_open_edition_text(client, open_edition, app):

    as_principal(app, READ, is_user=False)
    works = client.get("/v1/works").json()["items"]
    assert len(works) == 1 and works[0]["languages"] == [open_edition["language"]]
    r = client.get(f"/v1/editions/{open_edition['id']}/segments", params={"limit": 3})
    assert r.status_code == 200 and len(r.json()["segments"]) == 3
    # les autres éditions de l'œuvre restent invisibles
    detail = client.get(f"/v1/works/{works[0]['id']}").json()
    assert [e["id"] for e in detail["editions"]] == [open_edition["id"]]


def test_excerpt_edition_has_no_full_text(client, db, app, admin):
    edition = first_edition(client)
    db.execute("UPDATE editions SET access = 'excerpt' WHERE id = %s", (edition["id"],))
    try:
        as_principal(app, READ)
        assert client.get(f"/v1/editions/{edition['id']}").status_code == 200
        r = client.get(f"/v1/editions/{edition['id']}/segments")
        assert r.status_code == 403
        assert client.get(f"/v1/editions/{edition['id']}/epub").status_code == 403
    finally:
        db.execute("UPDATE editions SET access = 'restricted' WHERE id = %s", (edition["id"],))


def test_reviewer_role_required_for_reviews(client, reader):
    r = client.post(
        "/v1/alignment/reviews",
        json={
            "source": {"edition_id": "00000000-0000-0000-0000-000000000000", "seq": 0},
            "unit_id": "00000000-0000-0000-0000-000000000000",
            "verdict": "correct",
        },
    )
    assert r.status_code == 403


# ---------------------------------------------------------------- catalogue
def test_works_pagination_is_complete_and_stable(client, admin):
    seen, cursor = [], None
    while True:
        page = client.get("/v1/works", params={"limit": 5, "cursor": cursor}).json()
        seen += [w["id"] for w in page["items"]]
        cursor = page["next_cursor"]
        if cursor is None:
            break
    everything = [w["id"] for w in client.get("/v1/works", params={"limit": 100}).json()["items"]]
    assert seen == everything and len(set(seen)) == len(seen)


def test_works_sorted_by_year(client, admin):
    years = [
        w["first_published_year"] for w in client.get("/v1/works", params={"sort": "year"}).json()["items"]
    ]
    known = [y for y in years if y is not None]
    assert known == sorted(known)


def test_cursor_bound_to_sort(client, admin):
    cursor = client.get("/v1/works", params={"limit": 1}).json()["next_cursor"]
    assert client.get("/v1/works", params={"sort": "year", "cursor": cursor}).status_code == 400


# ---------------------------------------------------------------------- texte
def test_segments_revision_and_cache(client, admin):
    edition = first_edition(client)
    url = f"/v1/editions/{edition['id']}/segments"
    r = client.get(url, params={"limit": 2})
    assert r.status_code == 200 and r.headers["cache-control"] == "private, no-cache"
    assert (
        client.get(url, params={"limit": 2}, headers={"If-None-Match": r.headers["etag"]}).status_code == 304
    )
    r = client.get(url, params={"limit": 2, "rev": edition["revision"]})
    assert "immutable" in r.headers["cache-control"]
    r = client.get(url, params={"rev": edition["revision"] + 1})
    assert r.status_code == 409 and r.json()["current_revision"] == edition["revision"]


def test_toc_ranges_cover_segments(client, admin):
    edition = first_edition(client)
    toc = client.get(f"/v1/editions/{edition['id']}/toc").json()
    for section in toc["sections"]:
        for child in section["children"]:
            if child["seq_start"] is not None:
                assert section["seq_start"] <= child["seq_start"] <= child["seq_end"] <= section["seq_end"]


def test_anchor_relocated_after_text_change(client, db, admin):
    edition = first_edition(client)
    seg = client.get(f"/v1/editions/{edition['id']}/segments", params={"from_seq": 50, "limit": 1}).json()[
        "segments"
    ][0]
    quote = {"exact": seg["text"][10:40], "prefix": seg["text"][:10], "suffix": seg["text"][40:60]}
    # Les chunks référencent les seq (clé étrangère) : copie jetable, on les retire.
    db.execute("DELETE FROM chunks WHERE edition_id = %s", (edition["id"],))
    # Simule une nouvelle révision : un segment inséré décale tout le texte.
    db.execute(
        "UPDATE segments SET seq = seq + 1000000 WHERE edition_id = %s AND seq >= 10", (edition["id"],)
    )
    db.execute(
        "UPDATE segments SET seq = seq - 1000000 + 1 WHERE edition_id = %s AND seq >= 1000000",
        (edition["id"],),
    )
    db.execute("UPDATE editions SET revision = revision + 1 WHERE id = %s", (edition["id"],))
    try:
        r = client.post(
            f"/v1/editions/{edition['id']}/anchors:resolve",
            json={
                "anchors": [
                    {"key": "k", "revision": edition["revision"], "seq": 50, "offset": 10, "quote": quote}
                ]
            },
        ).json()
        assert r["revision"] == edition["revision"] + 1
        assert r["results"][0] == {"key": "k", "status": "relocated", "seq": 51, "offset": 10}
    finally:
        db.execute(
            "UPDATE segments SET seq = seq + 1000000 WHERE edition_id = %s AND seq >= 11", (edition["id"],)
        )
        db.execute(
            "UPDATE segments SET seq = seq - 1000000 - 1 WHERE edition_id = %s AND seq >= 1000000",
            (edition["id"],),
        )
        db.execute("UPDATE editions SET revision = revision - 1 WHERE id = %s", (edition["id"],))


def test_epub_served_with_ranges(client, admin):
    edition = first_edition(client)
    url = f"/v1/editions/{edition['id']}/epub"
    head = client.head(url)
    if head.status_code == 404:
        pytest.skip("EPUB absent du stockage S3")
    full = client.get(url)
    assert full.content[:2] == b"PK" and len(full.content) == int(head.headers["content-length"])
    part = client.get(url, headers={"Range": "bytes=10-19"})
    assert part.status_code == 206 and part.content == full.content[10:20]
    assert client.get(url, headers={"Range": f"bytes={len(full.content)}-"}).status_code == 416


# ------------------------------------------------------------------- recherche
def test_words_search_needs_no_encoder(client, admin):
    r = client.post("/v1/search", json={"q": "the", "mode": "words", "limit": 3})
    assert r.status_code == 200
    hits = r.json()["hits"]
    assert hits and all(h["passage"]["highlights"] for h in hits)
    assert len({h["work"]["id"] for h in hits}) == len(hits)  # un passage par œuvre


def test_theme_search_without_encoder_is_503(client, admin):
    r = client.post("/v1/search", json={"q": "la mer", "mode": "theme"})
    assert r.status_code == 503 and r.json()["type"].endswith("no-encoder")


def test_reader_search_skips_restricted(client, reader):
    assert client.post("/v1/search", json={"q": "the", "mode": "words"}).json()["hits"] == []


# ----------------------------------------------------------------- alignement
def aligned_work(db):
    return db.execute("SELECT u.work_id FROM work_units u GROUP BY u.work_id LIMIT 1").fetchone()


def test_review_and_manual_link(client, db, app, admin):
    work = aligned_work(db)
    if work is None:
        pytest.skip("aucune œuvre alignée")
    # Un relecteur ne voit que les textes ouverts : on ouvre l'œuvre dans la copie.
    db.execute("UPDATE editions SET access = 'open' WHERE work_id = %s", (work["work_id"],))
    try:
        review_and_link(client, db, app)
    finally:
        db.execute("UPDATE editions SET access = 'restricted' WHERE work_id = %s", (work["work_id"],))


def review_and_link(client, db, app):
    as_principal(app, REVIEW, sub="relecteur-1")
    link = client.get("/v1/alignment/sample").json()
    source = {"edition_id": link["source"]["edition_id"], "seq": link["source"]["seq"]}
    r = client.post(
        "/v1/alignment/reviews", json={"source": source, "unit_id": link["unit_id"], "verdict": "incorrect"}
    )
    assert r.status_code == 201
    row = db.execute(
        "SELECT reviewer_sub, alignment_method FROM alignment_reviews WHERE id = %s", (r.json()["id"],)
    ).fetchone()
    assert row == {"reviewer_sub": "relecteur-1", "alignment_method": link["method"]}

    last = db.execute("SELECT max(id) AS id FROM corpus_events").fetchone()["id"]
    assert (
        client.post("/v1/alignment/links", json={"source": source, "unit_id": link["unit_id"]}).status_code
        == 204
    )
    manual = db.execute(
        "SELECT method, created_by_sub FROM segment_alignments sa JOIN segments s ON s.id = sa.segment_id "
        "WHERE s.edition_id = %s AND s.seq = %s AND sa.unit_id = %s",
        (source["edition_id"], source["seq"], link["unit_id"]),
    ).fetchone()
    assert manual == {"method": "manual", "created_by_sub": "relecteur-1"}
    changes = client.get("/v1/changes", params={"after": str(last)}).json()["items"]
    assert [c["type"] for c in changes] == ["work.alignment_changed"]

    # un compte de service ne peut pas relire
    as_principal(app, REVIEW, is_user=False)
    assert (
        client.post(
            "/v1/alignment/reviews", json={"source": source, "unit_id": link["unit_id"], "verdict": "correct"}
        ).status_code
        == 403
    )


def test_parallel_pairs_follow_source_order(client, db, admin):
    row = aligned_work(db)
    if row is None:
        pytest.skip("aucune œuvre alignée")
    al = client.get(f"/v1/works/{row['work_id']}/alignment").json()
    ref = next(e for e in al["editions"] if e["is_reference"])
    other = next(e for e in al["editions"] if not e["is_reference"])
    par = client.get(
        f"/v1/editions/{ref['edition_id']}/parallel",
        params={"target": other["edition_id"], "from_seq": 0, "to_seq": 99},
    ).json()
    sources = [p["source_seqs"][0] for p in par["pairs"] if p["source_seqs"]]
    assert sources == sorted(sources) and sources[0] == 0
    cp = client.get(
        f"/v1/editions/{ref['edition_id']}/counterpart", params={"seq": 50, "target": other["edition_id"]}
    )
    assert cp.status_code == 200 and cp.json()["via"] in ("segment", "section")
