"""GRPH-1518 (P67 §6 L3/L5): project mapping and per-project push results.

The console is faked at `httpx.get` / `httpx.post`. Every failure shape is one of three
states: done, refused (the console's reason) or unknown (no answer) — never two.
"""
import httpx
import pytest

from app.db import SessionLocal
from app.models import Project, SyncLink, SyncProject
from app.security import secrets
from app.services import code_graph

CONSOLE = "https://console.example"
TOKEN = "gb_dep_tokensecretTOKENSECRET"


class Fake:
    """`rules` maps a project header (`X-Cloud-Project`) to a (status, json) or exception;
    `projects` is the answer to GET /api/sync/projects."""

    def __init__(self):
        self.rules: dict = {}
        self.default = (200, {})
        self.projects = (200, {"projects": [{"id": "c1", "name": "One"}]})
        self.posts: list[tuple[str, dict]] = []

    def _answer(self, answer, url):
        if isinstance(answer, Exception):
            raise answer
        code, body = answer
        return httpx.Response(code, json=body, request=httpx.Request("GET", url))

    def get(self, url, headers=None, timeout=None, **kw):
        return self._answer(self.projects, url)

    def post(self, url, json=None, headers=None, timeout=None, **kw):
        cloud = (headers or {}).get("X-Cloud-Project", "")
        self.posts.append((cloud, json))
        return self._answer(self.rules.get(cloud, self.default), url)


@pytest.fixture()
def fake(monkeypatch):
    f = Fake()
    monkeypatch.setattr(httpx, "get", f.get)
    monkeypatch.setattr(httpx, "post", f.post)
    return f


@pytest.fixture()
def linked(client, auth):
    """A signed-in deployment (token) with two local projects mapped."""
    with SessionLocal() as db:
        db.add(Project(id="side", name="Side", tag="SIDE"))
        link = db.get(SyncLink, "instance") or SyncLink(id="instance")
        link.console_url = link.cloud_url = CONSOLE
        link.token_enc = secrets.encrypt(TOKEN)
        db.add(link)
        db.commit()
        for pid in ("core", "side"):
            code_graph.upsert_node(db, project_id=pid, path="a.py", summary="a", content_hash="h")
        db.commit()
    r = client.put("/api/sync/projects", headers=auth, json={"mappings": [
        {"local_project_id": "core", "cloud_project_id": "c1"},
        {"local_project_id": "side", "cloud_project_id": "c2"}]})
    assert r.status_code == 200, r.text


def _push(client, auth):
    r = client.post("/api/sync/push", headers=auth, json={})
    assert r.status_code == 200, r.text
    return {p["project_id"]: p for p in r.json()["projects"]}


def _stored():
    with SessionLocal() as db:
        return {m.local_project_id: (m.last_push_result, m.last_push_reason)
                for m in db.query(SyncProject)}


def test_projects_ok_and_unknown_never_an_empty_list(client, auth, fake, linked):
    r = client.get("/api/sync/projects", headers=auth)
    assert r.json()["status"] == "ok" and r.json()["projects"] == [{"id": "c1", "name": "One"}]
    assert len(r.json()["mapping"]) == 2
    for down in (httpx.ConnectTimeout("t"), (503, {})):
        fake.projects = down
        r = client.get("/api/sync/projects", headers=auth).json()
        assert r["status"] == "unknown" and r["reason"] and "projects" not in r
    fake.projects = (401, {"reason": "token_revoked"})
    r = client.get("/api/sync/projects", headers=auth).json()
    assert r["status"] == "refused" and r["reason"] == "token_revoked"


def test_put_replaces_the_mapping(client, auth, fake, linked):
    r = client.put("/api/sync/projects", headers=auth, json={"mappings": [
        {"local_project_id": "core", "cloud_project_id": "c9"}]})
    assert [m["local_project_id"] for m in r.json()["mapping"]] == ["core"]
    assert client.put("/api/sync/projects", headers=auth, json={"mappings": [
        {"local_project_id": "nope", "cloud_project_id": "c"}]}).status_code == 404
    assert client.put("/api/sync/projects", headers=auth, json={"mappings": [
        {"local_project_id": "core", "cloud_project_id": ""}]}).status_code == 422


def test_one_refusal_does_not_stop_the_others(client, auth, fake, linked):
    fake.rules["c2"] = (403, {"reason": "no_project_access"})
    out = _push(client, auth)
    assert out["core"]["status"] == "done" and "nodes" in out["core"] and "edges" in out["core"]
    assert out["side"] == {**out["side"], "status": "refused", "reason": "no_project_access"}
    assert _stored()["core"] == ("ok", None)
    assert _stored()["side"] == ("refused", "no_project_access")


@pytest.mark.parametrize("hit", ["c1", "c2"])
@pytest.mark.parametrize("reason", ["seat_removed", "token_revoked"])
def test_a_credential_refusal_refuses_every_project(client, auth, fake, linked, reason, hit):
    # Only ONE project's push meets the 401; the other would answer 200. Whichever order they
    # are pushed in, the credential's refusal must refuse the link and every mapped project.
    fake.rules[hit] = (401, {"reason": reason})
    out = _push(client, auth)
    assert set(out) == {"core", "side"}
    assert {(p["status"], p["reason"]) for p in out.values()} == {("refused", reason)}
    assert _stored() == {"core": ("refused", reason), "side": ("refused", reason)}
    assert client.get("/api/sync/status", headers=auth).json()["state"] == "refused"


def test_a_project_401_with_another_reason_on_a_token_is_that_projects_only(
        client, auth, fake, linked):
    fake.rules["c2"] = (401, {"reason": "no_project_access"})
    out = _push(client, auth)
    assert out["core"]["status"] == "done"
    assert (out["side"]["status"], out["side"]["reason"]) == ("refused", "no_project_access")
    assert client.get("/api/sync/status", headers=auth).json()["state"] == "ok"


LEGACY_KEY = "gb_sync_legacyKEYSECRETvalue"


@pytest.fixture()
def legacy(client, auth, linked):
    """The same two mappings on a migrated key link: `legacy_key` set, no token."""
    with SessionLocal() as db:
        link = db.get(SyncLink, "instance")
        link.token_enc = ""
        link.legacy_key = secrets.encrypt(LEGACY_KEY)
        db.commit()


def test_a_legacy_link_pushes_with_its_key(client, auth, fake, legacy, monkeypatch):
    seen = []
    real = fake.post

    def spy(url, json=None, headers=None, timeout=None, **kw):
        seen.append(dict(headers or {}))
        return real(url, json=json, headers=headers, timeout=timeout)

    monkeypatch.setattr(httpx, "post", spy)
    out = _push(client, auth)
    assert {p["status"] for p in out.values()} == {"done"}
    assert seen and all(h.get("X-API-Key") == LEGACY_KEY for h in seen)
    assert not any("Authorization" in h for h in seen)


@pytest.mark.parametrize("reason", ["invalid_key", None])
def test_any_401_on_a_legacy_key_refuses_the_link(client, auth, fake, legacy, reason):
    fake.rules["c1"] = (401, {"reason": reason} if reason else {})
    out = _push(client, auth)
    assert {(p["status"], p["reason"]) for p in out.values()} == {("refused", "key_refused")}
    assert {p["console_reason"] for p in out.values()} == {reason or "http_401"}
    assert set(_stored().values()) == {("refused", "key_refused")}
    assert client.get("/api/sync/status", headers=auth).json()["state"] == "refused"


def test_a_legacy_key_403_is_still_one_projects_no(client, auth, fake, legacy):
    fake.rules["c2"] = (403, {"reason": "no_project_access"})
    out = _push(client, auth)
    assert out["core"]["status"] == "done"
    assert (out["side"]["status"], out["side"]["reason"]) == ("refused", "no_project_access")


@pytest.mark.parametrize("answer", [httpx.ReadTimeout("slow"), (502, {})])
def test_timeout_or_5xx_is_unknown_never_refused(client, auth, fake, linked, answer):
    fake.default = answer
    out = _push(client, auth)
    assert {p["status"] for p in out.values()} == {"unknown"}
    assert all(p["reason"] for p in out.values())
    assert {r for r, _ in _stored().values()} == {"unknown"}


@pytest.mark.parametrize("hit", ["c1", "c2"])
@pytest.mark.parametrize("code,body,state,reason", [
    (429, {"reason": "slow_down"}, "unknown", "429"),
    (408, {}, "unknown", "408"),
    (425, {}, "unknown", "425"),
    (400, {"reason": "bad_batch"}, "refused", "bad_batch"),
    (413, {}, "refused", "http_413"),
    (409, {"detail": "conflict"}, "refused", "conflict"),
])
def test_every_other_4xx_is_one_projects_result_never_a_500(
        client, auth, fake, linked, hit, code, body, state, reason):
    # Any non-2xx is classified (GRPH-1518 bounce): 408/425/429 are "not now" (unknown),
    # every other 4xx is the console's no for that project. Never an HTTPStatusError / 500,
    # and the other project (answering 200) is still pushed, whichever order they run in.
    fake.rules[hit] = (code, body)
    out = _push(client, auth)
    hit_local, other = ("core", "side") if hit == "c1" else ("side", "core")
    assert out[other]["status"] == "done"
    assert out[hit_local]["status"] == state and reason in out[hit_local]["reason"]
    stored = _stored()
    assert stored[other] == ("ok", None)
    assert stored[hit_local][0] == state and reason in stored[hit_local][1]
    assert client.get("/api/sync/status", headers=auth).json()["state"] != "refused"


def test_a_token_replacement_keeps_the_mappings(client, auth, fake, linked):
    with SessionLocal() as db:
        db.get(SyncLink, "instance").token_enc = secrets.encrypt("gb_dep_other")
        db.commit()
    assert {(m.local_project_id, m.cloud_project_id) for m in _rows()} == {
        ("core", "c1"), ("side", "c2")}
    assert set(_push(client, auth)) == {"core", "side"}


def _rows():
    with SessionLocal() as db:
        return db.query(SyncProject).all()


def test_fetch_org_overview_separates_refused_from_unknown(monkeypatch):
    from app.services import code_sync

    monkeypatch.setattr(httpx, "get", lambda *a, **k: httpx.Response(
        403, json={"reason": "no_access"}, request=httpx.Request("GET", "http://x")))
    with pytest.raises(code_sync.ConsoleRefused) as e:
        code_sync.fetch_org_overview(CONSOLE, "k")
    assert e.value.reason == "no_access"
    monkeypatch.setattr(httpx, "get", lambda *a, **k: httpx.Response(
        500, request=httpx.Request("GET", "http://x")))
    with pytest.raises(code_sync.ConsoleUnknown):
        code_sync.fetch_org_overview(CONSOLE, "k")


def test_sabotage_guard_projects_error_is_not_an_empty_ok(client, auth, fake, linked):
    fake.projects = (200, {"projects": []})
    assert client.get("/api/sync/projects", headers=auth).json() == {
        "status": "ok", "projects": [], "mapping": client.get(
            "/api/sync/projects", headers=auth).json()["mapping"]}
    fake.projects = httpx.ConnectError("down")
    assert client.get("/api/sync/projects", headers=auth).json()["status"] == "unknown"


def test_unchanged_push_still_contacts_the_console_and_sees_a_refusal(client, auth, fake, linked):
    """GRPH-1553: with nothing changed, a removed grant must show refused, not ok."""
    assert {p["status"] for p in _push(client, auth).values()} == {"done"}
    fake.posts.clear()
    fake.rules["c2"] = (403, {"reason": "no_project_access"})
    out = _push(client, auth)
    assert {c for c, _ in fake.posts} == {"c1", "c2"}
    assert out["core"]["status"] == "done"
    assert (out["side"]["status"], out["side"]["reason"]) == ("refused", "no_project_access")
