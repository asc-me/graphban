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


def _source_fact(body, nid):
    node = next(n for n in body["nodes"] if n["id"] == nid)
    return next(f for f in node["facts"] if f["key"] == "source")["value"]


def test_one_described_marker_makes_every_graphban_box_code_we_write(db, monkeypatch):
    # The code map is partial. gbagent used to draw as "a tool this project runs" on
    # Graphban's own project because no gbagent file had been described, while fleet —
    # same package — drew as ours. Every box ships from one repository, so one marker is
    # evidence for all of them.
    monkeypatch.setattr("app.services.fleet.fleet_status", lambda *_a, **_k: _roster([]))
    _clear_code_map(db)
    _describe(db, "fleet/src/gbfleet/until.py")
    body = stack_svc.topology(db, "core")
    rings = {n["id"]: n["ring"] for n in body["nodes"]}
    for nid in GRAPHBAN_BOXES:
        assert rings[nid] == stack_svc.RING_CODE, nid
    assert body["graphban_source"] == stack_svc.OWNED
    assert not any(e["id"] == "own_code" for e in body["empties"])


def test_an_inferred_box_says_which_marker_made_it_ours(db, monkeypatch):
    monkeypatch.setattr("app.services.fleet.fleet_status", lambda *_a, **_k: _roster([]))
    _clear_code_map(db)
    _describe(db, "fleet/src/gbfleet/until.py", "cli/src/gban/cli.py::main")
    body = stack_svc.topology(db, "core")
    # A direct hit names its own marker.
    assert _source_fact(body, "cli") == (
        "graphban-cli — this project writes it (cli/src/gban is in its code map)"
    )
    # An inferred one names the marker that matched elsewhere, and that its own is absent.
    gbagent = _source_fact(body, "gbagent")
    assert "this project writes it" in gbagent
    assert "(fleet/src/gbfleet)" in gbagent
    assert "none of gbagent's own files (fleet/src/gbagent) are described yet" in gbagent
    assert "tool this project runs" not in gbagent
    assert "none of" in _source_fact(body, "api")


def test_graphbans_own_project_draws_no_package_dir_beside_an_inferred_box(db, monkeypatch):
    monkeypatch.setattr("app.services.fleet.fleet_status", lambda *_a, **_k: _roster([]))
    _clear_code_map(db)
    # Only api's marker is described; fleet/ and cli/ are owned by inference, so their
    # manifests are Graphban's packages, not app boxes of their own.
    _describe(db, "backend/app/mcp_server.py", "fleet/pyproject.toml", "cli/pyproject.toml")
    assert _own(stack_svc.topology(db, "core")) == {}


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


# ── The project's own deployables come from its code map's deploy files ──

def _own(body):
    return {n["id"]: n for n in body["nodes"] if n["host"] == stack_svc.HOST_PROJECT}


def test_a_projects_dockerfiles_and_manifests_draw_its_own_deployables(db, monkeypatch):
    monkeypatch.setattr("app.services.fleet.fleet_status", lambda *_a, **_k: _roster([]))
    _clear_code_map(db)
    _describe(db, "frontend/package.json", "frontend/src/main.tsx",
              "server/Dockerfile", "server/pyproject.toml", "docker-compose.yml")
    body = stack_svc.topology(db, "core")
    own = _own(body)
    assert set(own) == {"app:frontend", "app:server"}
    server = own["app:server"]
    assert server["ring"] == stack_svc.RING_CODE
    assert server["scope"] == "project"
    assert server["liveness"] == "unknown"
    facts = {f["key"]: f["value"] for f in server["facts"]}
    assert facts["packaging"] == "container image"
    assert "file:server/Dockerfile" in facts and "file:server/pyproject.toml" in facts
    assert {f["key"]: f["value"] for f in own["app:frontend"]["facts"]}["packaging"] == "package"
    assert any(h["id"] == stack_svc.HOST_PROJECT and set(h["nodes"]) == set(own)
               for h in body["hosts"])
    assert not any(e["id"] == "own_code" for e in body["empties"])
    # Graphban's boxes are still the tools this project runs.
    assert {n["id"]: n["ring"] for n in body["nodes"]}["api"] == stack_svc.RING_INFRA


def test_fixtures_examples_and_node_modules_are_not_deployables(db, monkeypatch):
    monkeypatch.setattr("app.services.fleet.fleet_status", lambda *_a, **_k: _roster([]))
    _clear_code_map(db)
    _describe(db, "app/package.json", "tests/fixtures/demo/package.json",
              "examples/basic/Dockerfile", "app/node_modules/left-pad/package.json",
              "docs/site/package.json", "app/package.json::scripts")
    assert set(_own(stack_svc.topology(db, "core"))) == {"app:app"}


def test_a_workspace_root_manifest_is_dropped_when_packages_sit_below_it(db, monkeypatch):
    monkeypatch.setattr("app.services.fleet.fleet_status", lambda *_a, **_k: _roster([]))
    _clear_code_map(db)
    _describe(db, "package.json", "packages/api/package.json", "packages/ui/package.json")
    assert set(_own(stack_svc.topology(db, "core"))) == {"app:packages/api", "app:packages/ui"}
    _clear_code_map(db)
    _describe(db, "package.json", "src/index.ts")
    root = _own(stack_svc.topology(db, "core"))
    assert set(root) == {"app:."}
    assert root["app:."]["title"].endswith("(repo root)")


def test_graphbans_own_project_does_not_draw_backend_beside_api(db, monkeypatch):
    monkeypatch.setattr("app.services.fleet.fleet_status", lambda *_a, **_k: _roster([]))
    _clear_code_map(db)
    _describe(db, "backend/Dockerfile", "backend/pyproject.toml", "backend/app/mcp_server.py",
              "web/Dockerfile", "web/package.json", "web/src/lib/graph/layout.ts",
              "docker-compose.yml")
    body = stack_svc.topology(db, "core")
    assert _own(body) == {}
    rings = {n["id"]: n["ring"] for n in body["nodes"]}
    assert rings["api"] == rings["web"] == stack_svc.RING_CODE


def test_own_deployables_get_their_own_health_component(db, monkeypatch):
    from app.services import scans as scans_svc

    _clear_code_map(db)
    _describe(db, "frontend/package.json", "package.json")
    comps = {c["id"]: c for c in scans_svc.component_health(db, "core", head_sha="h1")["components"]}
    assert comps["app:frontend"]["path"] == "frontend/"
    # The workspace root was dropped, and a root deployable never gets an empty prefix.
    assert not any(c["path"] == "" or c["id"] == "app:." for c in comps.values())
    assert {"web", "api", "fleet", "cli"} <= set(comps)


def test_each_box_names_its_plane_and_the_map_says_who_writes_graphban(db, monkeypatch):
    # GRPH-1099: the client collapses Graphban into one box on a project that does not
    # write it, so the payload must say which boxes are the instance and which the app.
    monkeypatch.setattr("app.services.fleet.fleet_status", lambda *_a, **_k: _roster([]))
    _clear_code_map(db)
    _describe(db, "frontend/package.json")
    body = stack_svc.topology(db, "core")
    assert body["graphban_source"] == stack_svc.NOT_OWNED
    planes = {n["id"]: n["plane"] for n in body["nodes"]}
    assert planes["app:frontend"] == stack_svc.PLANE_APP
    for gid in ("api", "web", "db", "fleet", "gbagent", "cli"):
        assert planes[gid] == stack_svc.PLANE_GRAPHBAN, gid

    _clear_code_map(db)
    _describe(db, "backend/app/mcp_server.py")
    assert stack_svc.topology(db, "core")["graphban_source"] == stack_svc.OWNED

    _clear_code_map(db)
    # No code map at all: nobody looked. Not "owned", and not a confident "not_owned".
    assert stack_svc.topology(db, "core")["graphban_source"] == stack_svc.OWNERSHIP_UNKNOWN
