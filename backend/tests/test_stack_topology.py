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


# ── Ownership is relative to the project (the map called Graphban "ours" everywhere) ──

def _roster(agents):
    online = sum(1 for a in agents if a.get("state") != "offline")
    return {"agents": agents, "online": online, "total": len(agents), "posture": "fleet"}


def _clear_code_map(db, project_id="core"):
    from app.models import CodeNode

    db.query(CodeNode).filter(CodeNode.project_id == project_id).delete()
    db.commit()


def _describe(db, *paths, project_id="core"):
    from app.services import code_graph

    for p in paths:
        code_graph.upsert_node(db, project_id=project_id, path=p, kind="file")
    db.commit()


GRAPHBAN_BOXES = ("api", "web", "fleet", "gbagent", "cli")


def test_without_a_code_map_no_graphban_box_is_code_we_write(db, monkeypatch):
    monkeypatch.setattr("app.services.fleet.fleet_status", lambda *_a, **_k: _roster([]))
    _clear_code_map(db)
    body = stack_svc.topology(db, "core")
    by_id = {n["id"]: n for n in body["nodes"]}
    for nid in GRAPHBAN_BOXES:
        assert by_id[nid]["ring"] == stack_svc.RING_INFRA, nid
        source = next(f for f in by_id[nid]["facts"] if f["key"] == "source")
        assert "unknown" in source["value"], nid
    assert any(e["id"] == "own_code" for e in body["empties"])


def test_a_box_is_code_we_write_only_where_its_source_is_in_the_code_map(db, monkeypatch):
    monkeypatch.setattr("app.services.fleet.fleet_status", lambda *_a, **_k: _roster([]))
    _clear_code_map(db)
    _describe(db, "fleet/src/gbfleet/until.py", "cli/src/gban/cli.py::main")
    body = stack_svc.topology(db, "core")
    rings = {n["id"]: n["ring"] for n in body["nodes"]}
    assert rings["fleet"] == stack_svc.RING_CODE
    assert rings["cli"] == stack_svc.RING_CODE
    assert rings["api"] == stack_svc.RING_INFRA
    assert rings["web"] == stack_svc.RING_INFRA
    assert rings["gbagent"] == stack_svc.RING_INFRA
    cli = next(n for n in body["nodes"] if n["id"] == "cli")
    source = next(f for f in cli["facts"] if f["key"] == "source")
    assert "cli/src/gban" in source["value"]
    api = next(n for n in body["nodes"] if n["id"] == "api")
    assert "tool this project runs" in next(f for f in api["facts"] if f["key"] == "source")["value"]
    assert not any(e["id"] == "own_code" for e in body["empties"])


def test_a_generic_fastapi_vite_layout_does_not_claim_graphban(db, monkeypatch):
    # backend/app and web/src are everybody's layout; only Graphban-specific markers count.
    monkeypatch.setattr("app.services.fleet.fleet_status", lambda *_a, **_k: _roster([]))
    _clear_code_map(db)
    _describe(db, "backend/app/main.py", "web/src/App.tsx", "fleet/README.md",
              "backend/app/mcp_server_utils.py")
    body = stack_svc.topology(db, "core")
    for n in body["nodes"]:
        if n["id"] in GRAPHBAN_BOXES:
            assert n["ring"] == stack_svc.RING_INFRA, n["id"]


def test_gbfleet_gbagent_and_gban_are_separate_boxes(db, monkeypatch):
    agents = [
        {"id": "a1", "state": "working", "capabilities": {"vendor": "gbagent", "host": "mac"}},
        {"id": "a2", "state": "idle", "capabilities": {"vendor": "anthropic", "host": "mac"}},
        {"id": "a3", "state": "offline", "capabilities": {"vendor": "gbagent", "host": "mac"}},
    ]
    monkeypatch.setattr("app.services.fleet.fleet_status", lambda *_a, **_k: _roster(agents))
    _clear_code_map(db)
    _describe(db, "fleet/src/gbagent/cli.py")
    body = stack_svc.topology(db, "core")
    by_id = {n["id"]: n for n in body["nodes"]}
    assert by_id["fleet"]["label"] == "gbfleet"
    assert by_id["cli"]["label"] == "gban"
    assert by_id["gbagent"]["label"] == "gbagent"
    assert by_id["gbagent"]["liveness"] == "reachable"
    assert by_id["gbagent"]["ring"] == stack_svc.RING_CODE
    # Vendor CLIs count only non-gbagent children and are never anyone's code.
    vendor = by_id["vendor_agents"]
    assert vendor["ring"] == stack_svc.RING_INFRA
    assert next(f for f in vendor["facts"] if f["key"] == "online")["value"] == "1"
    assert any(e["from"] == "fleet" and e["to"] == "gbagent" for e in body["edges"])
    assert any(e["from"] == "gbagent" and e["to"] == "api" for e in body["edges"])


def test_an_idle_gbagent_is_unknown_not_unreachable(db, monkeypatch):
    agents = [{"id": "a2", "state": "idle", "capabilities": {"vendor": "anthropic"}}]
    monkeypatch.setattr("app.services.fleet.fleet_status", lambda *_a, **_k: _roster(agents))
    body = stack_svc.topology(db, "core")
    gbagent = next(n for n in body["nodes"] if n["id"] == "gbagent")
    assert gbagent["liveness"] == "unknown"


def test_every_source_marker_exists_in_this_repo():
    # A rename that strands a marker would quietly turn Graphban's own project into
    # "a tool it runs". Each marker must still name a real path in the checkout.
    from pathlib import Path

    root = Path(__file__).resolve().parents[2]
    for node_id, src in stack_svc.GRAPHBAN_SOURCES.items():
        for marker in src["markers"]:
            assert (root / marker).exists(), f"{node_id}: {marker} is not in the repo"
