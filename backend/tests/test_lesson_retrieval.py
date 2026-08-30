"""Org-reach retrieval widening, tested with a fixture row — not inert until promote.

A reach=org shard of A must surface from sibling B in search_memory AND list_shards
AND list_lessons. A project-local sibling must not. Both engines: SQLite cosine and
Postgres <=> . Sabotaging only one path leaves the other green.
"""
import pytest

from app.models import MemoryShard, Organization, OrgMembership, Project
from app.services import memory as mem_svc


@pytest.fixture()
def db(client):
    from app.db import SessionLocal

    s = SessionLocal()
    try:
        yield s
    finally:
        s.close()


ORG_TEXT = "ORG-REACH-NEEDLE unique lesson about pgvector widening"
LOCAL_TEXT = "PROJECT-LOCAL-NEEDLE stays in its originating project"


def _promote_reach(db, shard, reach="org"):
    shard.reach = reach
    db.commit()
    db.refresh(shard)
    return shard


def test_fixture_org_reach_is_visible_from_a_sibling(db, decoy):
    """Do not wait for promote. Insert reach=org and the sibling must already see it."""
    from tests.decoy import assert_populated
    assert_populated(db, decoy)
    sibling = decoy["project_id"]

    org_shard = mem_svc.add_memory(
        db, text_body=ORG_TEXT, project_id="core", status="published",
    )
    _promote_reach(db, org_shard, "org")
    local = mem_svc.add_memory(
        db, text_body=LOCAL_TEXT, project_id="core", status="published",
    )

    listed = [s.id for s in mem_svc.list_shards(db, project_id=sibling)]
    assert org_shard.id in listed, "list_shards must widen by reach=org"
    assert local.id not in listed, "project-local of A must not appear on B"

    hits = mem_svc.search_memory(db, ORG_TEXT, top_k=20, project_id=sibling)
    assert any(s.id == org_shard.id for s, _ in hits), "search_memory must widen by reach=org"
    local_hits = mem_svc.search_memory(db, LOCAL_TEXT, top_k=20, project_id=sibling)
    assert all(s.id != local.id for s, _ in local_hits)

    lessons = mem_svc.list_lessons(
        db, sibling, readable_project_ids={"core", sibling},
        filters={}, limit=50, offset=0,
    )
    ids = [r["id"] for r in lessons["results"]]
    assert org_shard.id in ids, "list_lessons must widen by reach=org"
    assert local.id not in ids


def test_get_lessons_empty_catalog_is_not_an_effectiveness_claim(client, auth):
    pid = client.post("/api/projects", json={"name": "Empty lessons"}, headers=auth).json()["id"]
    r = client.get(f"/api/memory/lessons?project_id={pid}", headers=auth)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["results"] == []
    assert body["total"] == 0
    assert "score" not in body
    assert body["enums"]["caught_states"] == list(mem_svc.CAUGHT_STATES)
    assert body["enums"]["unclassified_filter"] == "unclassified"


def test_get_lessons_calls_effectiveness_on_empty_outcomes(client, auth):
    """Sabotaging lesson_effectiveness to return 1.0 on [] must fail this.

    Seeded published shards have no outcomes. The GET must actually call the
    scorer — hardcoding unknown on the router would leave a sabotaged callee green.
    """
    r = client.get("/api/memory/lessons?project_id=core", headers=auth)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["results"], "core seed has published shards; empty here hides the caller"
    for row in body["results"]:
        assert row["effectiveness"]["score"] is None
        assert row["caught_state"] == "unknown"
        assert row["effectiveness"]["trend"] == "unmeasured"
        assert "history" not in row["effectiveness"]
        assert "history" not in row
        assert row["age_state"] in ("fresh", "stale")


def test_get_lessons_requires_auth(client):
    assert client.get("/api/memory/lessons?project_id=core").status_code == 401


def test_get_lessons_unclassified_filter_is_stored_empty(client, auth, db):
    shard = mem_svc.add_memory(
        db, text_body="unclassified published lesson xyzzy", project_id="core",
        status="published",
    )
    r = client.get(
        "/api/memory/lessons?project_id=core&lesson_class=unclassified",
        headers=auth,
    )
    assert r.status_code == 200
    ids = [row["id"] for row in r.json()["results"]]
    assert shard.id in ids
    assert all(row["lesson_class"] == "" for row in r.json()["results"])


def test_rest_add_shard_stamps_non_ingest_attribution(client, auth, db):
    r = client.post(
        "/api/memory/shards",
        json={"text": "human-authored lesson attribution", "project_id": "core"},
        headers=auth,
    )
    assert r.status_code == 201, r.text
    shard = db.get(MemoryShard, r.json()["id"])
    assert shard.actor_user_id == "u1"
    assert shard.attributed_project_id == "core"
    assert shard.reach == "project"


def test_import_leaves_reach_project_and_attribution_null(db):
    n = mem_svc.import_shards(db, [{"text": "old dump row", "scope": "global"}], project_id="core")
    assert n == 1
    imported = [s for s in mem_svc.list_shards(db, project_id="core") if s.text == "old dump row"]
    assert imported
    assert imported[0].reach == "project"
    assert imported[0].actor_user_id is None
    assert imported[0].attributed_project_id is None


def test_hosted_org_reach_does_not_cross_tenants(db, monkeypatch):
    """NULL org_id matching other NULL org_ids would leak. Same-org only."""
    from app.config import settings
    from app.models import Membership

    db.add_all([Organization(id="orgA", name="Org A"), Organization(id="orgB", name="Org B")])
    db.flush()
    db.add_all([
        Project(id="projA", name="Proj A", tag="PJA", org_id="orgA"),
        Project(id="projB", name="Proj B", tag="PJB", org_id="orgB"),
        Membership(user_id="u1", project_id="projA", access="write"),
        Membership(user_id="u1", project_id="projB", access="write"),
        OrgMembership(org_id="orgA", user_id="u1", role="owner"),
        OrgMembership(org_id="orgB", user_id="u1", role="owner"),
    ])
    db.commit()

    needle = mem_svc.add_memory(
        db, text_body="TENANT-ORG-NEEDLE must not leak", project_id="projA",
        status="published",
    )
    _promote_reach(db, needle, "org")

    monkeypatch.setattr(settings, "hosted_mode", True)
    listed_b = [s.id for s in mem_svc.list_shards(db, project_id="projB")]
    assert needle.id not in listed_b
    hits_b = mem_svc.search_memory(
        db, "TENANT-ORG-NEEDLE must not leak", top_k=20, project_id="projB")
    assert all(s.id != needle.id for s, _ in hits_b)
    lessons_b = mem_svc.list_lessons(
        db, "projB", readable_project_ids={"projA", "projB"},
        filters={}, limit=50, offset=0,
    )
    assert needle.id not in [r["id"] for r in lessons_b["results"]]

    listed_a = [s.id for s in mem_svc.list_shards(db, project_id="projA")]
    assert needle.id in listed_a
