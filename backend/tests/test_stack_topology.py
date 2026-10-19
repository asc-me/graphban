"""GRPH-1034 — derived Stack topology aggregator.

Absence must not read as clean: empty fleet, unwired providers, and missing
schema revision are named. MCP is a surface on api, not its own box. pgvector
is an extension inside db when present.
"""
from __future__ import annotations

import pytest

from app.mcp_server import LIVE_TOOL_COUNT
from app.services import stack as stack_svc


@pytest.fixture()
def db(client):
    from app.db import SessionLocal

    s = SessionLocal()
    try:
        yield s
    finally:
        s.close()


def test_stack_endpoint_returns_derived_topology(client, auth):
    r = client.get("/api/platform/stack?project_id=core", headers=auth)
    assert r.status_code == 200, r.text
    body = r.json()

    assert "nodes" in body and "edges" in body and "hosts" in body
    assert "empties" in body and "catalog" in body and "identity" in body

    by_id = {n["id"]: n for n in body["nodes"]}
    assert "api" in by_id and "web" in by_id and "db" in by_id
    assert "fleet" in by_id and "cli" in by_id

    api = by_id["api"]
    assert api["liveness"] == "reachable"
    assert api["scope"] == "instance"
    assert "MCP" in api["surfaces"]
    assert "REST" in api["surfaces"]
    # MCP must NOT be its own box.
    assert "mcp" not in by_id

    assert any(e["from"] == "web" and e["to"] == "api" for e in body["edges"])
    assert any(e["from"] == "api" and e["to"] == "db" for e in body["edges"])

    assert body["identity"]["mcp_tools"] == LIVE_TOOL_COUNT
    assert body["catalog"]["providers_total"] >= 1
    assert isinstance(body["catalog"]["unwired_count"], int)


def test_alembic_head_accessor_does_not_raise(db):
    # On SQLite create_all there is usually no alembic_version — None is absence.
    head = stack_svc.alembic_head(db)
    assert head is None or isinstance(head, str)


def test_topology_names_empty_fleet_when_nobody_is_online(client, auth, monkeypatch):
    def empty_roster(*_a, **_k):
        return {"agents": [], "online": 0, "total": 0, "posture": "single-agent"}

    monkeypatch.setattr("app.services.fleet.fleet_status", empty_roster)
    r = client.get("/api/platform/stack?project_id=core", headers=auth)
    assert r.status_code == 200, r.text
    body = r.json()
    fleet = next(n for n in body["nodes"] if n["id"] == "fleet")
    assert fleet["liveness"] == "not_configured"
    assert any(e["id"] == "fleet" for e in body["empties"])
    assert any("no fleet" in e["message"] for e in body["empties"])


def test_pgvector_is_extension_not_peer_box(client, auth):
    r = client.get("/api/platform/stack?project_id=core", headers=auth)
    assert r.status_code == 200, r.text
    body = r.json()
    by_id = {n["id"]: n for n in body["nodes"]}
    assert "pgvector" not in by_id
    assert "db" in by_id
    assert isinstance(by_id["db"]["extensions"], list)


def test_stack_refuses_unreadable_project(client, auth, monkeypatch):
    from app.config import settings

    monkeypatch.setattr(settings, "hosted_mode", True)
    r = client.get("/api/platform/stack?project_id=does-not-exist-zz", headers=auth)
    assert r.status_code in (403, 404)


def test_liveness_never_defaults_missing_web_sha_to_reachable(db, monkeypatch):
    monkeypatch.setattr(stack_svc, "_web_bundle_sha", lambda: None)
    monkeypatch.setattr(
        "app.services.fleet.fleet_status",
        lambda *_a, **_k: {"agents": [], "online": 0, "total": 0, "posture": "single-agent"},
    )
    body = stack_svc.topology(db, "core")
    web = next(n for n in body["nodes"] if n["id"] == "web")
    assert web["liveness"] == "unknown"
