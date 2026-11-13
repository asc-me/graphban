"""GRPH-1597: path counts and the serialisation preview behind the rule editors."""
from __future__ import annotations

import pytest

from app.services import code_graph, items as items_svc


def _login(client):
    r = client.post("/api/auth/login", json={"email": "alex@ascme-labs.com", "password": "graphban"})
    assert r.status_code == 200, r.text
    return {"Authorization": f"Bearer {r.json()['access_token']}"}


@pytest.fixture()
def db(client):
    from app.db import SessionLocal

    s = SessionLocal()
    try:
        yield s
    finally:
        s.close()


def _nodes(db, project, paths):
    for p in paths:
        code_graph.upsert_node(db, project_id=project, path=p, kind="file")
    db.commit()


def _count(client, auth, project, *paths):
    r = client.get(f"/api/projects/{project}/paths/count", params={"path": list(paths)},
                   headers=auth)
    assert r.status_code == 200, r.text
    return r.json()


def test_unindexed_project_answers_null_never_zero(client, db):
    auth = _login(client)
    body = _count(client, auth, "core", "db/migrations")
    assert body["indexed"] is False
    assert body["paths"][0]["files"] is None


def test_counts_read_prefixes_and_globs_like_the_rules(client, db):
    auth = _login(client)
    _nodes(db, "core", ["db/migrations/0001.sql", "db/migrations/0002.sql", "db/other.py",
                        "web/a.ts"])
    body = _count(client, auth, "core", "db/migrations", "db/migrations/**", "db/migrations/",
                  "nothing/here", "**", "db/*.py")
    assert body["indexed"] is True
    got = [p["files"] for p in body["paths"]]
    # a trailing glob or slash is ignored; an unmatched path is 0 (indexed), `**` has no prefix
    assert got == [2, 2, 2, 0, None, 1]


def test_preview_names_only_items_the_candidate_would_serialise(client, db):
    auth = _login(client)
    a = items_svc.create_item(db, title="one", project_id="core", status="next",
                              touchpoints=["db/migrations/0001.sql"])
    b = items_svc.create_item(db, title="two", project_id="core", status="next",
                              touchpoints=["db/migrations/*"])
    items_svc.create_item(db, title="three", project_id="core", status="next",
                          touchpoints=["web/a.ts"])
    db.commit()
    r = client.get("/api/rules/exclusive_areas/preview",
                   params={"project_id": "core", "path": ["db/migrations/**"]}, headers=auth)
    assert r.status_code == 200, r.text
    ids = {i["id"] for i in r.json()["serialised"]}
    assert ids == {a.key, b.key}
    r = client.get("/api/rules/exclusive_areas/preview",
                   params={"project_id": "core", "path": ["elsewhere"]}, headers=auth)
    assert r.json()["serialised"] == []
