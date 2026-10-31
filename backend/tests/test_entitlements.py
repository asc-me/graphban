"""PRD-62 S2 (GRPH-1248): the org entitlement registry.

What these pin, in the order the PRD argues them:

- **D2 — a subscription has no secret field.** Refused at the API (REST and MCP) AND impossible
  in the schema: the CHECK constraint is asserted by writing around the service.
- **D3 — api_key/cloud hold a handle, never the key.** A value shaped like a raw key is refused.
- **Unknown fields are refused, not dropped.** Dropping `password` would answer a request to
  store a credential with a success.
- **D8 — premium.** `free` and `pro` get the standard 402 / `quota_exceeded`.
- **S2 — a runner declares what it can run at `register_agent`, and the declaration is
  trusted, not verified** — every row says so, and a declared runner is never marked verified.
"""
import pytest
from sqlalchemy.exc import IntegrityError

SEED_PW = "graphban"


def _login(client, email, password=SEED_PW):
    r = client.post("/api/auth/login", json={"email": email, "password": password})
    assert r.status_code == 200, r.text
    return {"Authorization": f"Bearer {r.json()['access_token']}"}


def _set_plan(org_id: str, plan: str) -> None:
    from app.db import SessionLocal
    from app.models import Organization

    db = SessionLocal()
    try:
        db.get(Organization, org_id).plan = plan
        db.commit()
    finally:
        db.close()


@pytest.fixture()
def hosted(monkeypatch):
    from app.config import settings

    monkeypatch.setattr(settings, "hosted_mode", True)
    return settings


@pytest.fixture()
def org(client, hosted):
    owner = _login(client, "alex@ascme-labs.com")
    o = client.post("/api/orgs", json={"name": "Acme"}, headers=owner).json()
    p = client.post("/api/projects", json={"name": "Rocket"}, headers=owner).json()
    me = client.get("/api/auth/me", headers=owner).json()
    key = client.post("/api/api-keys", json={"name": "ent", "project_id": p["id"],
                                             "tool_tiers": ["fleet"]},
                      headers=owner).json()["plaintext"]
    _set_plan(o["id"], "team")
    return {"owner": owner, "id": o["id"], "project": p, "user_id": me["id"], "key": key}


def _url(org):
    return f"/api/orgs/{org['id']}/entitlements"


def _rpc(client, key, tool, args=None):
    return client.post(
        "/api/mcp",
        json={"jsonrpc": "2.0", "id": 1, "method": "tools/call",
              "params": {"name": tool, "arguments": args or {}}},
        headers={"X-API-Key": key},
    ).json()["result"]


def _ok(client, key, tool, args=None):
    res = _rpc(client, key, tool, args)
    assert not res.get("isError"), res
    return res["structuredContent"]


def _err(client, key, tool, args=None):
    res = _rpc(client, key, tool, args)
    assert res.get("isError"), res
    return res["structuredContent"]["error"]


def _subscription(org, **extra):
    return {"vendor": "anthropic", "kind": "subscription", "owner_user_id": org["user_id"],
            "plan_label": "Max", "reset_cadence": "weekly", **extra}


# ---- D2: no secret on a subscription — refused at the API, impossible in the schema ---------

def test_a_subscription_with_a_secret_ref_is_refused_at_the_api(client, org):
    r = client.post(_url(org), json=_subscription(org, secret_ref="CLAUDE_TOKEN"),
                    headers=org["owner"])
    assert r.status_code == 422, r.text
    assert "no secret field" in r.json()["detail"]
    assert client.get(_url(org), headers=org["owner"]).json() == [], "nothing was written"


@pytest.mark.parametrize("field", ["password", "oauth_token", "api_key", "session_token",
                                   "secret"])
def test_a_credential_shaped_field_is_refused_not_dropped(client, org, field):
    """The quiet failure is the dangerous one: a schema that ignored `password` would return
    201 and the caller would believe the server now holds it."""
    r = client.post(_url(org), json=_subscription(org, **{field: "hunter2"}),
                    headers=org["owner"])
    assert r.status_code == 422, r.text
    assert "never stores a credential" in r.json()["detail"]


def test_the_schema_refuses_a_secret_on_a_subscription_even_past_the_service(client, org):
    """'Impossible in the schema' (acceptance 3), proven by writing AROUND the service."""
    from app.db import SessionLocal
    from app.models import Entitlement

    db = SessionLocal()
    try:
        db.add(Entitlement(id="ent_raw", org_id=org["id"], vendor="anthropic",
                           kind="subscription", owner_user_id=org["user_id"],
                           secret_ref="CLAUDE_TOKEN"))
        with pytest.raises(IntegrityError):
            db.commit()
        db.rollback()
        # The control: the same row with a kind that may hold a handle is accepted, so the
        # refusal above is the CHECK on kind and not some other constraint.
        db.add(Entitlement(id="ent_raw", org_id=org["id"], vendor="anthropic",
                           kind="api_key", secret_ref="ANTHROPIC_API_KEY"))
        db.commit()
    finally:
        db.close()


def test_the_check_constraint_names_the_same_kinds_as_the_service():
    """The CHECK carries a literal because a model cannot import the service enum."""
    from app.models import Entitlement
    from app.services.entitlements import SECRET_KINDS

    (check,) = [c for c in Entitlement.__table__.constraints
                if c.name == "ck_entitlements_secret_only_api_key_cloud"]
    text = str(check.sqltext)
    assert all(f"'{k}'" in text for k in SECRET_KINDS)
    assert "'subscription'" not in text and "'local'" not in text


def test_a_kind_change_cannot_leave_a_subscription_holding_a_secret_ref(client, org):
    made = client.post(_url(org), json={"vendor": "anthropic", "kind": "api_key",
                                        "secret_ref": "ANTHROPIC_API_KEY",
                                        "owner_user_id": org["user_id"]},
                       headers=org["owner"])
    assert made.status_code == 201, made.text
    url = f"{_url(org)}/{made.json()['id']}"

    r = client.patch(url, json={"kind": "subscription"}, headers=org["owner"])
    assert r.status_code == 422, r.text

    r = client.patch(url, json={"kind": "subscription", "secret_ref": None},
                     headers=org["owner"])
    assert r.status_code == 200, r.text
    assert r.json()["kind"] == "subscription" and r.json()["secret_ref"] is None


# ---- D3: api_key/cloud carry a handle, never the key ------------------------------------------

def test_an_api_key_entitlement_keeps_a_handle(client, org):
    r = client.post(_url(org), json={"vendor": "openai", "kind": "api_key",
                                     "secret_ref": "OPENAI_API_KEY", "token_budget": 5_000_000},
                    headers=org["owner"])
    assert r.status_code == 201, r.text
    row = r.json()
    assert row["secret_ref"] == "OPENAI_API_KEY" and row["token_budget"] == 5_000_000


@pytest.mark.parametrize("raw", ["sk-ant-api03-abcdefghijklmnop", "sk-proj-abc123",
                                 "AKIAIOSFODNN7EXAMPLE", "has spaces in it"])
def test_a_secret_ref_shaped_like_the_key_itself_is_refused(client, org, raw):
    r = client.post(_url(org), json={"vendor": "openai", "kind": "api_key", "secret_ref": raw},
                    headers=org["owner"])
    assert r.status_code == 422, r.text
    assert "NAME the secret" in r.json()["detail"]


def test_a_subscription_needs_an_owner_who_holds_a_seat(client, org):
    no_owner = {k: v for k, v in _subscription(org).items() if k != "owner_user_id"}
    assert client.post(_url(org), json=no_owner, headers=org["owner"]).status_code == 422
    stranger = _subscription(org, owner_user_id="usr_nobody")
    assert client.post(_url(org), json=stranger, headers=org["owner"]).status_code == 422


# ---- D8: premium ------------------------------------------------------------------------------

@pytest.mark.parametrize("plan", ["free", "pro"])
def test_free_and_pro_are_refused_with_the_standard_quota_error(client, org, plan):
    _set_plan(org["id"], plan)
    assert client.get(_url(org), headers=org["owner"]).status_code == 402
    assert client.post(_url(org), json=_subscription(org),
                       headers=org["owner"]).status_code == 402
    assert _err(client, org["key"], "list_entitlements")["code"] == "quota_exceeded"
    assert _err(client, org["key"], "set_entitlement",
                {"vendor": "anthropic", "kind": "local"})["code"] == "quota_exceeded"


@pytest.mark.parametrize("plan", ["team", "enterprise"])
def test_team_and_enterprise_may_use_it(client, org, plan):
    _set_plan(org["id"], plan)
    assert client.get(_url(org), headers=org["owner"]).status_code == 200
    assert _ok(client, org["key"], "list_entitlements") == {"entitlements": []}


def test_a_project_with_no_org_is_refused_rather_than_waved_through(client, auth):
    """Self-host: no org, so no plan but the default. D8 keeps it behind the same check."""
    pid = client.post("/api/projects", json={"name": "Loner"}, headers=auth).json()["id"]
    key = client.post("/api/api-keys", json={"name": "k", "project_id": pid},
                      headers=auth).json()["plaintext"]
    err = _err(client, key, "list_entitlements")
    assert err["code"] == "quota_exceeded" and "no org" in err["message"]


# ---- the MCP surface is the same service ------------------------------------------------------

def test_mcp_set_then_list_round_trips_and_refuses_what_rest_refuses(client, org):
    made = _ok(client, org["key"], "set_entitlement",
               {"vendor": "Anthropic", "kind": "subscription", "owner_user_id": org["user_id"],
                "plan_label": "Team seat"})
    assert made["vendor"] == "anthropic" and made["secret_ref"] is None

    patched = _ok(client, org["key"], "set_entitlement", {"id": made["id"], "runner": "mbp-1"})
    assert patched["runner"] == "mbp-1" and patched["plan_label"] == "Team seat"

    rows = _ok(client, org["key"], "list_entitlements")["entitlements"]
    assert [r["id"] for r in rows] == [made["id"]]

    err = _err(client, org["key"], "set_entitlement",
               {"id": made["id"], "secret_ref": "CLAUDE_TOKEN"})
    assert err["code"] == "validation" and "no secret field" in err["message"]
    # An argument the manifest never declared still reaches the service and is refused —
    # the dispatcher's habit of ignoring unknown extras must not apply here.
    err = _err(client, org["key"], "set_entitlement", {"id": made["id"], "oauth_token": "x"})
    assert err["code"] == "validation" and "never stores a credential" in err["message"]


def test_another_orgs_entitlement_cannot_be_patched(client, org):
    other = _login(client, "dana@ascme-labs.com")
    o2 = client.post("/api/orgs", json={"name": "Northbeam"}, headers=other).json()
    _set_plan(o2["id"], "team")
    theirs = client.post(f"/api/orgs/{o2['id']}/entitlements",
                         json={"vendor": "openai", "kind": "local"}, headers=other).json()
    err = _err(client, org["key"], "set_entitlement", {"id": theirs["id"], "runner": "mine"})
    assert err["code"] == "not_found"


# ---- S2: a runner declares what it can run; trusted, not verified ------------------------------

def test_a_runner_declares_entitlements_at_registration_and_the_list_says_it_is_unverified(
        client, org):
    ent = _ok(client, org["key"], "set_entitlement",
              {"vendor": "anthropic", "kind": "subscription", "owner_user_id": org["user_id"]})

    reg = _ok(client, org["key"], "register_agent",
              {"label": "runner", "capabilities": {"host": "mbp-1"},
               "entitlements": [ent["id"]]})
    assert reg["entitlements"]["declared"] == [ent["id"]]
    assert reg["entitlements"]["verified"] is False

    (row,) = _ok(client, org["key"], "list_entitlements")["entitlements"]
    (decl,) = row["declared_by"]
    assert decl["agent_id"] == reg["agent_id"] and decl["host"] == "mbp-1"
    assert decl["verified"] is False
    assert "not verified" in row["declaration"]

    rest = client.get(_url(org), headers=org["owner"]).json()
    assert rest[0]["declared_by"][0]["agent_id"] == reg["agent_id"]
    assert "not verified" in rest[0]["declaration"]


def test_an_undeclared_entitlement_lists_no_runner(client, org):
    _ok(client, org["key"], "register_agent", {"label": "plain"})
    ent = _ok(client, org["key"], "set_entitlement", {"vendor": "ollama", "kind": "local"})
    (row,) = _ok(client, org["key"], "list_entitlements")["entitlements"]
    assert row["id"] == ent["id"] and row["declared_by"] == []


def test_declaring_an_unknown_entitlement_refuses_and_mints_no_agent(client, org):
    from app.db import SessionLocal
    from app.models import Agent

    def agents():
        db = SessionLocal()
        try:
            return db.query(Agent).filter(Agent.project_id == org["project"]["id"]).count()
        finally:
            db.close()

    before = agents()
    err = _err(client, org["key"], "register_agent",
               {"label": "liar", "entitlements": ["ent_nope"]})
    assert err["code"] == "validation" and "ent_nope" in err["message"]
    assert agents() == before


def test_declaring_on_a_free_plan_is_refused(client, org):
    ent = _ok(client, org["key"], "set_entitlement", {"vendor": "ollama", "kind": "local"})
    _set_plan(org["id"], "free")
    err = _err(client, org["key"], "register_agent", {"entitlements": [ent["id"]]})
    assert err["code"] == "quota_exceeded"
    # Registering WITHOUT a declaration is untouched by the gate.
    assert _ok(client, org["key"], "register_agent", {"label": "plain"})["agent_id"]


def test_a_plain_member_may_read_but_not_write(client, org):
    from app.db import SessionLocal
    from app.models import OrgMembership, User

    db = SessionLocal()
    try:
        dana = db.query(User).filter(User.email == "dana@ascme-labs.com").one()
        db.add(OrgMembership(org_id=org["id"], user_id=dana.id, role="member"))
        db.commit()
    finally:
        db.close()
    member = _login(client, "dana@ascme-labs.com")
    assert client.get(_url(org), headers=member).status_code == 200
    assert client.post(_url(org), json={"vendor": "ollama", "kind": "local"},
                       headers=member).status_code == 403


# ---- PRD-62 S3 (GRPH-1249): the headroom ledger ---------------------------------------------

def _new_ent(client, org):
    r = client.post(_url(org), json=_subscription(org), headers=org["owner"])
    assert r.status_code == 201, r.text
    return r.json()["id"]


def _ev(client, org, ent, event, **extra):
    return _ok(client, org["key"], "report_entitlement_event",
               {"id": ent, "event": event, **extra})


def _state(client, org, ent):
    (row,) = [e for e in client.get(_url(org), headers=org["owner"]).json() if e["id"] == ent]
    return row["headroom"]


def test_an_unreported_entitlement_is_unknown_not_available(client, org):
    ent = _new_ent(client, org)
    h = _state(client, org, ent)
    assert h["state"] == "unknown" and h["since"] is None
    assert h["spend"] == {"tokens": 0, "reported": 0, "unreported": 0}


def test_an_unknown_entitlement_admits_one_child_at_a_time(client, org):
    ent = _new_ent(client, org)
    assert _ev(client, org, ent, "spawn_start")["admitted"] is True
    second = _ev(client, org, ent, "spawn_start")
    assert second["admitted"] is False and "one child in flight" in second["reason"]
    assert _state(client, org, ent)["in_flight"] == 1, "a refusal records nothing"


def test_registering_moves_unknown_to_available_and_lifts_the_cap(client, org):
    ent = _new_ent(client, org)
    _ev(client, org, ent, "spawn_start")
    assert _ev(client, org, ent, "registered")["headroom"]["state"] == "available"
    assert _ev(client, org, ent, "spawn_start")["admitted"] is True
    assert _ev(client, org, ent, "spawn_start")["admitted"] is True


def test_a_limit_hit_exhausts_it_with_the_vendors_text_and_refuses_every_spawn(client, org):
    ent = _new_ent(client, org)
    _ev(client, org, ent, "spawn_start")
    text = "You've hit your limit · resets 9pm (America/New_York)"
    h = _ev(client, org, ent, "limit_hit", vendor_text=text)["headroom"]
    assert h["state"] == "exhausted" and h["vendor_text"] == text
    for _ in range(3):
        assert _ev(client, org, ent, "spawn_start")["admitted"] is False
    # A later successful spawn after the exhaustion makes it available again.
    assert _ev(client, org, ent, "registered")["headroom"]["state"] == "available"


def test_a_non_quota_failure_is_broken_and_probed_by_one_child(client, org):
    ent = _new_ent(client, org)
    _ev(client, org, ent, "spawn_start")
    assert _ev(client, org, ent, "spawn_failed", vendor_text="not logged in")[
        "headroom"]["state"] == "broken"
    assert _ev(client, org, ent, "spawn_start")["admitted"] is True
    assert _ev(client, org, ent, "spawn_start")["admitted"] is False
    assert _ev(client, org, ent, "registered")["headroom"]["state"] == "available"


def test_a_failure_or_limit_without_the_vendors_text_is_refused(client, org):
    ent = _new_ent(client, org)
    assert "vendor_text" in _err(client, org["key"], "report_entitlement_event",
                                 {"id": ent, "event": "limit_hit"})["message"]


def test_unreported_spend_is_a_count_not_a_zero(client, org):
    ent = _new_ent(client, org)
    for _ in range(3):
        _ev(client, org, ent, "spawn_start")
        _ev(client, org, ent, "registered")
    _ev(client, org, ent, "spend", tokens=1200)
    assert _state(client, org, ent)["spend"] == {"tokens": 1200, "reported": 1, "unreported": 2}


def test_the_ledger_is_org_scoped_and_premium(client, org):
    other = _err(client, org["key"], "report_entitlement_event",
                 {"id": "ent_nope", "event": "registered"})
    assert other["code"] == "not_found"
    _set_plan(org["id"], "free")
    assert _err(client, org["key"], "report_entitlement_event",
                {"id": "ent_x", "event": "registered"})["code"] == "quota_exceeded"
