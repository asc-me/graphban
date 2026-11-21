"""The box reads other deployments' specs and applies console revisions (GRPH-1677)."""
import pytest

from app.models import RemoteSpec
from app.services import code_sync, spec_pull
from app.services import prds as prd_svc

URL, KEY = "https://console.example", "k"


@pytest.fixture()
def db(client):
    from app.db import SessionLocal

    s = SessionLocal()
    try:
        yield s
    finally:
        s.close()


class _Resp:
    def __init__(self, status=200, body=None):
        self.status_code, self._body = status, body

    def json(self):
        return self._body

    def raise_for_status(self):
        if self.status_code >= 400:
            raise spec_pull.httpx.HTTPError("x")


@pytest.fixture()
def console(monkeypatch):
    state = {"record": {}, "reports": [], "config": {"features": ["specs"]}, "get": 200}

    def get(url, params=None, headers=None, timeout=None):
        if url.endswith("/api/config"):
            return _Resp(200, state["config"])
        if params["project"] != state.get("only", params["project"]):
            return _Resp(state["get"], {})
        return _Resp(state["get"], state["record"])

    def post(url, json=None, headers=None, timeout=None):
        state["reports"].append(json)
        return _Resp(200)

    monkeypatch.setattr(spec_pull.httpx, "get", get)
    monkeypatch.setattr(spec_pull.httpx, "post", post)
    monkeypatch.setattr(code_sync, "link_status", lambda _db: {"linked": True})
    return state


def _prd(client, auth, name="Pull One"):
    p = client.post("/api/projects", headers=auth, json={"name": name}).json()
    prd = client.post("/api/prds", headers=auth, json={
        "title": "Mine", "project_id": p["id"], "template": "standard"}).json()
    return p, prd


def _pull(db):
    db.expire_all()
    return spec_pull.pull_specs(db, cloud_url=URL, api_key=KEY)


def test_remote_spec_is_stored_read_only_with_home(client, auth, db, console):
    p, _ = _prd(client, auth)
    console["only"] = "Pull One"
    console["record"] = {"specs": [{"prd_id": "OTHER-P1", "title": "Theirs", "home": "box-a",
                                    "base_hash": "h1", "body_markdown": "# T"}]}
    out = _pull(db)
    assert out["remote"] == ["OTHER-P1"]
    row = db.get(RemoteSpec, "OTHER-P1")
    assert row.home == "box-a" and row.base_hash == "h1"
    assert spec_pull.list_remote(db, p["id"])[0]["home"] == "box-a"
    assert prd_svc.get_prd(db, "OTHER-P1") is None  # never a Prd, so never editable
    with pytest.raises(spec_pull.RemoteSpecRefused, match="box-a"):
        spec_pull.refuse_if_remote(db, "OTHER-P1")
    r = client.post("/api/prds/OTHER-P1/decompose", headers=auth)
    assert r.status_code == 409 and "box-a" in r.text
    assert "spec.remote_is_read_only" in r.text  # the refusal names its rule
    console["record"] = {"specs": []}
    _pull(db)
    assert db.get(RemoteSpec, "OTHER-P1") is None


def test_matching_revision_applies_and_reports(client, auth, db, console):
    _, prd = _prd(client, auth)
    console["only"] = "Pull One"
    cur = prd_svc.body_hash(prd_svc.get_prd(db, prd["id"]).body)
    console["record"] = {"revisions": [{"id": 7, "prd_id": prd["id"], "base_hash": cur,
                                        "body_markdown": "# Mine\n\n## Overview\n\nnew\n",
                                        "author": "Dana"}]}
    out = _pull(db)
    assert out["applied"] == [prd["id"]]
    got = prd_svc.get_prd(db, prd["id"])
    assert "new" in got.body and got.version != "v0.1"
    rep = console["reports"][-1]["results"][0]
    assert rep["status"] == "applied" and rep["id"] == 7
    # re-served after a lost report: reads applied, not conflict
    assert _pull(db)["applied"] == [prd["id"]]


def test_stale_revision_is_conflict_and_not_applied(client, auth, db, console):
    _, prd = _prd(client, auth)
    console["only"] = "Pull One"
    before = prd_svc.get_prd(db, prd["id"]).body
    console["record"] = {"revisions": [{"id": 8, "prd_id": prd["id"], "base_hash": "stale",
                                        "body_markdown": "# overwritten", "author": "Dana"}]}
    out = _pull(db)
    assert out["conflicts"] == [prd["id"]] and out["applied"] == []
    assert prd_svc.get_prd(db, prd["id"]).body == before
    rep = console["reports"][-1]["results"][0]
    assert rep["status"] == "conflict" and rep["base_hash"] == prd_svc.body_hash(before)


def test_console_without_specs_is_untouched(db, console):
    console["config"] = {"features": []}
    assert _pull(db) is None
    console["config"] = {"features": ["specs"]}
    console["get"] = 404
    assert _pull(db) is None
