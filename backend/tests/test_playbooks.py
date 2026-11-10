"""GET /api/playbooks (GRPH-1491, PRD GRPH-P65)."""
from __future__ import annotations

import hashlib
import json
import pathlib
import subprocess
import sys

from app.config import settings
from app.services import playbooks as svc

ROOT = pathlib.Path(__file__).resolve().parents[2]


def _check_body(body: dict) -> None:
    assert body["version"]
    assert {f["kind"] for f in body["files"]} == {"skill", "loop", "playbook"}
    for f in body["files"]:
        assert set(f) == {"path", "kind", "sha256", "body"}
        assert hashlib.sha256(f["body"].encode()).hexdigest() == f["sha256"]
    paths = {f["path"] for f in body["files"]}
    assert "AGENTS.md" not in paths and "CLAUDE.md" not in paths


def test_session_gets_the_pack_stamped_with_the_health_sha(client, auth):
    r = client.get("/api/playbooks", headers=auth)
    assert r.status_code == 200
    body = r.json()
    assert body["git_sha"] == settings.resolved_git_sha == client.get("/health").json()["git_sha"]
    _check_body(body)


def test_project_api_key_gets_the_pack(client, auth):
    key = client.post("/api/api-keys", json={"name": "pb", "project_id": "core"},
                      headers=auth).json()["plaintext"]
    r = client.get("/api/playbooks", headers={"X-API-Key": key})
    assert r.status_code == 200
    _check_body(r.json())


def test_unauthenticated_is_refused(client):
    assert client.get("/api/playbooks").status_code == 401


def test_a_flipped_body_fails_the_route(client, auth, tmp_path, monkeypatch):
    data = json.loads((ROOT / "backend/scripts/gban_playbooks.json").read_text())
    data["files"][0]["body"] += "tampered"
    bad = tmp_path / "pack.json"
    bad.write_text(json.dumps(data))
    monkeypatch.setattr(svc, "PACK_FILE", bad)
    assert client.get("/api/playbooks", headers=auth).status_code == 503


def test_the_pack_equals_its_sources():
    ran = subprocess.run([sys.executable, str(ROOT / "scripts/gen_playbooks.py"), "--check"],
                         capture_output=True, text=True)
    assert ran.returncode == 0, ran.stdout


def test_the_dockerfile_ships_the_pack():
    df = (ROOT / "backend/Dockerfile").read_text()
    assert "COPY scripts/gban_playbooks.json ./app/data/playbooks.json" in df
    assert "test -s /app/app/data/playbooks.json" in df
    assert df.index("playbooks.json ./app/data") < df.index("pip install --no-cache-dir .")
