"""GRPH-1297: linked boxes pull the org's rule decisions and report when they applied them."""
import pytest

from app.errors import Refused
from app.models import ApiKey
from app.services import code_sync
from app.services import rule_toggles as T
from app.services import rules_org as RO
from tests.test_rule_toggles import RULE, _db, _login, _set_plan
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


def _console_v1(version="4"):
    """What the console's rule-decisions route actually returns (S6 v1).

    `version` is the integer rendered as a decimal string. `profile.version` is null
    because the console has no rule pack. `published_at` is present and ignored.
    `params: null` means inherit. The contract sample uses `tests_first`; this box's
    library rule is `review.sabotage_required`, which is what apply can write.
    """
    return {"org_id": "org_cloud", "org_name": "Acme", "version": version,
            "published_at": "2026-11-09T00:00:00Z",
            "toggles": [{"rule_id": RULE, "on": True, "params": None, "locked": True}],
            "profile": {"id": "standard", "version": None}}


def _spy_apply(monkeypatch):
    applied = []
    real = RO.apply_decisions

    def spy(db, record):
        applied.append(record.get("version"))
        return real(db, record)

    monkeypatch.setattr(RO, "apply_decisions", spy)
    return applied


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


def test_console_v1_record_applies_and_a_repeat_does_not(client, org, monkeypatch):  # noqa: F811
    pid = _box_project(client, org["owner"])
    reports = []
    applied = _spy_apply(monkeypatch)

    class Resp:
        def __init__(self, body):
            self.status_code, self._b = 200, body
        def json(self): return self._b
        def raise_for_status(self): pass

    monkeypatch.setattr(code_sync.httpx, "get", lambda *a, **k: Resp(_console_v1()))
    monkeypatch.setattr(code_sync.httpx, "post", lambda url, **k: reports.append(k["json"]) or Resp({}))
    db = _db()
    out = code_sync.pull_rule_decisions(db, cloud_url="https://c", api_key="k")
    assert out == {"pulled": True, "version": "4", "changed": True, "reported": True}
    assert reports == [{"org_id": "org_cloud", "version": "4"}]
    assert applied == ["4"]
    assert T.resolve(db, RULE, pid)["locked"] is True
    again = code_sync.pull_rule_decisions(db, cloud_url="https://c", api_key="k")
    assert again["changed"] is False and again["reported"] is True
    assert applied == ["4"]


def test_nothing_published_does_not_apply_or_report(client, org, monkeypatch):  # noqa: F811
    reports = []
    applied = _spy_apply(monkeypatch)
    record = _console_v1(version=None)
    record["published_at"] = None

    class Resp:
        status_code = 200
        def json(self): return record
        def raise_for_status(self): pass

    monkeypatch.setattr(code_sync.httpx, "get", lambda *a, **k: Resp())
    monkeypatch.setattr(code_sync.httpx, "post", lambda *a, **k: reports.append(k.get("json")))
    out = code_sync.pull_rule_decisions(_db(), cloud_url="https://c", api_key="k")
    assert out == {"pulled": False, "reason": "nothing published"}
    assert reports == [] and applied == []


def test_a_crash_before_the_report_reports_next_pull_without_reapplying(client, org, monkeypatch):  # noqa: F811
    import httpx

    reports = []
    applied = _spy_apply(monkeypatch)
    posts = {"n": 0}

    class Resp:
        status_code = 200
        def json(self): return _console_v1()
        def raise_for_status(self): pass

    def post(*a, **k):
        posts["n"] += 1
        if posts["n"] == 1:
            raise httpx.ConnectError("down")
        reports.append(k["json"])
        return Resp()

    monkeypatch.setattr(code_sync.httpx, "get", lambda *a, **k: Resp())
    monkeypatch.setattr(code_sync.httpx, "post", post)
    db = _db()
    first = code_sync.pull_rule_decisions(db, cloud_url="https://c", api_key="k")
    assert first["reported"] is False and applied == ["4"]
    second = code_sync.pull_rule_decisions(db, cloud_url="https://c", api_key="k")
    assert second["changed"] is False and second["reported"] is True
    assert applied == ["4"]
    assert reports == [{"org_id": "org_cloud", "version": "4"}]


# ---- the pull rides the box's sync cadence (bounce on 33583cc1) --------------------------

class _FakeCloud:
    """The cloud as the box sees it: a published record it can change, and the reports."""

    def __init__(self, record):
        self.record, self.reports, self.graph = record, [], []

    def install(self, monkeypatch):
        cloud = self

        class Resp:
            def __init__(self, body=None):
                self.status_code, self._b = 200, body or {}
            def json(self): return self._b
            def raise_for_status(self): pass

        def get(url, **k):
            assert url.endswith("/api/sync/rule-decisions")
            return Resp(dict(cloud.record))

        def post(url, json=None, **k):
            (cloud.reports if url.endswith("/rule-decisions/applied") else cloud.graph).append(json)
            return Resp()

        monkeypatch.setattr(code_sync.httpx, "get", get)
        monkeypatch.setattr(code_sync.httpx, "post", post)
        return self


def test_a_normal_sync_applies_a_newly_published_lock(client, org, monkeypatch):  # noqa: F811
    _self_host(monkeypatch)
    pid = _box_project(client, org["owner"])
    cloud = _FakeCloud(_record(version="v1", locked=False)).install(monkeypatch)
    db = _db()
    out = code_sync.push(db, project_id=pid, cloud_url="https://c", api_key="k")
    assert out["rules"]["version"] == "v1" and T.resolve(db, RULE, pid)["locked"] is False
    cloud.record = _record(version="v2", locked=True)          # the org locks the rule
    out = code_sync.push(db, project_id=pid, cloud_url="https://c", api_key="k")
    assert out["rules"] == {"pulled": True, "version": "v2", "changed": True, "reported": True}
    assert cloud.reports[-1] == {"org_id": "org_cloud", "version": "v2"}
    assert T.resolve(db, RULE, pid)["locked"] is True
    with pytest.raises(Refused):
        T.set_project_toggle(db, pid, RULE, {"on": False}, actor=_user(db), reason="try")


def test_the_push_endpoint_pulls_rules_too(client, org, monkeypatch):  # noqa: F811
    _self_host(monkeypatch)
    pid = _box_project(client, org["owner"])
    _FakeCloud(_record(version="v9")).install(monkeypatch)
    monkeypatch.setattr(code_sync.settings, "sync_cloud_url", "https://c")
    monkeypatch.setattr(code_sync.settings, "sync_api_key", "k")
    r = client.post("/api/sync/push", json={"project_id": pid}, headers=org["owner"])
    assert r.status_code == 200, r.text
    assert r.json()["rules"]["version"] == "v9"
    assert T.resolve(_db(), RULE, pid)["locked"] is True


def test_a_failed_rule_pull_never_fails_the_push(client, org, monkeypatch):  # noqa: F811
    import httpx
    pid = _box_project(client, org["owner"])

    def down(*a, **k):
        raise httpx.ConnectError("down")

    monkeypatch.setattr(code_sync.httpx, "get", down)
    monkeypatch.setattr(code_sync.httpx, "post", lambda *a, **k: None)
    out = code_sync.push(_db(), project_id=pid, cloud_url="https://c", api_key="k")
    assert out["project_id"] == pid and "pushed" in out
    assert out["rules"]["pulled"] is False and "ConnectError" in out["rules"]["error"]


def test_pull_rules_endpoint_not_linked_is_409_like_push(client, org, monkeypatch):  # noqa: F811
    _self_host(monkeypatch)
    monkeypatch.setattr(code_sync.settings, "sync_cloud_url", "")
    monkeypatch.setattr(code_sync.settings, "sync_api_key", "")
    r = client.post("/api/sync/pull-rules", headers=org["owner"])
    assert r.status_code == 409, r.text


# ---- `changed_at` moves with every decision, not only a toggle write ---------------------

def _age(db, org_id, hours=5):
    from datetime import timedelta
    from app.models import OrgRuleVersion
    row = db.get(OrgRuleVersion, org_id)
    row.changed_at = row.changed_at - timedelta(hours=hours)
    db.commit()
    return row.changed_at


def test_changed_at_moves_when_the_org_drops_a_toggle(client, org):  # noqa: F811
    from app.models import RuleToggle
    assert _apply(client, org).status_code == 200
    db = _db()
    old = _age(db, org["id"])
    row = db.query(RuleToggle).filter_by(org_id=org["id"], rule_id=OTHER).one()
    db.delete(row)
    db.commit()
    pub = RO.published_decisions(db, org["id"])
    assert pub["changed_at"] > old


def test_changed_at_moves_when_a_profile_is_applied(client, org):  # noqa: F811
    db = _db()
    RO.published_decisions(db, org["id"])
    old = _age(db, org["id"])
    assert _apply(client, org).status_code == 200
    db.expire_all()
    assert RO.published_decisions(db, org["id"])["changed_at"] > old


def test_an_unchanged_record_keeps_its_changed_at_so_overdue_fires(client, org):  # noqa: F811
    db = _db()
    key = _key(db, org)
    RO.record_box_applied(db, key, org["id"], "old")
    RO.published_decisions(db, org["id"])
    _age(db, org["id"])
    pub = RO.published_decisions(db, org["id"])
    st = RO.box_rule_state(db, key.id, pub)
    assert st["pending"] is True and st["overdue"] is True


def _sync_view(db, org_id):
    """The console serves this view. The product keeps the service."""
    return RO.org_box_states(db, _user(db), org_id)


def test_org_admin_route_serves_each_boxes_sync_state(client, org, monkeypatch):  # noqa: F811
    db = _db()
    key = _key(db, org)
    body = _sync_view(db, org["id"])
    box = [b for b in body["boxes"] if b["name"] == "box-1"][0]
    assert box["applied_version"] is None and box["pending"] is True
    RO.record_box_applied(db, key, org["id"], "old")
    box = [b for b in _sync_view(db, org["id"])["boxes"] if b["name"] == "box-1"][0]
    assert box["applied_version"] == "old" and box["pending"] is True
    pub = RO.published_decisions(db, org["id"])
    RO.record_box_applied(db, key, org["id"], pub["version"])
    box = [b for b in _sync_view(db, org["id"])["boxes"] if b["name"] == "box-1"][0]
    assert box["pending"] is False and box["applied_version"] == pub["version"]


def test_org_sync_route_reports_published_and_overdue(client, org):  # noqa: F811
    db = _db()
    key = _key(db, org)
    RO.record_box_applied(db, key, org["id"], "old")
    pub = RO.published_decisions(db, org["id"])
    r = _sync_view(db, org["id"])
    assert r["published_version"] == pub["version"] and r["published_at"] is not None
    box = [b for b in r["boxes"] if b["name"] == "box-1"][0]
    assert box["published_version"] == pub["version"]
    assert box["pending"] is True and box["overdue"] is False   # just published: not overdue yet
    _age(db, org["id"])
    db.expire_all()
    box = [b for b in _sync_view(db, org["id"])["boxes"] if b["name"] == "box-1"][0]
    assert box["pending"] is True and box["overdue"] is True


def test_org_sync_route_refuses_a_plain_member_with_403(client, org):  # noqa: F811
    from fastapi import HTTPException
    from app.models import OrgMembership, User
    from sqlalchemy import select
    db = _db()
    try:
        dana = db.scalar(select(User).where(User.email == "dana@ascme-labs.com"))
        db.add(OrgMembership(org_id=org["id"], user_id=dana.id, role="member"))
        db.commit()
        with pytest.raises(HTTPException) as e:
            RO.org_box_states(db, dana, org["id"])
        assert e.value.status_code == 403
    finally:
        db.close()


def test_org_sync_route_is_404_for_a_real_org_the_caller_is_not_in(client, org):  # noqa: F811
    from fastapi import HTTPException
    dana = _login(client, "dana@ascme-labs.com")
    other = client.post("/api/orgs", json={"name": "Elsewhere"}, headers=dana)
    assert other.status_code in (200, 201), other.text
    _set_plan(other.json()["id"], "team")
    db = _db()
    try:
        with pytest.raises(HTTPException) as e:
            RO.org_box_states(db, _user(db), other.json()["id"])
        assert e.value.status_code == 404
    finally:
        db.close()


def test_org_sync_route_needs_team_or_enterprise(client, org):  # noqa: F811
    from app.errors import QuotaExceeded
    db = _db()
    _set_plan(org["id"], "free")
    db.expire_all()
    with pytest.raises(QuotaExceeded):
        _sync_view(db, org["id"])
    _set_plan(org["id"], "enterprise")
    db.expire_all()
    assert _sync_view(db, org["id"])["org_id"] == org["id"]
