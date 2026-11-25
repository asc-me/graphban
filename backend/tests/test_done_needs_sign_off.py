"""GRPH-1940: `done` needs an independent sign-off unless the project's rules allow it.

Observed on the P74 walk: a planner key with no fleet on it moved an item from `review` to
`done` with every done predicate attested and nobody signing it off, while the same call on a
fleet key was refused. Whether review was skipped depended on the key, not on a rule. The rule
is `review.sign_off_before_done` (on = sign-off required); absent from the pack, it reads on.

Every test here is marked `sign_off_gate`, so the suite-wide fixture that runs other tests as a
project with the rule off does not apply.
"""
from __future__ import annotations

import pytest

from app.models import ApiKey
from app.services import fleet as fleet_svc
from app.services import items as items_svc
from app.services import rule_toggles as T
from app.services import rules as R

pytestmark = pytest.mark.sign_off_gate

RULE = T.SIGN_OFF_BEFORE_DONE_RULE
HEAD = "7f18c3db0a1b2c3d4e5f60718293a4b5c6d7e8f9"
BASE = "0000111122223333444455556666777788889999"
MERGE = "abcdef1234567890aaaabbbbccccddddeeeeffff"
GREEN = {"kind": "attestation", "adapter": "github-checks", "commit": HEAD,
         "predicates": [{"name": "suite_green", "passed": True, "detail": "ok"}]}


@pytest.fixture()
def db(client):
    from app.db import SessionLocal
    s = SessionLocal()
    try:
        yield s
    finally:
        s.close()


@pytest.fixture()
def library(monkeypatch):
    """The pack carrying the rule's template, default on, as the pack item will ship it."""
    monkeypatch.setattr(T, "LIBRARY", {})
    T.register(T.LibraryRule(
        id=RULE, stage="review", title="Sign-off before done",
        summary="done needs an independent fleet.sign_off at the item's head.",
        enforcement="enforced", enforcer=R.Enforcer("graphban_source", "app/services/items.py"),
        default_on=True))
    return T.LIBRARY


def _attested_in_review(db):
    """In `review`, built by another agent, with every done predicate attested at its head and
    no `fleet.sign_off` — P74W-1 as it stood."""
    it = items_svc.create_item(db, project_id="core", title="P74W-1 stand-in")
    it.status = "review"
    it.built_by = "builder-agent"
    it.head_commit = HEAD
    it.evidence = items_svc.append_evidence(it.evidence, [
        {"kind": "note", "detail": f"{items_svc.CUT_FROM_MARKER}{BASE} (`gb/fixture-1`)"},
        GREEN])
    db.commit()
    db.refresh(it)
    return it


def _planner_key(client, auth, db):
    made = client.post("/api/api-keys", json={"name": "p74-planner",
                                              "scopes": ["read", "write", "gate"],
                                              "project_id": "core"},
                       headers=auth).json()
    db.get(ApiKey, made["id"]).roles = ["planner"]
    db.commit()
    return made["plaintext"]


def _rpc(client, key, name, arguments):
    return client.post("/api/mcp", json={
        "jsonrpc": "2.0", "id": 1, "method": "tools/call",
        "params": {"name": name, "arguments": arguments},
    }, headers={"X-API-Key": key}).json()


def _status(db, item_id):
    db.expire_all()
    return items_svc.get_item(db, item_id).status


def _assert_names_the_rule(text):
    assert RULE in text, text
    assert "exploratory" in text, text
    assert "owner-close" in text, text


# ---- refused by default -------------------------------------------------------------------

def test_a_planner_key_with_no_fleet_and_no_agent_id_is_refused_409(client, auth, db):
    it = _attested_in_review(db)
    key = _planner_key(client, auth, db)

    out = _rpc(client, key, "update_item", {"id": it.id, "status": "done"})

    result = out.get("result") or {}
    assert result.get("isError"), out
    err = (result.get("structuredContent") or {}).get("error") or {}
    assert err.get("code") == "conflict", err
    _assert_names_the_rule(err.get("message") or str(out))
    assert _status(db, it.id) == "review"


def test_the_same_refusal_is_a_409_over_rest(client, auth, db):
    it = _attested_in_review(db)

    r = client.patch(f"/api/items/{it.id}", json={"status": "done"}, headers=auth)

    assert r.status_code == 409, r.text
    _assert_names_the_rule(r.text)
    assert _status(db, it.id) == "review"


def test_a_project_that_never_set_the_rule_is_refused(db, library):
    """The pack carries the rule and nobody has set it: platform default, on."""
    it = _attested_in_review(db)
    assert T.resolve(db, RULE, "core")["on"] == {"value": True, "source": "platform default"}

    with pytest.raises(items_svc.MissingAttestation) as exc:
        items_svc.update_item(db, it.id, status="done")

    _assert_names_the_rule(str(exc.value))


def test_a_pack_without_the_template_reads_the_rule_as_on(db, monkeypatch):
    monkeypatch.setattr(T, "LIBRARY", {})
    assert T.sign_off_before_done(db, "core") is True
    it = _attested_in_review(db)

    with pytest.raises(items_svc.MissingAttestation):
        items_svc.update_item(db, it.id, status="done")


# ---- the paths that stay open -------------------------------------------------------------

def test_a_passing_sign_off_at_the_head_reaches_done(db):
    it = _attested_in_review(db)

    out = fleet_svc.sign_off(db, item_id=it.id, agent_id="reviewer-1", commit=HEAD)

    assert out.status == "done"
    assert out.reviewed_by == "reviewer-1"
    assert items_svc.signed_off_at(out.evidence, HEAD)


def test_a_sign_off_receipt_at_the_head_lets_update_item_close_it(db):
    """Reopened after a sign-off, then closed again at the same head: the receipt stands."""
    it = _attested_in_review(db)
    fleet_svc.sign_off(db, item_id=it.id, agent_id="reviewer-1", commit=HEAD)
    items_svc.update_item(db, it.id, status="review")

    assert items_svc.update_item(db, it.id, status="done").status == "done"


def test_a_sign_off_at_another_commit_does_not_count(db):
    it = _attested_in_review(db)
    stale = {"kind": "attestation", "adapter": items_svc.SIGN_OFF_ADAPTER, "commit": BASE,
             "predicates": [{"name": "independent_review", "passed": True, "detail": "x"}]}

    assert not items_svc.signed_off_at(items_svc.append_evidence(it.evidence, [stale]), HEAD)


def test_a_project_that_turned_the_rule_off_reaches_done_without_a_sign_off(
        client, auth, db, library):
    r = client.put(f"/api/rules/toggles/{RULE}", params={"project_id": "core"},
                   json={"on": False, "reason": "exploratory project"}, headers=auth)
    assert r.status_code == 200, r.text
    assert T.resolve(db, RULE, "core")["on"]["source"] == "project"
    it = _attested_in_review(db)

    assert items_svc.update_item(db, it.id, status="done").status == "done"


def test_owner_close_still_closes_with_no_sign_off(db):
    it = _attested_in_review(db)

    out = items_svc.owner_close(db, it.id, closer={"id": "u1", "handle": "ascme"},
                                commit=MERGE, pr_url="https://github.com/o/r/pull/1")

    assert out.status == "done"
    assert out.reviewed_by is None


def test_a_resolution_close_still_closes_with_no_sign_off(db):
    original = _attested_in_review(db)
    fleet_svc.sign_off(db, item_id=original.id, agent_id="reviewer-1", commit=HEAD)
    dup = items_svc.create_item(db, project_id="core", title="same thing again")

    out = items_svc.resolve_item(db, dup.id, kind="duplicate", of=original.id,
                                 reason="filed twice", actor="planner-1")

    assert out.status == "done"
