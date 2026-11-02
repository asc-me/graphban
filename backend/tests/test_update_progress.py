"""GET /api/platform/update-progress (GRPH-1298)."""
from __future__ import annotations

import os

from app.services import instance_update as svc

OLD = """
=== apply 2026.10.1 2026-10-01T10:00:00+0000 1000
==> pinning the deploy worktree to 2026.10.1
@@ 1000
!! old failure
"""
CUR = """
=== apply 2026.11.1 2026-11-01T10:00:00+0000 2000
==> pinning the deploy worktree to 2026.11.1
@@ 2001
==> shipping abc1234 (msg)
@@ 2005
==> building on (local)
@@ 2010
#12 [api 3/6] RUN pip install
"""


def _w(tmp_path, text):
    p = tmp_path / "deploy.log"
    p.write_text(text, encoding="utf-8")
    return str(p)


def test_only_current_attempt(tmp_path):
    got = svc.progress(paths=[_w(tmp_path, OLD + CUR)])
    assert got["state"] == "running" and got["tag"] == "2026.11.1"
    assert [(s["key"], s["status"]) for s in got["steps"]] == [
        ("pin", "done"), ("ship", "done"), ("build", "active"),
        ("verify", "pending"), ("live", "pending")]
    assert got["steps"][2]["started_at"] == 2010
    assert got["current_line"] == "#12 [api 3/6] RUN pip install"
    assert got["error"] == ""


def test_failed_has_error(tmp_path):
    got = svc.progress(paths=[_w(tmp_path, CUR + "!! api serves x, expected y\n")])
    assert got["state"] == "failed"
    assert got["error"] == "!! api serves x, expected y"
    assert [s["status"] for s in got["steps"]][2] == "failed"


def test_succeeded(tmp_path):
    got = svc.progress(paths=[_w(tmp_path, CUR + "==> verifying release identity (api :1)\n@@ 2020\n==> abc1234 is live\n@@ 2030\n")])
    assert got["state"] == "succeeded"
    assert all(s["status"] == "done" for s in got["steps"])


def test_idle_cases(tmp_path):
    assert svc.progress(paths=[str(tmp_path / "nope")])["state"] == "idle"
    nomark = svc.progress(paths=[_w(tmp_path, "==> shipping x\n")])
    assert nomark["state"] == "idle" and nomark["reason"]
    bad = tmp_path / "bad.log"
    bad.write_text(CUR)
    os.chmod(bad, 0)
    try:
        got = svc.progress(paths=[str(bad)])
        if os.geteuid() != 0:
            assert got["state"] == "idle" and "unreadable" in got["reason"]
    finally:
        os.chmod(bad, 0o600)


def test_masking(tmp_path):
    got = svc.progress(paths=[_w(tmp_path, CUR + "SOMETHING_TOKEN=abc123 password=hunter2\n")])
    assert "abc123" not in got["current_line"] and "hunter2" not in got["current_line"]
    assert "SOMETHING_TOKEN=***" in got["current_line"]


def test_rest_endpoint(client, auth, monkeypatch, tmp_path):
    monkeypatch.setattr(svc, "COMPOSE_LOG", _w(tmp_path, CUR))
    monkeypatch.setattr(svc, "NATIVE_ROOT", str(tmp_path / "none"))
    r = client.get("/api/platform/update-progress", headers=auth)
    assert r.status_code == 200 and r.json()["state"] == "running"
    assert client.get("/api/platform/update-progress").status_code in (401, 403)


def test_native_start_logs_output_with_marker(tmp_path):
    import io
    import tarfile
    root = tmp_path / "root"
    (root / "current" / "scripts").mkdir(parents=True)
    (root / "current" / "scripts" / "graphban_host.py").write_text("#\n")
    packed = tmp_path / "graphban-2026.10.1"
    (packed).mkdir()
    (packed / "GIT_SHA").write_text("deadbee\n")
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tf:
        tf.add(packed, arcname="graphban-2026.10.1")
    seen = {}

    def popen(cmd, **kw):
        seen.update(kw)

    got = svc.start_native("2026.10.1", "https://x/a.tgz", root=str(root),
                           download=lambda u: buf.getvalue(), popen=popen)
    assert got["ok"] is True
    assert seen["stdout"] is not None and seen["stdout"] != __import__("subprocess").DEVNULL
    text = (root / svc.NATIVE_LOG).read_text()
    assert "=== apply 2026.10.1 " in text
    assert svc.parse_progress(text)["tag"] == "2026.10.1"


def test_deploy_build_output_not_discarded():
    import pathlib
    sh = (pathlib.Path(__file__).resolve().parents[2] / "scripts" / "deploy.sh").read_text()
    line = next(ln for ln in sh.splitlines() if "docker compose up -d --build" in ln)
    assert "/dev/null" not in line
