"""GRPH-1098 — one current app-topology record per project.

A producer that holds the repo posts the map. The server folds it into
GET /api/platform/stack. A refused post names every bad path and writes nothing,
so the previous record cannot be blanked by a bad payload. Staleness is the
sha against the same head scans use; a missing head is never current.
"""
from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

from app.config import settings

SHA = "a" * 40
SHA2 = "b" * 40
_BACKEND = Path(__file__).resolve().parents[1]
_PROV = {"file": "compose.yml", "rule": "services.api", "line": 12}


def _key(client, auth, name="app-topology"):
    return client.post("/api/api-keys", json={"name": name}, headers=auth).json()["plaintext"]


def _head(monkeypatch, sha, railway=""):
    monkeypatch.setattr(settings, "git_sha", sha)
    monkeypatch.setattr(settings, "railway_git_commit_sha", railway)


def _body(**over):
    body = {
        "project_id": "core",
        "producer_id": "gban",
        "producer_version": "0.4.0",
        "mapped_sha": SHA,
        "components": [{
            "id": "api",
            "dir": "backend",
            "label": "API",
            "env": "DATABASE_URL",
            "provenance": dict(_PROV),
        }],
        "infrastructure": [{
            "id": "db",
            "label": "Postgres",
            "kind": "database",
            "provenance": {"file": "compose.yml", "rule": "services.db", "line": 30},
        }],
        "external": [{
            "id": "stripe",
            "label": "Stripe",
            "provenance": {"file": "backend/app/config.py", "rule": "stripe_client", "line": 4},
        }],
        "edges": [
            {
                "from": "api", "to": "db", "protocol": "psycopg", "label": "SQL",
                "provenance": {"file": "compose.yml", "rule": "services.api.depends_on", "line": 18},
            },
            {
                "from": "api", "to": "stripe", "protocol": "HTTPS", "label": "charges",
                "provenance": {"file": "backend/app/config.py", "rule": "stripe_client", "line": 9},
            },
        ],
    }
    body.update(over)
    return body


def _post(client, key, body):
    return client.post(
        "/api/platform/stack/app", json=body, headers={"X-API-Key": key},
    )


def _get(client, auth):
    r = client.get("/api/platform/stack?project_id=core", headers=auth)
    assert r.status_code == 200, r.text
    return r.json()


@pytest.fixture()
def db(client):
    from app.db import SessionLocal

    s = SessionLocal()
    try:
        yield s
    finally:
        s.close()


def test_a_valid_post_replaces_the_record_and_shows_in_get(client, auth, monkeypatch):
    _head(monkeypatch, SHA)
    key = _key(client, auth)
    r = _post(client, key, _body())
    assert r.status_code == 200, r.text
    posted = r.json()
    assert posted["mapped_sha"] == SHA
    assert posted["previous_mapped_sha"] == ""
    assert posted["posted_by"].startswith("key:")
    assert posted["producer_id"] == "gban"
    assert posted["components"][0]["provenance"]["file"] == "compose.yml"
    assert posted["components"][0]["env"] == "DATABASE_URL"
    assert "postgres://" not in r.text

    body = _get(client, auth)
    assert body["mapping"]["state"] == "mapped"
    assert body["mapping"]["mapped_sha"] == SHA
    assert body["mapping"]["head_sha"] == SHA
    assert body["mapping"]["previous_mapped_sha"] == ""
    assert body["mapping"]["producer_id"] == "gban"
    assert body["mapping"]["producer_version"] == "0.4.0"
    assert body["mapping"]["posted_by"].startswith("key:")
    assert body["app"]["mapped_sha"] == SHA
    assert body["app"]["external"][0]["id"] == "stripe"

    node = next(n for n in body["nodes"] if n["id"] == "app:backend")
    assert node["kind"] == "deployable"
    assert node["ring"] == 1
    assert node["host"] == "project_app"
    assert node["liveness"] == "unknown"
    assert node["provenance"] == _PROV
    facts = {f["key"]: f["value"] for f in node["facts"]}
    assert facts["producer"] == "gban"
    assert facts["mapped_sha"] == SHA
    assert facts["env"] == "DATABASE_URL"
    assert facts["dir"] == "backend"

    edge = next(e for e in body["edges"] if e["from"] == "app:backend" and e["to"] == "infra:db")
    assert edge["protocol"] == "psycopg"
    assert edge["provenance"]["rule"] == "services.api.depends_on"
    infra = next(n for n in body["nodes"] if n["id"] == "infra:db")
    assert infra["kind"] == "infra"
    assert infra["ring"] == 2
    assert infra["host"] == "app_infra"
    ext = next(n for n in body["nodes"] if n["id"] == "ext:stripe")
    assert ext["kind"] == "external"
    assert ext["ring"] == 3


def test_a_second_post_replaces_and_keeps_the_previous_sha(client, auth, monkeypatch):
    _head(monkeypatch, SHA)
    key = _key(client, auth)
    assert _post(client, key, _body()).status_code == 200
    second = _body(
        mapped_sha=SHA2,
        components=[{
            "id": "web", "dir": "web", "label": "Web",
            "provenance": {"file": "compose.yml", "rule": "services.web"},
        }],
        infrastructure=[], external=[], edges=[],
    )
    r = _post(client, key, second)
    assert r.status_code == 200, r.text
    assert r.json()["previous_mapped_sha"] == SHA
    assert r.json()["mapped_sha"] == SHA2

    body = _get(client, auth)
    ids = {n["id"] for n in body["nodes"]}
    assert "app:web" in ids
    assert "app:backend" not in ids
    assert "infra:db" not in ids
    assert body["mapping"]["state"] == "stale"
    assert body["mapping"]["mapped_sha"] == SHA2
    assert body["mapping"]["head_sha"] == SHA
    assert body["mapping"]["previous_mapped_sha"] == SHA
    assert body["app"]["components"][0]["dir"] == "web"


def test_missing_provenance_is_422_and_the_previous_record_stays(client, auth, monkeypatch):
    _head(monkeypatch, SHA)
    key = _key(client, auth)
    assert _post(client, key, _body()).status_code == 200

    bad = _body(mapped_sha=SHA2)
    bad["components"] = [
        bad["components"][0],
        {"id": "worker", "dir": "worker", "label": "worker"},
    ]
    bad["edges"] = list(bad["edges"]) + [
        {"from": "api", "to": "db", "protocol": "psycopg", "label": "again",
         "provenance": {"file": "compose.yml", "rule": "services.api.depends_on"}},
        {"from": "api", "to": "stripe", "protocol": "HTTPS", "label": ""},
    ]
    r = _post(client, key, bad)
    assert r.status_code == 422, r.text
    detail = r.json()["detail"]
    assert isinstance(detail, list)
    assert {"path": "components[1].provenance", "reason": "missing"} in detail
    assert {"path": "edges[3].provenance", "reason": "missing"} in detail

    body = _get(client, auth)
    assert body["mapping"]["mapped_sha"] == SHA
    assert body["mapping"]["state"] == "mapped"
    assert body["app"]["components"][0]["dir"] == "backend"
    assert any(n["id"] == "app:backend" for n in body["nodes"])
    assert not any(n["id"] == "app:worker" for n in body["nodes"])


def test_an_edge_keeps_every_provenance_and_a_documented_state(client, auth, monkeypatch):
    _head(monkeypatch, SHA)
    key = _key(client, auth)
    body = _body()
    body["edges"][0]["state"] = "documented"
    body["edges"][0]["provenances"] = [
        {"file": "compose.yml", "rule": "services.api.depends_on", "line": 18},
        {"file": ".env.example", "rule": "env.template", "line": 4},
    ]
    r = _post(client, key, body)
    assert r.status_code == 200, r.text
    stored = r.json()["edges"][0]
    assert stored["state"] == "documented"
    assert [item["file"] for item in stored["provenances"]] == ["compose.yml", ".env.example"]

    got = _get(client, auth)
    edge = next(e for e in got["edges"] if e["to"] == "infra:db")
    assert edge["state"] == "documented"
    assert [item["file"] for item in edge["provenances"]] == ["compose.yml", ".env.example"]

    refused = _body(mapped_sha=SHA2)
    refused["edges"][0]["state"] = "guessed"
    bad = _post(client, key, refused)
    assert bad.status_code == 422, bad.text
    assert {"path": "edges[0].state", "reason": "must be set or documented"} in bad.json()["detail"]
    assert _get(client, auth)["mapping"]["mapped_sha"] == SHA


def test_a_dangling_edge_writes_nothing(client, auth, monkeypatch):
    _head(monkeypatch, SHA)
    key = _key(client, auth)
    bad = _body()
    bad["edges"] = list(bad["edges"]) + [{
        "from": "api", "to": "redis", "protocol": "RESP", "label": "cache",
        "provenance": {"file": "compose.yml", "rule": "services.redis"},
    }]
    r = _post(client, key, bad)
    assert r.status_code == 422, r.text
    reasons = r.json()["detail"]
    assert any(
        e["path"] == "edges[2].to" and "unresolved" in e["reason"] for e in reasons
    )
    body = _get(client, auth)
    assert body["mapping"]["state"] == "unmapped"
    assert body["mapping"]["mapped_sha"] is None
    assert body["app"] is None


def test_an_explicit_unresolved_node_is_allowed(client, auth, monkeypatch):
    _head(monkeypatch, SHA)
    key = _key(client, auth)
    body = _body()
    body["unresolved"] = [{
        "id": "redis", "label": "Redis",
        "provenance": {"file": "compose.yml", "rule": "services.redis", "line": 40},
    }]
    body["edges"] = list(body["edges"]) + [{
        "from": "api", "to": "redis", "protocol": "RESP", "label": "cache",
        "provenance": {"file": "compose.yml", "rule": "services.redis"},
    }]
    r = _post(client, key, body)
    assert r.status_code == 200, r.text
    got = _get(client, auth)
    node = next(n for n in got["nodes"] if n["id"] == "unresolved:redis")
    assert node["kind"] == "unresolved"
    assert node["provenance"]["file"] == "compose.yml"
    assert any(
        e["from"] == "app:backend" and e["to"] == "unresolved:redis" for e in got["edges"]
    )


def test_an_env_value_is_refused_and_a_variable_name_is_kept(client, auth, monkeypatch):
    _head(monkeypatch, SHA)
    key = _key(client, auth)
    assert _post(client, key, _body()).status_code == 200

    bad = _body(mapped_sha=SHA2)
    bad["components"][0]["label"] = "postgres://app:s3cret@db/app"
    r = _post(client, key, bad)
    assert r.status_code == 422, r.text
    assert "s3cret" not in r.text
    assert any(e["reason"] == "env values are never stored, only variable names" for e in r.json()["detail"])

    got = _get(client, auth)
    assert got["mapping"]["mapped_sha"] == SHA
    assert "s3cret" not in str(got)
    assert got["app"]["components"][0]["env"] == "DATABASE_URL"
    assert got["app"]["components"][0]["label"] == "API"


def test_unknown_is_not_a_commit_sha(client, auth):
    key = _key(client, auth)
    r = _post(client, key, _body(mapped_sha="unknown"))
    assert r.status_code == 422, r.text
    assert {"path": "mapped_sha", "reason": "unknown is not a commit"} in r.json()["detail"]
    assert _get(client, auth)["mapping"]["state"] == "unmapped"


def test_an_empty_map_is_refused(client, auth):
    key = _key(client, auth)
    r = _post(client, key, _body(components=[], infrastructure=[], external=[], edges=[]))
    assert r.status_code == 422, r.text
    assert {"path": "nodes", "reason": "missing"} in r.json()["detail"]


def test_a_duplicate_component_dir_is_refused(client, auth):
    key = _key(client, auth)
    body = _body()
    body["components"] = list(body["components"]) + [{
        "id": "api2", "dir": "backend/",
        "provenance": {"file": "compose.yml", "rule": "services.api"},
    }]
    r = _post(client, key, body)
    assert r.status_code == 422, r.text
    assert any(e["path"] == "components[1].dir" and "duplicate" in e["reason"] for e in r.json()["detail"])


def test_no_key_is_401(client):
    r = client.post("/api/platform/stack/app", json=_body())
    assert r.status_code == 401, r.text


def test_a_non_member_key_cannot_write_403(client, auth):
    reg = client.post("/api/auth/register", json={
        "name": "Sam Rivers", "email": "sam-topology@example.com", "handle": "samtopo",
        "password": "pw123456",
    })
    assert reg.status_code == 201, reg.text
    other = {"Authorization": f"Bearer {reg.json()['access_token']}"}
    other_key = client.post(
        "/api/api-keys", json={"name": "outsider"}, headers=other).json()["plaintext"]
    r = _post(client, other_key, _body())
    assert r.status_code == 403, r.text


def test_no_record_is_unmapped_even_when_the_head_is_known(client, auth, monkeypatch):
    _head(monkeypatch, SHA)
    body = _get(client, auth)
    assert body["mapping"]["state"] == "unmapped"
    assert body["mapping"]["mapped_sha"] is None
    assert body["mapping"]["head_sha"] == SHA
    assert body["app"] is None


def test_a_record_with_no_head_is_freshness_unknown(client, auth, monkeypatch):
    _head(monkeypatch, SHA)
    key = _key(client, auth)
    assert _post(client, key, _body()).status_code == 200
    _head(monkeypatch, "unknown")
    body = _get(client, auth)
    assert body["identity"]["api_git_sha"] == "unknown"
    assert body["mapping"]["state"] == "freshness_unknown"
    assert body["mapping"]["head_sha"] is None
    assert body["mapping"]["mapped_sha"] == SHA
    assert body["mapping"]["state"] != "mapped"


def test_a_different_head_is_stale_with_both_shas(client, auth, monkeypatch):
    _head(monkeypatch, SHA)
    key = _key(client, auth)
    assert _post(client, key, _body()).status_code == 200
    _head(monkeypatch, "c" * 40)
    body = _get(client, auth)
    assert body["mapping"]["state"] == "stale"
    assert body["mapping"]["mapped_sha"] == SHA
    assert body["mapping"]["head_sha"] == "c" * 40


def test_a_record_replaces_code_map_deployables(client, auth, db, monkeypatch):
    from app.models import CodeNode
    from app.services import code_graph

    _head(monkeypatch, SHA)
    db.query(CodeNode).filter(CodeNode.project_id == "core").delete()
    db.commit()
    code_graph.upsert_node(db, project_id="core", path="frontend/package.json", kind="file")
    db.commit()

    before = _get(client, auth)
    assert any(n["id"] == "app:frontend" for n in before["nodes"])
    assert before["mapping"]["state"] == "unmapped"
    assert any(
        h["id"] == "project_app" and "from the code map" in h["label"] for h in before["hosts"]
    )

    key = _key(client, auth)
    assert _post(client, key, _body()).status_code == 200
    after = _get(client, auth)
    ids = {n["id"] for n in after["nodes"]}
    assert "app:frontend" not in ids
    assert "app:backend" in ids
    assert any(
        h["id"] == "project_app" and "app topology record" in h["label"] for h in after["hosts"]
    )
    assert not any(e["id"] == "own_code" for e in after["empties"])


def test_an_unreadable_record_is_freshness_unknown_not_unmapped(db, monkeypatch):
    from app.models import AppTopology
    from app.services import stack as stack_svc

    real_get = db.get

    def get(model, ident, **kwargs):
        if model is AppTopology:
            raise RuntimeError("app topology unreadable")
        return real_get(model, ident, **kwargs)

    monkeypatch.setattr(db, "get", get)
    monkeypatch.setattr(
        "app.services.fleet.fleet_status",
        lambda *_a, **_k: {"agents": [], "online": 0, "total": 0, "posture": "single-agent"},
    )
    _head(monkeypatch, SHA)
    body = stack_svc.topology(db, "core")
    assert body["mapping"]["state"] == "freshness_unknown"
    assert body["app"] is None
    assert body["mapping"]["head_sha"] == SHA
    assert body["mapping"]["mapped_sha"] is None


def _migration():
    path = _BACKEND / "alembic" / "versions" / "0140_app_topology.py"
    spec = importlib.util.spec_from_file_location("migration_0140", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_alembic_stays_single_head_with_0140_in_the_chain():
    from alembic.config import Config
    from alembic.script import ScriptDirectory

    cfg = Config(str(_BACKEND / "alembic.ini"))
    cfg.set_main_option("script_location", str(_BACKEND / "alembic"))
    cfg.set_main_option("path_separator", "os")
    script = ScriptDirectory.from_config(cfg)
    # 0143 (GRPH-1127, scan finding grain) on 0142 (GRPH-1125, scan result); linear through 0140.
    assert script.get_heads() == ["0143"]
    assert script.get_revision("0143").down_revision == "0142"
    assert script.get_revision("0142").down_revision == "0141"
    assert script.get_revision("0141").down_revision == "0140"
    rev = script.get_revision("0140")
    assert rev.down_revision == "0139"


def test_migration_0140_upgrades_and_downgrades_cleanly():
    from alembic.operations import Operations
    from alembic.runtime.migration import MigrationContext
    from sqlalchemy import create_engine, inspect, text

    engine = create_engine("sqlite://")
    with engine.begin() as conn:
        conn.execute(text("CREATE TABLE projects (id VARCHAR NOT NULL PRIMARY KEY)"))
    mod = _migration()

    def run(fn):
        with engine.begin() as conn:
            ctx = MigrationContext.configure(conn)
            with Operations.context(ctx):
                fn()

    run(mod.upgrade)
    assert "app_topologies" in inspect(engine).get_table_names()
    run(mod.downgrade)
    assert "app_topologies" not in inspect(engine).get_table_names()
    run(mod.upgrade)
    assert "app_topologies" in inspect(engine).get_table_names()


def test_record_boxes_are_the_app_plane_even_on_the_external_host(client, auth, monkeypatch):
    # GRPH-1099: `ext:stripe` shares the external host with Graphban's providers; the
    # collapsed map must still keep it outside the "Built with Graphban" box.
    _head(monkeypatch, SHA)
    key = _key(client, auth)
    assert _post(client, key, _body()).status_code == 200
    planes = {n["id"]: n["plane"] for n in _get(client, auth)["nodes"]}
    assert planes["app:backend"] == planes["infra:db"] == planes["ext:stripe"] == "app"
    assert planes["api"] == planes["db"] == "graphban"
