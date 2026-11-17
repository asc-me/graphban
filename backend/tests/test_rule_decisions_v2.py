"""GRPH-1680: the box pulls per-project profile applies (rule decisions contract v2)."""
from app.services import code_sync
from app.services import rule_toggles as T
from app.services import rules_org as RO
from tests.test_rule_decision_sync import _box_project, _record, _self_host
from tests.test_rule_toggles import RULE, _db
from tests.test_rules_org import org  # noqa: F401  (org is a fixture)


class Resp:
    def __init__(self, body=None):
        self.status_code, self._b = 200, body or {}
    def json(self): return self._b
    def raise_for_status(self): pass


def _v2(entries, **kw):
    rec = _record(locked=False, on=False)
    rec.update(contracts=[1, 2], project_profiles=entries, **kw)
    return rec


def _wire(monkeypatch, rec):
    gets, posts = [], []
    monkeypatch.setattr(code_sync.httpx, "get",
                        lambda url, **k: gets.append(k.get("params")) or Resp(rec))
    monkeypatch.setattr(code_sync.httpx, "post",
                        lambda url, **k: posts.append(k["json"]) or Resp())
    return gets, posts


def test_a_project_profile_changes_only_that_project(client, org, monkeypatch):  # noqa: F811
    _self_host(monkeypatch)
    pid = _box_project(client, org["owner"])
    other = client.post("/api/projects", json={"name": "Other"}, headers=org["owner"]).json()["id"]
    gets, posts = _wire(monkeypatch, _v2([
        {"project_id": pid, "profile": {"id": "standard", "version": "1"}},
        {"project_id": "gone", "profile": {"id": "standard", "version": "1"}}]))
    db = _db()
    out = code_sync.pull_rule_decisions(db, cloud_url="https://c", api_key="k")
    assert gets == [None, {"contract": 2}]
    assert T.resolve(db, RULE, pid)["on"]["value"] is True
    assert T.resolve(db, RULE, pid)["params"]["value"] == {"n": 3}
    assert T.resolve(db, RULE, other)["on"]["value"] is False   # org says off; not this project
    assert out["project_profiles_applied"] == [
        {"project_id": pid, "profile": "standard", "version": "1"}]
    assert out["not_on_box"] == ["gone"]
    assert posts[0]["project_profiles_applied"] == out["project_profiles_applied"]
    assert posts[0]["not_on_box"] == ["gone"]


def test_a_console_without_v2_pulls_exactly_as_today(client, org, monkeypatch):  # noqa: F811
    _self_host(monkeypatch)
    _box_project(client, org["owner"])
    gets, posts = _wire(monkeypatch, _record())
    out = code_sync.pull_rule_decisions(_db(), cloud_url="https://c", api_key="k")
    assert gets == [None]
    assert out == {"pulled": True, "version": "v1", "changed": True, "reported": True}
    assert posts == [{"org_id": "org_cloud", "version": "v1"}]


def test_an_org_lock_holds_against_a_project_profile(client, org, monkeypatch):  # noqa: F811
    _self_host(monkeypatch)
    pid = _box_project(client, org["owner"])
    rec = _v2([{"project_id": pid, "profile": {"id": "standard", "version": "1"}}])
    rec["toggles"][0].update(locked=True, on=False)
    _wire(monkeypatch, rec)
    db = _db()
    code_sync.pull_rule_decisions(db, cloud_url="https://c", api_key="k")
    assert T.resolve(db, RULE, pid)["on"]["value"] is False
