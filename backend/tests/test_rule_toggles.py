"""PRD-P63 S2a (GRPH-1262): library rule toggles, layered project → org → platform default.

What these pin, in the order the PRD argues them:

- **D4 — resolution at all three scopes**, each read carrying its `source`.
- **D4 — a lock fixes the value for projects**: a project override is refused naming
  `rules.org_lock_holds`, and an org admin's change to a locked rule reaches every project as
  ONE audit event naming the rule, the old and new values, and the projects it changed.
- **D10 — a person adopts.** The recommendation engine's identity, and an agent's API key, are
  refused as writers; no MCP tool writes a toggle.
- **D11 — org-level writes are `team` / `enterprise`**: 402 / `quota_exceeded` otherwise.
- **D3 — the registry draws a library rule with `truth = db`, `control = toggle`**, and an off
  toggle as NOT USED, never hidden.
"""
import pytest

from app.services import rule_toggles as T
from app.services import rules as R

SEED_PW = "graphban"
RULE = "review.sabotage_required"


def _login(client, email, password=SEED_PW):
    r = client.post("/api/auth/login", json={"email": email, "password": password})
    assert r.status_code == 200, r.text
    return {"Authorization": f"Bearer {r.json()['access_token']}"}


def _db():
    from app.db import SessionLocal
    return SessionLocal()


def _set_plan(org_id: str, plan: str) -> None:
    from app.models import Organization

    db = _db()
    try:
        db.get(Organization, org_id).plan = plan
        db.commit()
    finally:
        db.close()


@pytest.fixture()
def library(monkeypatch):
    """One library rule, as the pack loader would register it. The default is OFF, so a rule
    that resolved to the default by mistake reads differently from one an org turned on."""
    monkeypatch.setattr(T, "LIBRARY", {})
    T.register(T.LibraryRule(
        id=RULE, stage="review", title="Sabotage required",
        summary="Sign-off needs a reviewer sabotage whose tests failed.",
        enforcement="enforced", enforcer=R.Enforcer("graphban_source", "app/services/fleet.py"),
        default_on=False, default_params={"min_tests_failed": 1}))
    return T.LIBRARY


@pytest.fixture()
def org(client, monkeypatch, library):
    from app.config import settings

    monkeypatch.setattr(settings, "hosted_mode", True)
    owner = _login(client, "alex@ascme-labs.com")
    o = client.post("/api/orgs", json={"name": "Acme"}, headers=owner).json()
    a = client.post("/api/projects", json={"name": "Rocket"}, headers=owner).json()
    b = client.post("/api/projects", json={"name": "Probe"}, headers=owner).json()
    _set_plan(o["id"], "team")
    return {"owner": owner, "id": o["id"], "a": a["id"], "b": b["id"]}


def _toggles(client, org, pid):
    r = client.get("/api/rules/toggles", params={"project_id": pid}, headers=org["owner"])
    assert r.status_code == 200, r.text
    return {t["rule_id"]: t for t in r.json()["toggles"]}


def _put_project(client, org, pid, body):
    return client.put(f"/api/rules/toggles/{RULE}", params={"project_id": pid}, json=body,
                      headers=org["owner"])


class _Svc:
    """What a removed org route used to return. Behavior tests call the service."""

    def __init__(self, status, body=None, text=""):
        self.status_code = status
        self._body = {} if body is None else body
        self.text = text or (body if isinstance(body, str) else str(body))

    def json(self):
        return self._body if isinstance(self._body, dict) else {"detail": self.text}


def _owner(db):
    from sqlalchemy import select
    from app.models import User
    user = db.scalar(select(User).where(User.email == "alex@ascme-labs.com"))
    assert user is not None
    return user


def _as_http(fn):
    from app.errors import NotFound, QuotaExceeded, Validation
    try:
        return _Svc(200, fn())
    except QuotaExceeded as e:
        detail = str(e) + (f" — {e.hint}" if e.hint else "")
        return _Svc(402, {"detail": detail}, detail)
    except Validation as e:
        detail = str(e) + (f" — {e.hint}" if e.hint else "")
        return _Svc(422, {"detail": detail}, detail)
    except NotFound as e:
        detail = str(e) + (f" — {e.hint}" if e.hint else "")
        return _Svc(404, {"detail": detail}, detail)


def _put_org(client, org, body, rule_id=RULE, dry_run=False):
    """Org toggle write through the service. The product route is the console's."""
    db = _db()
    try:
        changes = dict(body)
        reason = changes.pop("reason", "")
        return _as_http(lambda: T.set_org_toggle(
            db, org["id"], rule_id, changes, actor=_owner(db), reason=reason, dry_run=dry_run))
    finally:
        db.close()


def _toggle_events():
    from app.models import Event

    db = _db()
    try:
        return [(e.project_id, e.meta) for e in db.query(Event).filter(
            Event.action == "set_rule_toggle").order_by(Event.id)]
    finally:
        db.close()


def _sv(d):
    """A toggle value without `inherits` (GRPH-1631 has its own test)."""
    return {k: v for k, v in d.items() if k != "inherits"}


# ---- D4: resolution at all three scopes ---------------------------------------------------

def test_platform_default_when_nothing_is_set(client, org):
    t = _toggles(client, org, org["a"])[RULE]
    assert _sv(t["on"]) == {"value": False, "source": "platform default"}
    assert _sv(t["params"]) == {"value": {"min_tests_failed": 1}, "source": "platform default"}
    assert t["locked"] is False


def test_org_value_reaches_every_project_and_a_project_overrides_its_own(client, org):
    assert _put_org(client, org, {"on": True, "reason": "P62 escapes"}).status_code == 200
    for pid in (org["a"], org["b"]):
        assert _sv(_toggles(client, org, pid)[RULE]["on"]) == {"value": True, "source": "org"}

    r = _put_project(client, org, org["a"], {"on": False, "params": {"min_tests_failed": 2},
                                             "reason": "prototype"})
    assert r.status_code == 200, r.text
    a, b = _toggles(client, org, org["a"])[RULE], _toggles(client, org, org["b"])[RULE]
    assert _sv(a["on"]) == {"value": False, "source": "project"}
    assert _sv(a["params"]) == {"value": {"min_tests_failed": 2}, "source": "project"}
    # The org layer still holds for the project that did not override it.
    assert _sv(b["on"]) == {"value": True, "source": "org"}
    assert b["params"]["source"] == "platform default"


def test_clearing_a_project_value_inherits_again(client, org):
    _put_org(client, org, {"on": True, "reason": "r"})
    _put_project(client, org, org["a"], {"on": False, "reason": "r"})
    assert _put_project(client, org, org["a"], {"on": None, "reason": "back to the org"}
                        ).status_code == 200
    assert _sv(_toggles(client, org, org["a"])[RULE]["on"]) == {"value": True, "source": "org"}


def test_resolution_at_the_service_ignores_no_layer(library):
    """The pure resolution, three layers, no HTTP: each layer wins only when set."""
    from app.models import RuleToggle

    rule = library[RULE]
    org_row = RuleToggle(rule_id=RULE, org_id="o", on=True)
    proj_row = RuleToggle(rule_id=RULE, project_id="p", params={"min_tests_failed": 3})
    r = T._resolve(rule, proj_row, org_row, "o")
    assert _sv(r["on"]) == {"value": True, "source": "org"}
    assert _sv(r["params"]) == {"value": {"min_tests_failed": 3}, "source": "project"}
    assert T._resolve(rule, None, None, None)["on"]["source"] == "platform default"


# ---- D4: locks -----------------------------------------------------------------------------

def test_a_locked_rule_refuses_a_project_override_with_its_rule_id(client, org):
    from app.models import Event

    assert _put_org(client, org, {"on": True, "locked": True, "reason": "regulated"}
                    ).status_code == 200
    t = _toggles(client, org, org["a"])[RULE]
    assert t["locked"] is True and t["locked_by"] == org["id"]
    assert _sv(t["on"]) == {"value": True, "source": "org"}
    # Locking with no params set writes the default down, so the org is the source of both.
    assert t["params"]["source"] == "org"

    r = _put_project(client, org, org["a"], {"on": False, "reason": "too slow"})
    assert r.status_code == 409, r.text
    db = _db()
    try:
        ev = db.query(Event).filter(Event.action == "refusal").order_by(Event.id.desc()).first()
        assert ev.meta["rule_id"] == T.LOCK_RULE
        assert ev.meta["locked_rule"] == RULE
    finally:
        db.close()
    assert _toggles(client, org, org["a"])[RULE]["on"]["value"] is True


def test_a_lock_hides_a_project_override_and_unlocking_gives_it_back(client, org):
    _put_project(client, org, org["a"], {"on": True, "reason": "ours"})
    _put_org(client, org, {"on": False, "locked": True, "reason": "org says off"})
    assert _sv(_toggles(client, org, org["a"])[RULE]["on"]) == {"value": False, "source": "org"}
    _put_org(client, org, {"locked": False, "reason": "lifted"})
    assert _sv(_toggles(client, org, org["a"])[RULE]["on"]) == {"value": True, "source": "project"}


def test_an_org_change_to_a_locked_rule_cascades_as_one_event(client, org):
    _put_project(client, org, org["a"], {"on": True, "reason": "ours"})
    _put_org(client, org, {"on": True, "locked": True, "reason": "regulated"})
    before = len(_toggle_events())

    r = _put_org(client, org, {"on": False, "reason": "too much friction"})
    assert r.status_code == 200, r.text
    assert sorted(r.json()["projects_changed"]) == sorted([org["a"], org["b"]])
    for pid in (org["a"], org["b"]):
        assert _sv(_toggles(client, org, pid)[RULE]["on"]) == {"value": False, "source": "org"}

    events = _toggle_events()[before:]
    assert len(events) == 1
    project_id, meta = events[0]
    assert project_id is None and meta["scope"] == "org"
    assert meta["old"]["on"] is True and meta["new"]["on"] is False
    assert sorted(meta["projects_changed"]) == sorted([org["a"], org["b"]])
    assert meta["reason"] == "too much friction"


def test_an_unlocked_org_change_names_only_the_projects_it_changed(client, org):
    _put_project(client, org, org["a"], {"on": False, "reason": "ours"})
    r = _put_org(client, org, {"on": False, "reason": "no-op for a, real for nobody"})
    assert r.json()["projects_changed"] == []
    r = _put_org(client, org, {"on": True, "reason": "on"})
    assert r.json()["projects_changed"] == [org["b"]]


def test_a_project_cannot_lock(client, org):
    assert _put_project(client, org, org["a"], {"locked": True, "reason": "x"}
                        ).status_code == 422


# ---- audit: who and why -------------------------------------------------------------------

def test_every_change_writes_an_event_with_who_and_why(client, org):
    from app.models import Event

    _put_project(client, org, org["a"], {"on": True, "reason": "caught two escapes"})
    db = _db()
    try:
        ev = db.query(Event).filter(Event.action == "set_rule_toggle").one()
        assert ev.actor_type == "user" and ev.actor_id
        assert ev.project_id == org["a"] and ev.target_id == RULE
        assert ev.meta["reason"] == "caught two escapes"
        assert ev.meta["effective_before"]["on"] is False
        assert ev.meta["effective_after"]["on"] is True
    finally:
        db.close()


@pytest.mark.parametrize("reason", ["", "   "])
def test_a_change_without_a_reason_is_refused(client, org, reason):
    assert _put_project(client, org, org["a"], {"on": True, "reason": reason}).status_code == 422
    assert _put_org(client, org, {"on": True, "reason": reason}).status_code == 422
    assert _toggle_events() == []


def test_params_are_the_templates_own_keys_and_types(client, org):
    assert _put_project(client, org, org["a"], {"params": {"bogus": 1}, "reason": "r"}
                        ).status_code == 422
    assert _put_project(client, org, org["a"], {"params": {"min_tests_failed": "1"},
                                                "reason": "r"}).status_code == 422
    assert _put_project(client, org, org["a"], {"params": {"min_tests_failed": True},
                                                "reason": "r"}).status_code == 422


def test_a_params_write_keeps_the_params_it_does_not_name(client, org):
    rid = T.TEST_SCOPE_RULE
    T.register(T.LibraryRule(
        id=rid, stage="build", title="Test scope", summary="",
        enforcement="checked", enforcer=R.Enforcer("graphban_source", "app/services/fleet.py"),
        default_on=True, default_params={"mode": "agent_and_pr",
                                         "engines": ["postgres", "sqlite"]}))
    url = f"/api/rules/toggles/{rid}"
    hdr = org["owner"]
    first = client.put(url, params={"project_id": org["a"]}, headers=hdr, json={
        "params": {"engines": ["sqlite"]}, "reason": "r"})
    assert first.status_code == 200, first.text
    second = client.put(url, params={"project_id": org["a"]}, headers=hdr, json={
        "params": {"mode": "agent_targeted"}, "reason": "r"})
    assert second.status_code == 200, second.text
    got = _toggles(client, org, org["a"])[rid]["params"]["value"]
    assert got["mode"] == "agent_targeted"
    assert got["engines"] == ["sqlite"]  # not reset to the template default


def test_an_unknown_rule_is_404(client, org):
    r = client.put("/api/rules/toggles/review.nope", params={"project_id": org["a"]},
                   json={"on": True, "reason": "r"}, headers=org["owner"])
    assert r.status_code == 404


def test_a_project_member_without_admin_is_refused(client, org):
    from app.models import Event, Membership, OrgMembership

    db = _db()
    try:
        db.add(OrgMembership(org_id=org["id"], user_id="u2", role="member"))
        db.add(Membership(user_id="u2", project_id=org["a"], role="member", access="write"))
        db.commit()
    finally:
        db.close()
    member = _login(client, "dana@ascme-labs.com")
    r = client.put(f"/api/rules/toggles/{RULE}", params={"project_id": org["a"]},
                   json={"on": True, "reason": "r"}, headers=member)
    assert r.status_code == 403, r.text
    db = _db()
    try:
        ev = db.query(Event).filter(Event.action == "refusal").order_by(Event.id.desc()).first()
        assert ev.meta["rule_id"] == T.ADMIN_RULE
    finally:
        db.close()


# ---- D10: a person adopts; the engine only suggests ---------------------------------------

def test_the_recommendation_engine_cannot_set_a_toggle(client, org):
    from app.errors import Refused
    from app.models import RuleToggle
    from app.services import rules_recommend

    db = _db()
    try:
        with pytest.raises(Refused) as e:
            T.set_project_toggle(db, org["a"], RULE, {"on": True},
                                 actor=rules_recommend.ENGINE_IDENTITY, reason="suggested")
        assert e.value.rule_id == T.PERSON_RULE
        with pytest.raises(Refused) as e:
            T.set_org_toggle(db, org["id"], RULE, {"on": True, "locked": True},
                             actor=rules_recommend.ENGINE_IDENTITY, reason="suggested")
        assert e.value.rule_id == T.PERSON_RULE
        assert db.query(RuleToggle).count() == 0
    finally:
        db.close()
    assert _toggle_events() == []


def test_an_agent_key_cannot_set_a_toggle(client, org):
    from app.errors import Refused
    from app.models import ApiKey

    key = client.post("/api/api-keys", json={"name": "k", "project_id": org["a"]},
                      headers=org["owner"]).json()["plaintext"]
    # Not over REST: the toggle routes take a logged-in person, never a key.
    r = client.put(f"/api/rules/toggles/{RULE}", params={"project_id": org["a"]},
                   json={"on": True, "reason": "r"}, headers={"X-API-Key": key})
    assert r.status_code == 401
    # Not at the service either, if a future caller hands it a key.
    db = _db()
    try:
        k = db.query(ApiKey).first()
        with pytest.raises(Refused) as e:
            T.set_project_toggle(db, org["a"], RULE, {"on": True}, actor=k, reason="r")
        assert e.value.rule_id == T.PERSON_RULE
    finally:
        db.close()


def test_no_mcp_tool_writes_a_toggle(client):
    from app.mcp_server import TOOLS

    toggle_tools = [t for t in TOOLS if "rule_toggle" in t["name"]]
    assert [t["name"] for t in toggle_tools] == ["get_rule_toggles"]
    assert toggle_tools[0]["annotations"]["readOnlyHint"] is True


def test_the_mcp_read_carries_source_and_lock(client, org):
    key = client.post("/api/api-keys", json={"name": "k", "project_id": org["a"],
                                             "tool_tiers": ["misc"]},
                      headers=org["owner"]).json()["plaintext"]
    _put_org(client, org, {"on": True, "locked": True, "reason": "r"})
    res = client.post("/api/mcp", json={
        "jsonrpc": "2.0", "id": 1, "method": "tools/call",
        "params": {"name": "get_rule_toggles", "arguments": {"project_id": org["a"]}}},
        headers={"X-API-Key": key}).json()["result"]
    assert not res.get("isError"), res
    out = res["structuredContent"]
    assert out["library_loaded"] is True
    t = out["toggles"][0]
    assert t["rule_id"] == RULE and _sv(t["on"]) == {"value": True, "source": "org"}
    assert t["locked"] is True


# ---- D11: org-level writes are plan-gated -------------------------------------------------

@pytest.mark.parametrize("plan", ["free", "pro"])
def test_org_writes_need_team_or_enterprise(client, org, plan):
    _set_plan(org["id"], plan)
    r = _put_org(client, org, {"on": True, "reason": "r"})
    assert r.status_code == 402, r.text
    assert "org-level rules" in r.json()["detail"]
    # Per-project toggles ship on every plan.
    assert _put_project(client, org, org["a"], {"on": True, "reason": "r"}).status_code == 200


def test_enterprise_may_write_at_org_level(client, org):
    _set_plan(org["id"], "enterprise")
    assert _put_org(client, org, {"on": True, "reason": "r"}).status_code == 200


# ---- D3: the registry draws a library rule as a toggle ------------------------------------

def test_registry_draws_a_library_rule_with_db_truth_and_a_toggle(client, org):
    r = client.get("/api/rules", params={"project_id": org["a"]}, headers=org["owner"])
    entry = next(x for x in r.json()["rules"] if x["id"] == RULE)
    assert entry["truth"] == "db" and entry["control"] == "toggle"
    assert entry["value"] is False and entry["source"] == "platform default"
    assert entry["enforcement"] == "enforced"
    # Every non-library rule says it has no toggle (the memory write mode is a select).
    assert all(x["control"] is None for x in r.json()["rules"]
               if x["id"] not in (RULE, "memory.write_mode"))


def test_an_off_toggle_is_drawn_not_used_never_hidden(client, org):
    def drawn():
        r = client.get("/api/rules/flow", params={"project_id": org["a"]},
                       headers=org["owner"])
        assert r.status_code == 200, r.text
        return next(x for s in r.json()["stages"] for x in s["rules"] if x["id"] == RULE)

    off = drawn()
    assert off["used"] is False and off["label"] == R.TOGGLED_OFF_LABEL
    _put_project(client, org, org["a"], {"on": True, "reason": "adopt"})
    on = drawn()
    assert on["used"] is True and on["label"] is None


def test_no_library_says_so(client, monkeypatch):
    monkeypatch.setattr(T, "LIBRARY", {})
    auth = _login(client, "alex@ascme-labs.com")
    r = client.get("/api/rules/toggles", params={"project_id": "core"}, headers=auth)
    assert r.json() == {"library_loaded": False, "library_reason": None, "toggles": []}


# ---- GRPH-1325: org-level read and the dry-run preview ------------------------------------

def _org_rows():
    from app.models import RuleToggle

    db = _db()
    try:
        return db.query(RuleToggle).filter(RuleToggle.org_id.isnot(None)).count()
    finally:
        db.close()


def _get_org(client, org):
    """Org toggle read through the service, still plan-gated. The route is the console's."""
    from app.services import quotas
    db = _db()
    try:
        def read():
            quotas.require_org_rules(db, org["id"])
            return T.org_view(db, org["id"])
        return _as_http(read)
    finally:
        db.close()


def test_org_read_inherited_adopted_and_locked(client, org):
    r = _get_org(client, org)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["library_loaded"] is True
    rule = body["rules"][0]
    assert rule["on"] == {"value": False, "source": "platform default"} and rule["locked"] is False
    assert {p["project_id"] for p in rule["projects"]} == {org["a"], org["b"]}

    _put_org(client, org, {"on": True, "reason": "adopt"})
    _put_project(client, org, org["a"], {"on": False, "reason": "ours"})
    rule = _get_org(client, org).json()["rules"][0]
    assert _sv(rule["on"]) == {"value": True, "source": "org"}
    by = {p["project_id"]: p for p in rule["projects"]}
    assert _sv(by[org["a"]]["on"]) == {"value": False, "source": "project"}
    assert _sv(by[org["b"]]["on"]) == {"value": True, "source": "org"}

    _put_org(client, org, {"locked": True, "reason": "regulated"})
    rule = _get_org(client, org).json()["rules"][0]
    assert rule["locked"] is True
    assert all(_sv(p["on"]) == {"value": True, "source": "org"} for p in rule["projects"])


def test_org_read_with_no_projects_and_plan_gate(client, org):
    db = _db()
    try:  # an org with no projects still reads: one call, not one per project
        empty = T.org_view(db, "org-without-projects")
    finally:
        db.close()
    assert empty["rules"][0]["projects"] == [] and empty["library_loaded"] is True
    _set_plan(org["id"], "free")
    assert _get_org(client, org).status_code == 402


def test_preview_writes_nothing(client, org):
    _put_project(client, org, org["a"], {"on": True, "reason": "ours"})
    rows, events = _org_rows(), len(_toggle_events())
    r = _put_org(client, org, {"on": True, "locked": True, "reason": "preview"}, dry_run=True)
    assert r.status_code == 200, r.text
    assert _org_rows() == rows and len(_toggle_events()) == events
    assert _get_org(client, org).json()["rules"][0]["locked"] is False


def test_preview_agrees_with_the_write(client, org):
    _put_project(client, org, org["a"], {"on": False, "reason": "ours"})
    body = {"on": True, "locked": True, "reason": "regulated"}
    prev = _put_org(client, org, body, dry_run=True).json()
    real = _put_org(client, org, body).json()
    assert prev["projects_changed"] and sorted(prev["projects_changed"]) == sorted(
        real["projects_changed"])
    assert prev == real


# ---- GRPH-1425: build.undeclared_files ------------------------------------------------------

def test_undeclared_files_mode_is_hold_or_warn_and_nothing_else(client, monkeypatch):
    monkeypatch.setattr(T, "LIBRARY", {})
    rid = T.UNDECLARED_FILES_RULE
    T.register(T.LibraryRule(
        id=rid, stage="build", title="Undeclared files",
        summary="Hold the PR or only warn.", enforcement="checked",
        enforcer=R.Enforcer("doc", "docs/rules.md"), default_on=True,
        default_params={"mode": T.UNDECLARED_FILES_DEFAULT}))
    auth = _login(client, "alex@ascme-labs.com")

    def put(mode):
        return client.put(f"/api/rules/toggles/{rid}", params={"project_id": "core"},
                          json={"params": {"mode": mode}, "reason": "try"}, headers=auth)

    with _db() as db:
        assert T.undeclared_files_mode(db, "core") == "hold"
    assert put("warn").status_code == 200
    with _db() as db:
        assert T.undeclared_files_mode(db, "core") == "warn"
    bad = put("ignore")
    assert bad.status_code == 422 and "hold" in bad.text


def test_undeclared_warned_observation_names_the_files_and_the_rule():
    from app.services import fleet as F
    got = F._observation_receipt("undeclared_warned", branch="b", base=None, url=None,
                                 commit=None, reason="web/src/App.tsx")
    assert got["kind"] == "note" and "web/src/App.tsx" in got["detail"]
    assert "build.undeclared_files" in got["detail"]
    with pytest.raises(F.ObservationError):
        F._observation_receipt("undeclared_warned", branch="b", base=None, url=None,
                               commit=None, reason="")

def test_generated_files_default_is_todays_list_and_a_list_param_is_accepted(library):
    # GRPH-1424: absent from the pack -> today's list; registered -> its default_params paths.
    assert "AGENTS.md" in T.generated_files(None, None)
    T.register(T.LibraryRule(
        id=T.GENERATED_FILES_RULE, stage="build", title="Generated files", summary="",
        enforcement="checked", enforcer=R.Enforcer("graphban_source", "fleet/src/gbfleet/supervisor.py"),
        default_on=True, default_params={"paths": list(T.GENERATED_FILES_DEFAULT)}))
    assert T.generated_files(None, None) == T.GENERATED_FILES_DEFAULT
    rule = T.LIBRARY[T.GENERATED_FILES_RULE]
    assert T._check_params(rule, {"paths": ["client/generated/**"]}) == {
        "paths": ["client/generated/**"]}
    with pytest.raises(Exception):
        T._check_params(rule, {"paths": "client/generated/**"})


# ---- GRPH-1631: what a Reset would inherit ---------------------------------------------------

def test_inherits_names_org_only_when_the_org_has_a_row_for_that_field(client, org):
    _put_project(client, org, org["a"], {"on": True, "params": {"min_tests_failed": 2},
                                         "reason": "own"})
    t = _toggles(client, org, org["a"])[RULE]
    # The project is in an org, but the org has no row for this rule: reset lands on the default.
    assert t["on"]["inherits"] == "platform default"
    assert t["params"]["inherits"] == "platform default"

    _put_org(client, org, {"on": False, "reason": "org on/off only"})
    t = _toggles(client, org, org["a"])[RULE]
    assert t["on"]["inherits"] == "org"
    assert t["params"]["inherits"] == "platform default"  # the org set `on`, not params
