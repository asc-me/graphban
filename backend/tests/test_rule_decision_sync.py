"""GRPH-1297: linked boxes pull the org's rule decisions and report when they applied them."""
import pytest

from app.errors import Refused
from app.models import ApiKey
from app.services import code_sync
from app.services import rule_toggles as T
from app.services import rules_org as RO
from tests.test_rule_toggles import RULE, _db, _login
from tests.test_rules_org import OTHER, _apply, org  # noqa: F401  (org is a fixture)


def _record(version="v1", locked=True, on=True):
    return {"org_id": "org_cloud", "org_name": "Acme", "version": version,
            "toggles": [{"rule_id": RULE, "on": on, "params": {"n": 3}, "locked": locked}],
            "profile": {"id": "standard", "version": "1"}}


def _self_host(monkeypatch):
    from app.config import settings
    monkeypatch.setattr(settings, "hosted_mode", False)   # a linked box is a self-host


def _box_project(client, owner):
    pid = client.post("/api/projects", json={"name": "BoxP"}, headers=owner).json()["id"]
    db = _db()
    from app.models import Project
    from app.models import Membership
    db.get(Project, pid).org_id = None   # a box project belongs to no local org
    uid = _user(db).id
    if not db.query(Membership).filter_by(user_id=uid, project_id=pid).first():
        db.add(Membership(user_id=uid, project_id=pid, role="owner", access="write"))
    db.commit()
    db.close()
    return pid


def _key(db, org):
    from app.models import Project
    pid = org["ps"][0]
    k = ApiKey(id="k_sync1", user_id=_user(db).id, project_id=pid, name="box-1",
               prefix="gb_sk_x", hashed_key="h", scopes=["sync"])
    db.add(k)
    db.commit()
    return k


def _user(db, owner_email="alex@ascme-labs.com"):
    from sqlalchemy import select
    from app.models import User
    return db.scalar(select(User).where(User.email == owner_email))


def test_publish_is_versioned_and_moves_with_a_decision(client, org):  # noqa: F811
    db = _db()
    before = RO.published_decisions(db, org["id"])
    assert before["toggles"] == [] and before["profile"] is None
    assert _apply(client, org).status_code == 200
    db.expire_all()
    after = RO.published_decisions(db, org["id"])
    assert after["version"] != before["version"]
    assert {t["rule_id"]: (t["on"], t["params"], t["locked"]) for t in after["toggles"]}[RULE] == (
        True, {"n": 3}, True)
    assert after["profile"] == {"id": "standard", "version": "1"}
    assert RO.published_decisions(db, org["id"])["version"] == after["version"]


def test_a_pulled_lock_refuses_a_project_override_on_the_box(client, org, monkeypatch):  # noqa: F811
    _self_host(monkeypatch)
    pid = _box_project(client, org["owner"])
    db = _db()
    user = _user(db)
    RO.apply_decisions(db, _record())
    r = T.resolve(db, RULE, pid)
    assert r["locked"] and r["on"] == {"value": True, "source": "org"}
    with pytest.raises(Refused) as e:
        T.set_project_toggle(db, pid, RULE, {"on": False}, actor=user, reason="try")
    assert e.value.rule_id == T.LOCK_RULE
    assert T.resolve(db, RULE, pid)["on"]["value"] is True


def test_an_unlocked_pull_lets_the_project_decide(client, org, monkeypatch):  # noqa: F811
    _self_host(monkeypatch)
    pid = _box_project(client, org["owner"])
    db = _db()
    RO.apply_decisions(db, _record(locked=False))
    T.set_project_toggle(db, pid, RULE, {"on": False}, actor=_user(db), reason="local")
    assert T.resolve(db, RULE, pid)["on"] == {"value": False, "source": "project"}


def test_a_box_that_has_not_pulled_shows_the_old_version(client, org):  # noqa: F811
    db = _db()
    key = _key(db, org)
    RO.record_box_applied(db, key, org["id"], "old")
    assert _apply(client, org).status_code == 200
    db.expire_all()
    pub = RO.published_decisions(db, org["id"])
    st = RO.box_rule_state(db, key.id, pub)
    assert st["applied_version"] == "old" and st["applied_version"] != pub["version"]
    assert st["pending"] is True and st["published_version"] == pub["version"]
    RO.record_box_applied(db, key, org["id"], pub["version"])
    assert RO.box_rule_state(db, key.id, pub)["pending"] is False


def test_a_box_never_reported_has_no_applied_version(client, org):  # noqa: F811
    db = _db()
    key = _key(db, org)
    st = RO.box_rule_state(db, key.id, RO.published_decisions(db, org["id"]))
    assert st["applied_version"] is None and st["pending"] is True


def test_pull_applies_then_reports_and_skips_when_current(client, org, monkeypatch):  # noqa: F811
    pid = _box_project(client, org["owner"])
    reports = []

    class Resp:
        def __init__(self, code=200, body=None):
            self.status_code, self._b = code, body or {}
        def json(self): return self._b
        def raise_for_status(self): pass

    rec = _record()
    monkeypatch.setattr(code_sync.httpx, "get", lambda *a, **k: Resp(200, rec))
    monkeypatch.setattr(code_sync.httpx, "post",
                        lambda url, **k: reports.append(k["json"]) or Resp())
    db = _db()
    out = code_sync.pull_rule_decisions(db, cloud_url="https://c", api_key="k")
    assert out == {"pulled": True, "version": "v1", "changed": True, "reported": True}
    assert reports == [{"org_id": "org_cloud", "version": "v1"}]
    assert T.resolve(db, RULE, pid)["locked"] is True
    out = code_sync.pull_rule_decisions(db, cloud_url="https://c", api_key="k")
    assert out["changed"] is False
    rec.update(version="v2", toggles=[])        # the org dropped its decision
    code_sync.pull_rule_decisions(db, cloud_url="https://c", api_key="k")
    assert T.resolve(db, RULE, pid)["locked"] is False


def test_a_failed_report_is_retried_not_claimed(client, org, monkeypatch):  # noqa: F811
    import httpx

    class Resp:
        status_code = 200
        def json(self): return _record()
        def raise_for_status(self): pass

    def boom(*a, **k):
        raise httpx.ConnectError("down")

    monkeypatch.setattr(code_sync.httpx, "get", lambda *a, **k: Resp())
    monkeypatch.setattr(code_sync.httpx, "post", boom)
    db = _db()
    assert code_sync.pull_rule_decisions(db, cloud_url="https://c", api_key="k")["reported"] is False
