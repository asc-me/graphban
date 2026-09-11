"""An item sent to review without a claim has an author, and independence from nobody is not
a pass (GRPH-848).

`built_by` was written in exactly one place: the claim. An item never claimed and moved to
`review` through `update_item` carried no author, `independent(reviewer, None)` answered True
with the comment "human-authored", and `_independent_of_author` for an unregistered caller
answered True as soon as `author is None`.

Measured 2026-09-10 on the deployed instance: of the 20 most recently signed-off items, 17 had
`built_by` empty, every one carried a `fleet.sign_off` attestation reading "independent of the
author", and every one was reviewed by a registered agent. They were built by sessions that did
the work directly and sent the item to review without claiming. The gate did not decide those
reviews were independent; it had nobody to compare against.

GRPH-435 closed the claim-and-release version of this. This is the remaining door, and it is
the ORDINARY path for a planner or a human-key session that builds inline — so the tests here
walk through the tool, not the service, and the load-bearing one is the flow that passed
before this change: one credential sends an item to review unclaimed, then signs it off as a
registered agent on that same credential.
"""
from __future__ import annotations

import pytest

from app.models import ApiKey, Item
from app.services import fleet
from app.services import keys as keys_svc
from tests.test_fleet_review import (  # noqa: F401
    _new_item, _ok, _refused, _register, _rpc, agent_key, db, proj)


def _row(db, item_key: str) -> Item:
    return db.get(Item, keys_svc.resolve_item(db, item_key))


def _sent_unclaimed(client, key, title="inline work", agent_id=None):
    """The defect's path: created, never claimed, pushed to `review` by whoever is calling."""
    item_id = _new_item(client, key, title)["id"]
    args = {"id": item_id, "status": "review"}
    if agent_id:
        args["agent_id"] = agent_id
    _ok(client, key, "update_item", args)
    return item_id


@pytest.fixture()
def other_key(client, auth, proj):
    """A SECOND credential on the same project — the control every refusal here needs."""
    return client.post("/api/api-keys", json={"name": "other", "project_id": proj},
                       headers=auth).json()["plaintext"]


def _attestation(client, key, item_id):
    ev = _ok(client, key, "get_item_details", {"id": item_id})["evidence"]
    att = [e for e in ev if e.get("kind") == "attestation" and e.get("adapter") == "fleet.sign_off"]
    assert len(att) == 1, ev
    return next(p for p in att[0]["predicates"] if p["name"] == "independent_review")


# ---- the stamp ------------------------------------------------------------------------------

def test_an_unclaimed_item_sent_to_review_is_stamped_with_the_credential(client, agent_key, db):
    item_id = _sent_unclaimed(client, agent_key)
    assert _row(db, item_id).built_by == "key:fleet", \
        "the bare caller is the author, marked as a credential the way reviewed_by already is"


def test_a_call_that_carries_an_agent_id_stamps_the_agent(client, agent_key, db):
    me = _register(client, agent_key, "worker")
    item_id = _sent_unclaimed(client, agent_key, agent_id=me["agent_id"])
    assert _row(db, item_id).built_by == me["agent_id"]


def test_an_existing_author_survives_a_bounce_and_resubmit(client, agent_key, other_key, db):
    """NEVER overwritten. A bounce clears the lease and not the authorship; the resubmit —
    here by the bare credential, which is how a supervisor or the same session comes back —
    must keep the real author, or the second review would be independent of the wrong one."""
    a = _register(client, agent_key, "worker", label="A")
    _new_item(client, agent_key, "A's work")
    c = _ok(client, agent_key, "claim_next", {"agent_id": a["agent_id"]})
    item_id = c["item"]["id"]
    _ok(client, agent_key, "update_item",
        {"id": item_id, "status": "review", "agent_id": a["agent_id"]})
    b = _register(client, other_key, "worker", label="B")
    _ok(client, other_key, "bounce", {"id": item_id, "agent_id": b["agent_id"],
                                      "reason": "one more test"})
    assert _row(db, item_id).status == "next"

    _ok(client, agent_key, "update_item", {"id": item_id, "status": "review"})

    db.expire_all()
    assert _row(db, item_id).built_by == a["agent_id"], "the bounce-and-resubmit keeps its author"


def test_a_re_save_of_an_item_already_in_review_does_not_stamp(client, agent_key, db):
    """On the TRANSITION only. A row that predates the stamp sits in review with no author;
    the next caller to touch it is not thereby its builder."""
    item_id = _ok(client, agent_key, "create_item", {"title": "old row", "status": "review"})["id"]
    assert _row(db, item_id).built_by is None
    _ok(client, agent_key, "update_item", {"id": item_id, "status": "review", "title": "old row, retitled"})
    db.expire_all()
    assert _row(db, item_id).built_by is None


# ---- the gate: the flow that used to pass ---------------------------------------------------

def test_the_credential_cannot_sign_off_what_it_sent_through_an_agent_on_the_same_key(
        client, agent_key, db):
    """THE defect. This exact sequence returned `done` before GRPH-848: the item had no
    author, so the registered reviewer was independent of nobody. Remove the stamp in
    `update_item` and this passes again — that is the sabotage the item names."""
    item_id = _sent_unclaimed(client, agent_key)
    me = _register(client, agent_key, "worker", label="same key, registered later")

    out = _ok(client, agent_key, "claim_review", {"agent_id": me["agent_id"]})
    assert out["claimed"] is False, "claim_review applies the same rule, so it is never leased"

    err = _refused(client, agent_key, "sign_off",
                   {"id": item_id, "agent_id": me["agent_id"],
                    "evidence": [{"kind": "note", "detail": "looks fine"}]})
    assert "key:fleet" in err["message"], err
    db.expire_all()
    assert _row(db, item_id).status == "review"


def test_the_bare_credential_cannot_sign_off_what_it_sent(client, agent_key, db):
    """The same session, still unidentified: both sides of `built_by == caller` are the
    credential, which is the property the `key:` mark exists to make hold (GRPH-437)."""
    item_id = _sent_unclaimed(client, agent_key)
    err = _refused(client, agent_key, "sign_off",
                   {"id": item_id, "evidence": [{"kind": "note", "detail": "looks fine"}]})
    assert "key:fleet" in err["message"], err


def test_a_reviewer_on_a_second_credential_still_signs_off(client, agent_key, other_key, db):
    """The control. A different key is a different operator, exactly as before."""
    item_id = _sent_unclaimed(client, agent_key)
    b = _register(client, other_key, "worker", label="B")

    out = _ok(client, other_key, "claim_review", {"agent_id": b["agent_id"]})
    assert out["claimed"] is True and out["item"]["id"] == item_id

    signed = _ok(client, other_key, "sign_off",
                 {"id": item_id, "agent_id": b["agent_id"], "commit": "abc1234",
                  "evidence": [{"kind": "note", "detail": "read the diff"}]})
    assert signed["status"] == "done"
    row = _row(db, item_id)
    assert row.built_by == "key:fleet" and row.reviewed_by == b["agent_id"]
    assert "independent of key:fleet" in _attestation(client, other_key, item_id)["detail"]


def test_a_seated_reviewer_on_the_same_credential_is_independent(client, agent_key, proj, db):
    """A seat is the one identity an agent cannot assert for itself: the server issued it and
    the agent redeemed it single-use (PRD-19). The bare credential that sent the item holds no
    seat, so the reviewer's seat is what separates them."""
    item_id = _sent_unclaimed(client, agent_key)
    _, code = fleet.issue_enrolment(db, project_id=proj, role="worker")
    db.commit()
    seated = _ok(client, agent_key, "register_agent",
                 {"label": "seated", "enrolment_code": code})
    assert seated["enrolled"] is True

    out = _ok(client, agent_key, "claim_review", {"agent_id": seated["agent_id"]})
    assert out["claimed"] is True and out["item"]["id"] == item_id
    signed = _ok(client, agent_key, "sign_off",
                 {"id": item_id, "agent_id": seated["agent_id"],
                  "evidence": [{"kind": "note", "detail": "read the diff"}]})
    assert signed["status"] == "done"


def test_a_bare_second_credential_is_independent_of_the_first(client, agent_key, other_key, db):
    """Two people, two keys, neither registered: reviewable, because the strings differ AND
    the ids behind them differ."""
    item_id = _sent_unclaimed(client, agent_key)
    signed = _ok(client, other_key, "sign_off",
                 {"id": item_id, "evidence": [{"kind": "note", "detail": "read the diff"}]})
    assert signed["status"] == "done"
    assert _row(db, item_id).reviewed_by == "key:other"


# ---- the receipt ----------------------------------------------------------------------------

def test_the_receipt_says_author_unrecorded_when_there_is_none(client, agent_key, db):
    """Rows written before the stamp stay empty (backfill is out of scope), and the receipt on
    them must stop claiming a comparison that was not made."""
    item_id = _ok(client, agent_key, "create_item", {"title": "old row", "status": "review"})["id"]
    assert _row(db, item_id).built_by is None
    me = _register(client, agent_key, "worker")
    _ok(client, agent_key, "sign_off",
        {"id": item_id, "agent_id": me["agent_id"], "commit": "abc1234",
         "evidence": [{"kind": "note", "detail": "read the diff"}]})

    pred = _attestation(client, agent_key, item_id)
    assert pred["passed"] is True, "the gate still opens; the wording is what changes"
    assert "author unrecorded" in pred["detail"], pred
    assert "independent of" not in pred["detail"], pred


# ---- the resolution -------------------------------------------------------------------------

def test_credential_key_ids_matches_name_and_id_and_every_collision(client, auth, proj, db):
    """`caller_identity` stamps the NAME, names are not unique, and a nameless key stamps its
    id — so all three have to resolve, and a collision resolves to every key that wears it."""
    for name in ("twin", "twin"):
        client.post("/api/api-keys", json={"name": name, "project_id": proj}, headers=auth)
    twins = {k.id for k in db.query(ApiKey).filter(ApiKey.name == "twin").all()}
    assert len(twins) == 2
    assert fleet.credential_key_ids(db, "key:twin") == twins
    one = next(iter(twins))
    assert fleet.credential_key_ids(db, f"key:{one}") == {one}
    assert fleet.credential_key_ids(db, "GRPH-A1") == set(), "an agent id is not a credential"
    assert fleet.credential_key_ids(db, "key:nobody") == set()
