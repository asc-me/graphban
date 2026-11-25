"""GRPH-1896: GET /api/sync/status runs on every page load.

It answers from rows already stored. It does not call the console, and the number of SQL
statements does not grow from 3 readable projects to 30.
"""
from datetime import datetime, timezone

import httpx
from sqlalchemy import event, select

from app.models import CodeNode, CodeSyncState, PlatformConfig, SyncProject, User


def _db():
    from app.db import SessionLocal

    return SessionLocal()


def _block_outbound(monkeypatch) -> list:
    """Every httpx entry the console clients use. A call is recorded and then refused."""
    calls: list = []

    def blocked(*args, **kwargs):
        calls.append(args[0] if args else kwargs.get("url"))
        raise httpx.ConnectError("outbound HTTP is blocked")

    for name in ("get", "post", "request", "stream", "Client"):
        monkeypatch.setattr(httpx, name, blocked)
    return calls


def _stored_core(db) -> None:
    """core: one node whose hash the manifest has not confirmed, plus a mapped push result."""
    now = datetime.now(timezone.utc)
    db.add(CodeNode(
        id="cn_status_core", project_id="core", path="app/a.py", kind="file",
        content_hash="new",
    ))
    db.add(CodeSyncState(
        project_id="core", manifest={"app/a.py": "old", "gone.py": "x"}, last_synced_at=now,
    ))
    db.add(SyncProject(
        local_project_id="core", cloud_project_id="cloud-core", cloud_project_tag="CORE",
        last_push_at=now, last_push_result="ok",
    ))
    db.commit()


def _core(body: dict) -> dict:
    return next(p for p in body["projects"] if p["project_id"] == "core")


def test_status_returns_stored_state_when_outbound_http_is_blocked(client, auth, monkeypatch):
    db = _db()
    try:
        _stored_core(db)
    finally:
        db.close()
    client.post(
        "/api/sync/link",
        json={"cloud_url": "cloud.example", "api_key": "gb_sk_secret", "org": "Acme"},
        headers=auth,
    )
    calls = _block_outbound(monkeypatch)

    r = client.get("/api/sync/status", headers=auth)

    assert r.status_code == 200, r.text
    assert calls == [], f"status called the console: {calls}"
    assert "gb_sk_secret" not in r.text
    body = r.json()
    assert body["linked"] is True
    assert body["cloud_url"] == "https://cloud.example"
    assert body["org"] == "Acme"
    core = _core(body)
    # changed hash + a path the manifest still has and the graph does not = 2 pending
    assert core["total_nodes"] == 1
    assert core["synced_nodes"] == 2
    assert core["pending"] == 2
    assert core["status"] == "stale"
    assert core["mapped"] is True
    assert core["cloud_project_id"] == "cloud-core"
    assert core["cloud_project_tag"] == "CORE"
    assert core["last_push_result"] == "ok"


def _statements(client, auth) -> tuple[list[str], dict]:
    from app.db import engine

    seen: list[str] = []

    def hook(conn, cursor, statement, parameters, context, executemany):
        seen.append(statement)

    event.listen(engine, "before_cursor_execute", hook)
    try:
        r = client.get("/api/sync/status", headers=auth)
    finally:
        event.remove(engine, "before_cursor_execute", hook)
    assert r.status_code == 200, r.text
    return seen, r.json()


def test_statement_count_does_not_grow_from_3_to_30_projects(client, auth):
    """Asserted as "does not grow", not a magic number — the absolute count varies by engine.

    The seeded operator already reads three projects (core, web, infra). A second measurement
    after 27 more is what a per-project loop would inflate. Each measurement is the second
    request at that size, so a one-time session warmup is not the difference."""
    from app.services import projects as proj_svc

    db = _db()
    try:
        _stored_core(db)
        _statements(client, auth)  # warm
        small, three = _statements(client, auth)
        assert len(three["projects"]) == 3
        assert _core(three)["pending"] == 2 and _core(three)["status"] == "stale"
        assert "embedding" not in "\n".join(small).lower()

        owner = db.scalars(select(User).where(User.email == "alex@ascme-labs.com")).one()
        paused_id = None
        empty_id = None
        for i in range(27):
            project = proj_svc.create_project(
                db, name=f"Extra {i:02d}", owner_user_id=owner.id,
            )
            if i == 0:
                paused_id = project.id
                db.get(PlatformConfig, project.id).sync_graph = False
                db.add(CodeNode(
                    id="cn_status_paused", project_id=project.id, path="a.py", kind="file",
                    content_hash="h",
                ))
            elif i == 1:
                empty_id = project.id
            else:
                db.add(CodeNode(
                    id=f"cn_status_extra_{i}", project_id=project.id, path="a.py", kind="file",
                    content_hash="h",
                ))
        db.commit()

        _statements(client, auth)  # warm at the larger size
        big, thirty = _statements(client, auth)
    finally:
        db.close()

    assert len(thirty["projects"]) == 30
    assert len(big) == len(small), (
        f"statement count grew from {len(small)} at 3 projects to {len(big)} at 30"
    )
    assert "embedding" not in "\n".join(big).lower()
    assert _core(thirty)["pending"] == 2 and _core(thirty)["status"] == "stale"
    paused = next(p for p in thirty["projects"] if p["project_id"] == paused_id)
    assert paused["sync_graph"] is False and paused["status"] == "paused"
    empty = next(p for p in thirty["projects"] if p["project_id"] == empty_id)
    assert empty["status"] == "empty" and empty["total_nodes"] == 0
