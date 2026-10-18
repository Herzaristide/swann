"""Atelier d'alignement (/v1/admin/alignment), sur la copie jetable."""

from __future__ import annotations

import pytest

from swann_api.routers.admin_alignment import wilson


@pytest.fixture(scope="module")
def aligned(db):
    row = db.execute(
        """
        SELECT ea.edition_id, ea.reference_edition_id, e.work_id
        FROM edition_alignments ea JOIN editions e ON e.id = ea.edition_id
        WHERE ea.reference_edition_id IS NOT NULL AND ea.n_aligned_segments > 20
        ORDER BY ea.aligned_ratio DESC LIMIT 1
        """
    ).fetchone()
    if row is None:
        pytest.skip("aucune édition alignée dans la base modèle")
    return row


def test_requires_review_role(client, reader):
    assert client.get("/v1/admin/alignment/editions").status_code == 403


def test_list_and_map(client, reviewer, aligned):
    editions = client.get("/v1/admin/alignment/editions").json()
    row = next(e for e in editions if e["edition_id"] == str(aligned["edition_id"]))
    assert row["reference_edition_id"] == str(aligned["reference_edition_id"])
    assert row["n_aligned_segments"] <= row["n_segments"]

    m = client.get(f"/v1/admin/alignment/editions/{aligned['edition_id']}/map").json()
    assert m["sections"] and sum(s["n_aligned"] for s in m["sections"]) > 0
    assert all(s["n_aligned"] <= s["n_segments"] for s in m["sections"])


def test_workbench_and_next(client, reviewer, aligned):
    first = client.get(
        f"/v1/admin/alignment/editions/{aligned['edition_id']}/next", params={"threshold": 1}
    ).json()
    assert first["seq"] is not None
    r = client.get(
        f"/v1/admin/alignment/editions/{aligned['edition_id']}/workbench",
        params={"from_seq": first["seq"], "limit": 20},
    )
    assert r.status_code == 200, r.text
    bench = r.json()
    assert bench["target"][0]["seq"] >= first["seq"]
    ref_units = {x["unit_id"] for x in bench["reference"] if x["unit_id"]}
    linked = [link for t in bench["target"] for link in t["links"]]
    assert linked and all(
        link["unit_id"] in ref_units for link in linked if link["reference_seq"] is not None
    )

    ref = client.get(f"/v1/admin/alignment/editions/{aligned['reference_edition_id']}/workbench")
    assert ref.status_code == 409


def test_replace_links_and_recount(client, reviewer, aligned, db):
    bench = client.get(
        f"/v1/admin/alignment/editions/{aligned['edition_id']}/workbench", params={"limit": 60}
    ).json()
    target = next(t for t in bench["target"] if t["links"] and t["kind"] != "heading")
    refs = [x["seq"] for x in bench["reference"] if x["unit_id"]][:2]
    before = db.execute(
        "SELECT n_aligned_segments FROM edition_alignments WHERE edition_id = %s", (aligned["edition_id"],)
    ).fetchone()

    r = client.post(
        "/v1/admin/alignment/links/replace",
        json={"edition_id": str(aligned["edition_id"]), "seq": target["seq"], "reference_seqs": refs},
    )
    assert r.status_code == 200, r.text
    out = r.json()
    assert out["manual"] and sorted(link["reference_seq"] for link in out["links"]) == sorted(refs)
    assert all(link["created_by_sub"] == "relecteur-1" for link in out["links"])
    event = db.execute("SELECT * FROM corpus_events ORDER BY id DESC LIMIT 1").fetchone()
    assert event["type"] == "work.alignment_changed" and event["actor"] == "relecteur-1"

    # Segment sans correspondant
    r = client.post(
        "/v1/admin/alignment/links/replace",
        json={"edition_id": str(aligned["edition_id"]), "seq": target["seq"], "reference_seqs": []},
    )
    assert r.json()["links"] == []
    after = db.execute(
        "SELECT n_aligned_segments FROM edition_alignments WHERE edition_id = %s", (aligned["edition_id"],)
    ).fetchone()
    assert after["n_aligned_segments"] == before["n_aligned_segments"] - 1

    bad = client.post(
        "/v1/admin/alignment/links/replace",
        json={"edition_id": str(aligned["edition_id"]), "seq": target["seq"], "reference_seqs": [10**7]},
    )
    assert bad.status_code == 400


def test_status_and_precision(client, reviewer, aligned, db):
    r = client.post(
        f"/v1/admin/alignment/editions/{aligned['edition_id']}/status", json={"status": "rejected"}
    )
    assert r.status_code == 200
    assert next(e for e in r.json() if e["edition_id"] == str(aligned["edition_id"]))["status"] == "rejected"

    links = db.execute(
        """
        SELECT sa.segment_id, sa.unit_id FROM segment_alignments sa JOIN segments s ON s.id = sa.segment_id
        WHERE s.edition_id = %s AND sa.method <> 'manual' LIMIT 4
        """,
        (aligned["edition_id"],),
    ).fetchall()
    for link, verdict in zip(links, ["correct", "correct", "partial", "incorrect"], strict=True):
        db.execute(
            "INSERT INTO alignment_reviews (segment_id, unit_id, verdict, is_sample, alignment_method) "
            "VALUES (%s, %s, %s, true, 'test-method')",
            (link["segment_id"], link["unit_id"], verdict),
        )
    stats = {p["method"]: p for p in client.get("/v1/admin/alignment/precision").json()}["test-method"]
    assert stats["reviews"] == 4 and stats["precision"] == pytest.approx(2.5 / 4)
    assert 0 <= stats["wilson_low"] < 0.5 < stats["wilson_high"] <= 1


def test_wilson():
    assert wilson(0, 0) == (None, None)
    low, high = wilson(95, 100)
    assert 0.88 < low < 0.9 and 0.97 < high < 0.99
