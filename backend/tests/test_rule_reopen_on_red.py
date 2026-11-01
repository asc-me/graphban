"""PRD-P63 §6 primitive 5/7 (GRPH-1272): `merge.reopen_on_red`, the `reopen_on_red` primitive
bound to a library rule.

With the rule ON, a `suite_green` attestation that FAILS at the head an item was signed off on
returns the item to its builder (`next`, claim cleared) with the failure quoted in a note. It is
a server-side transition on attestation intake. On/off comes from the layered resolution.
"""
from app.services import fleet
from app.services import rule_pack as P
from app.services import rule_toggles as T
from app.services import rules as R
from tests.test_fleet_adversarial_gate import (  # noqa: F401  (fixtures + helpers)
    CUT_FROM, _ok, _rpc, db, key, proj)
from tests.test_rule_pack import FIX, TRUST, build

RULE = "merge.reopen_on_red"
HEAD = "abcdef0123456789abcdef0123456789abcdef01"
OTHER = "fedcba9876543210fedcba9876543210fedcba98"
RUN = "https://github.com/o/r/actions/runs/42/attempts/1"


def _library(monkeypatch, *, on: bool):
    monkeypatch.setattr(T, "LIBRARY", {})
    T.register(T.LibraryRule(
        id=RULE, stage="merge", title="Reopen on red", summary="s",
        enforcement="enforced", enforcer=R.Enforcer("graphban_source", "app/services/fleet.py"),
        default_on=on, default_params={}))


def _signed_off(client, key):
    """An item built by one agent and signed off by another at HEAD. Returns (item, builder)."""
    worker = _ok(client, key, "register_agent",
                 {"branch": "gb/test", "label": "w", "capabilities": {"instance": "w"}})
    _ok(client, key, "create_item", {"title": "some work", "status": "next", "effort": 0})
    c = _ok(client, key, "claim_next", {"agent_id": worker["agent_id"]})
    item = c["item"]["id"]
    _ok(client, key, "update_item",
        {"id": item, "agent_id": worker["agent_id"],
         "evidence": [{"kind": "note", "detail": f"gbfleet: branch cut from {CUT_FROM} (`gb/test`)"}]})
    _ok(client, key, "update_item", {"id": item, "status": "review", "agent_id": worker["agent_id"]})
    rev = _ok(client, key, "register_agent",
              {"branch": "gb/test", "label": "r", "role_hint": "reviewer",
               "capabilities": {"instance": "r"}})
    out = _ok(client, key, "sign_off", {"id": item, "agent_id": rev["agent_id"], "commit": HEAD})
    assert out["status"] == "done"
    return item, worker["agent_id"]


def _gate(client, auth, proj):
    return client.post("/api/api-keys", json={"name": "ci", "project_id": proj,
                       "scopes": ["read", "write", "gate"]}, headers=auth).json()["plaintext"]


def _ci(client, gate, item, *, commit: str, passed: bool):
    _ok(client, gate, "update_item", {"id": item, "evidence": [{
        "kind": "attestation", "adapter": "github-actions", "commit": commit, "run_ref": RUN,
        "branch": "gb/test",
        "predicates": [{"name": "suite_green", "passed": passed,
                        "detail": "backend-postgres (2) failed" if not passed
                        else f"CI passed on gb/test at {commit[:12]}"}]}]})
    return _ok(client, gate, "get_item_details", {"id": item})


# ---- both states ------------------------------------------------------------------------

def test_on_reopens_to_the_builder_with_the_failure_quoted(client, key, auth, proj, monkeypatch,
                                                           db):
    _library(monkeypatch, on=True)
    item, builder = _signed_off(client, key)

    got = _ci(client, _gate(client, auth, proj), item, commit=HEAD, passed=False)

    assert got["status"] == "next"
    assert not got["claimed_by"] and not got["reviewed_by"]
    note = [e for e in got["evidence"] if e["kind"] == "note" and RULE in e["detail"]]
    assert len(note) == 1
    assert RUN in note[0]["detail"] and HEAD[:12] in note[0]["detail"]
    assert "backend-postgres (2) failed" in note[0]["detail"]
    from app.models import Item
    row = db.get(Item, item) or db.query(Item).filter(Item.key == item).one()
    db.refresh(row)
    assert row.bounce_pinned_to == builder


def test_off_leaves_the_item_signed_off(client, key, auth, proj, monkeypatch):
    _library(monkeypatch, on=False)
    item, _ = _signed_off(client, key)

    got = _ci(client, _gate(client, auth, proj), item, commit=HEAD, passed=False)

    assert got["status"] == "done"
    assert not any(RULE in (e.get("detail") or "") for e in got["evidence"])


def test_an_empty_library_leaves_the_item_signed_off(client, key, auth, proj, monkeypatch):
    monkeypatch.setattr(T, "LIBRARY", {})
    item, _ = _signed_off(client, key)

    assert _ci(client, _gate(client, auth, proj), item, commit=HEAD, passed=False)["status"] == "done"


def test_a_project_override_turns_it_on_through_the_resolution(client, key, auth, proj,
                                                              monkeypatch, db):
    from app.models import User

    _library(monkeypatch, on=False)
    T.set_project_toggle(db, proj, RULE, {"on": True}, actor=db.query(User).first(),
                         reason="send red work back")
    db.commit()
    item, _ = _signed_off(client, key)

    assert _ci(client, _gate(client, auth, proj), item, commit=HEAD, passed=False)["status"] == "next"


# ---- only a failure, only at the signed-off head ----------------------------------------

def test_a_failure_on_a_different_commit_does_nothing(client, key, auth, proj, monkeypatch):
    _library(monkeypatch, on=True)
    item, _ = _signed_off(client, key)

    got = _ci(client, _gate(client, auth, proj), item, commit=OTHER, passed=False)

    assert got["status"] == "done"


def test_a_pass_at_the_signed_off_head_does_nothing(client, key, auth, proj, monkeypatch):
    _library(monkeypatch, on=True)
    item, _ = _signed_off(client, key)

    assert _ci(client, _gate(client, auth, proj), item, commit=HEAD, passed=True)["status"] == "done"


def test_an_abbreviated_signed_head_still_matches(client, key, auth, proj, monkeypatch):
    _library(monkeypatch, on=True)
    item, _ = _signed_off(client, key)

    assert _ci(client, _gate(client, auth, proj), item, commit=HEAD[:12],
               passed=False)["status"] == "next"


# ---- the contract and the fixture -------------------------------------------------------

def test_primitive_is_published_as_enforced():
    prim = P.PRIMITIVES["reopen_on_red"]
    assert prim.level == "enforced"
    assert "reopen_on_red" in {p["id"] for p in P.contract()["primitives"]}


def test_the_fixture_template_loads():
    pack = P.load(*build(FIX / "api1"), trusted_keys=TRUST)
    assert RULE not in pack.refused
    assert pack.templates[RULE]["primitive"] == "reopen_on_red"
    assert pack.templates[RULE]["claims"] == "enforced"


def test_the_enforcer_names_the_transition():
    assert fleet.REOPEN_ON_RED_RULE == RULE
