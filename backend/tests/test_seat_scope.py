"""A seat carries the wave's scope, and a child cannot claim past it (GRPH-827).

Reported from a real wave: `--prd SA-P11` was given, the supervisor delegated three items and
every one of them was inside the PRD. Then the children finished, did what every worker posture
tells them to do — call `claim_cluster` when there is nothing to review — and took six more
items, none of them in the PRD. One was an ops item whose checklist mutates production and
which sits top of the queue on score. The worker declined that one on its own judgment, which
is the best possible outcome of a mechanism that offered it at all.

The flag bounded the SUPERVISOR. This bounds the CREDENTIAL, which is the only half the child
cannot decline to honour.

Two things every test here is really checking:

- the gate is on the two writes that hand an item to a holder (`claim_item`, `claim_next`),
  so the paths above them inherit it rather than each restating it;
- a refusal and an empty result stay different answers, because "nothing left in your scope"
  and "that item is not yours to take" call for different next moves.
"""
from __future__ import annotations

import pytest
from sqlalchemy import select

from app.models import Enrolment, Item
from app.services import clustering as cluster_svc
from app.services import fleet as fleet_svc
from app.services import items as items_svc


def _mcp(client, key, name, args=None):
    r = client.post(
        "/api/mcp",
        json={"jsonrpc": "2.0", "id": 1, "method": "tools/call",
              "params": {"name": name, "arguments": args or {}}},
        headers={"X-API-Key": key},
    )
    assert r.status_code == 200, r.text
    return r.json()["result"]


def _ok(res) -> dict:
    assert not res.get("isError"), res
    return res["structuredContent"]


def _err(res) -> dict:
    assert res.get("isError"), res
    return res["structuredContent"]["error"]


@pytest.fixture()
def proj(client, auth):
    return client.post("/api/projects", json={"name": "Scoped"}, headers=auth).json()["id"]


@pytest.fixture()
def key(client, auth, proj):
    return client.post("/api/api-keys", json={"name": "shared", "project_id": proj,
                                              "scopes": ["read", "write", "gate"]},
                       headers=auth).json()["plaintext"]


@pytest.fixture()
def db(_clean_database):
    from app.db import SessionLocal

    s = SessionLocal()
    try:
        yield s
    finally:
        s.close()


def _prd(client, auth, proj, title):
    r = client.post("/api/prds", json={"title": title, "project_id": proj}, headers=auth)
    assert r.status_code in (200, 201), r.text
    return r.json()


def _item(client, key, title, touchpoints, prd_id=None):
    args = {"title": title, "status": "next", "touchpoints": touchpoints}
    if prd_id:
        args["prd_id"] = prd_id
    return _ok(_mcp(client, key, "create_item", args))["id"]


def _agent(client, key, label, **kw) -> str:
    return _ok(_mcp(client, key, "register_agent", {"label": label, **kw}))["agent_id"]


def _scoped_worker(client, auth, key, db, proj, planner, scope) -> str:
    """A worker registered on a seat minted for `scope` — the credential under test."""
    code = _ok(_mcp(client, key, "mint_enrolment",
                    {"agent_id": planner, "role": "worker", "wave": "w", "scope": scope}))
    return _agent(client, key, "child", enrolment_code=code["enrolment_code"])


@pytest.fixture()
def planner(client, auth, key, proj, db):
    """A planner that may mint. Seats are minted by a planner credential, so the fixture
    registers on one issued for the project rather than promoting a worker."""
    code = client.post("/api/fleet/seats",
                       json={"project_id": proj, "roles": ["planner"], "wave": "w"},
                       headers=auth).json()["seats"][0]["code"]
    return _agent(client, key, "planner", enrolment_code=code)


def _stored(db, item_key) -> Item:
    from app.services import keys

    db.expire_all()
    return db.get(Item, keys.resolve_item(db, item_key) or item_key)


# ---- the reported escape ---------------------------------------------------------------------

def test_a_scoped_seat_cannot_claim_a_cluster_outside_its_scope(
        client, auth, key, db, proj, planner):
    """THE ONE THAT MATTERS. The measured wave, in miniature: work inside the scope is gone,
    other work is ready, and the child asks for the next cluster.

    Sabotage: drop the `prd_id=scope or None` argument in `claim_cluster` and this passes an
    item from the other PRD straight back."""
    mine = _prd(client, auth, proj, "in scope")
    _prd(client, auth, proj, "somewhere else")
    _item(client, key, "ops: rotate production keys", ["ops/deploy.sh"])

    child = _scoped_worker(client, auth, key, db, proj, planner, mine["id"])
    got = fleet_svc.claim_cluster(db, agent_id=child, project_id=proj)

    assert got["claimed"] is False, got
    assert got["items"] == []


def test_the_refusal_names_the_scope_rather_than_saying_nothing_is_ready(
        client, auth, key, db, proj, planner):
    """"Nothing ready to claim" is what an unscoped agent hears when the project is empty, and
    it means STOP. A scoped agent hearing it about a project full of work would report the
    fleet as drained. The two states stay distinguishable."""
    mine = _prd(client, auth, proj, "in scope")
    _item(client, key, "other work", ["a/b.py"])

    child = _scoped_worker(client, auth, key, db, proj, planner, mine["id"])
    got = fleet_svc.claim_cluster(db, agent_id=child, project_id=proj)

    assert got["scope"] == mine["id"]
    assert mine["id"] in got["reason"]
    assert got["reason"] != "nothing ready to claim"


def test_a_scoped_seat_still_claims_the_work_it_was_provisioned_for(
        client, auth, key, db, proj, planner):
    """The control. A scope that also blocks the wave's own work is a broken wave, not a safe
    one — and it is the failure an unresolvable scope would produce silently."""
    mine = _prd(client, auth, proj, "in scope")
    inside = _item(client, key, "the actual work", ["svc/thing.py"], prd_id=mine["id"])
    _item(client, key, "unrelated", ["ops/deploy.sh"])

    child = _scoped_worker(client, auth, key, db, proj, planner, mine["id"])
    got = fleet_svc.claim_cluster(db, agent_id=child, project_id=proj)

    assert got["claimed"] is True, got
    assert [i["id"] for i in got["items"]] == [inside]


def test_an_unscoped_seat_behaves_exactly_as_it_did(client, auth, key, db, proj, planner):
    """The other control, and the one that decides whether this can ship: every seat minted
    before this column existed reads NULL, and must keep claiming anything."""
    _prd(client, auth, proj, "some prd")
    _item(client, key, "unrelated", ["ops/deploy.sh"])

    child = _scoped_worker(client, auth, key, db, proj, planner, None)
    got = fleet_svc.claim_cluster(db, agent_id=child, project_id=proj)

    assert got["claimed"] is True, got
    assert got["scope"] == ""


# ---- the gate is on the two writes, not on the tools --------------------------------------

def test_claim_next_skips_what_is_out_of_scope_instead_of_stopping_at_it(
        client, auth, key, db, proj, planner):
    """`claim_next` walks a SCORED queue. If the out-of-scope item at the head stopped the
    sweep rather than being skipped, a scoped agent could reach nothing behind it — which is
    the GRPH-429 failure, re-created by a new filter."""
    mine = _prd(client, auth, proj, "in scope")
    _item(client, key, "top of the queue, elsewhere", ["ops/deploy.sh"])
    inside = _item(client, key, "mine", ["svc/thing.py"], prd_id=mine["id"])

    child = _scoped_worker(client, auth, key, db, proj, planner, mine["id"])
    got = items_svc.claim_next(db, child, project_id=proj)

    assert got is not None and got.key == inside


def test_naming_an_out_of_scope_item_is_refused_not_reported_as_a_lost_race(
        client, auth, key, db, proj, planner):
    """`claim_item` returns None for "somebody beat you to it", and the right response to that
    is to try the next candidate. Out of scope is the opposite instruction — no candidate will
    work — so it raises. Flattening the two is the defect class this repository keeps finding,
    and it would show up here as a caller that walks the whole project claiming nothing."""
    mine = _prd(client, auth, proj, "in scope")
    outside = _item(client, key, "not yours", ["ops/deploy.sh"])

    child = _scoped_worker(client, auth, key, db, proj, planner, mine["id"])
    with pytest.raises(items_svc.OutOfScope) as exc:
        items_svc.claim_item(db, _stored(db, outside).id, child)

    assert mine["id"] in str(exc.value)


def test_an_item_with_no_prd_says_so_because_no_scoped_wave_can_reach_it(
        client, auth, key, db, proj, planner):
    """Most bug reports carry no PRD. A scoped wave cannot reach them AT ALL, and the refusal
    says which of the two situations this is rather than leaving the reader to check."""
    mine = _prd(client, auth, proj, "in scope")
    orphan = _item(client, key, "a windows bug nobody filed a PRD for", ["src/win.rs"])

    child = _scoped_worker(client, auth, key, db, proj, planner, mine["id"])
    with pytest.raises(items_svc.OutOfScope) as exc:
        items_svc.claim_item(db, _stored(db, orphan).id, child)

    assert "carries no PRD" in str(exc.value)


def test_a_cluster_neighbour_outside_the_scope_is_skipped_and_the_seed_still_lands(
        client, auth, key, db, proj, planner):
    """`next_cluster` claims a seed through `claim_next` and its neighbours through
    `claim_item`. The neighbour path raises now, so an unhandled refusal would lose the seed
    as well — the work the child was provisioned for, dropped by the guard protecting it."""
    mine = _prd(client, auth, proj, "in scope")
    seed = _item(client, key, "seed", ["svc/pkg/a.py"], prd_id=mine["id"])
    _item(client, key, "neighbour, other prd", ["svc/pkg/b.py"])

    child = _scoped_worker(client, auth, key, db, proj, planner, mine["id"])
    batch = cluster_svc.next_cluster(db, child, project_id=proj)

    assert [b["item"].key for b in batch] == [seed]


def test_review_is_scoped_too_because_the_reviewer_is_the_half_that_signs_off(
        client, auth, key, db, proj, planner):
    """A scoped wave whose children review another PRD's work is the same escape with the
    reviewer's hat on, and review is the role that can also mark work done."""
    mine = _prd(client, auth, proj, "in scope")
    other = _item(client, key, "someone else's work", ["ops/deploy.sh"])
    builder = _agent(client, key, "builder")
    row = _stored(db, other)
    row.status, row.built_by = "review", builder
    db.commit()

    child = _scoped_worker(client, auth, key, db, proj, planner, mine["id"])
    assert fleet_svc.claim_review(db, agent_id=child, project_id=proj) is None


# ---- minting the scope -----------------------------------------------------------------------

def test_an_unknown_scope_is_refused_rather_than_dropped(client, auth, key, db, proj, planner):
    """A dropped scope is the whole finding one layer down: the operator sets it, nothing
    applies it, and the wave reports as scoped. A typo has to fail loudly."""
    e = _err(_mcp(client, key, "mint_enrolment",
                  {"agent_id": planner, "role": "worker", "scope": "NOPE-P9"}))
    assert e["code"] == "validation", e
    assert "NOPE-P9" in e["message"]


def test_a_scope_given_as_a_key_is_stored_as_the_frozen_id(client, auth, key, db, proj, planner):
    """Agents quote KEYS; the column holds a frozen id, and the two diverge permanently once a
    project is retagged — which this project has been. A seat storing the key would match no
    item at all, so the child would hold a credential that can claim nothing while its wave
    reported as scoped. Same class as GRPH-319, on a new column."""
    prd = _prd(client, auth, proj, "in scope")
    frozen = prd["id"]
    from app.models import Prd, Project

    # A real retag, not a rewritten primary key: the project's tag changes, every key
    # re-renders, and the stored ids stay exactly as they were. That is the divergence.
    db.get(Project, proj).tag = "RTAG"
    db.commit()
    db.expire_all()
    rendered = db.get(Prd, frozen).key
    assert rendered != frozen, "the retag did not change the rendering; test proves nothing"

    _ok(_mcp(client, key, "mint_enrolment",
             {"agent_id": planner, "role": "worker", "scope": rendered}))
    seat = db.scalars(select(Enrolment).order_by(Enrolment.created_at.desc())).first()

    assert seat.prd_id == frozen


def test_delegate_puts_the_scope_on_the_bound_seat(client, auth, key, db, proj, planner):
    """The path `until` actually takes. Sabotage: drop `prd_id=scope or None` from the mint
    inside `delegate` — the delegation still succeeds and the seat comes back unscoped."""
    mine = _prd(client, auth, proj, "in scope")
    item = _item(client, key, "the work", ["svc/thing.py"], prd_id=mine["id"])

    _ok(_mcp(client, key, "delegate", {"id": item, "lane": "backend", "tier": "cheap",
                                       "agent_id": planner, "seat": True,
                                       "scope": mine["id"]}))
    db.expire_all()
    seat = db.scalars(select(Enrolment).order_by(Enrolment.created_at.desc())).first()

    assert seat.prd_id == mine["id"]


def test_a_bound_seat_pointed_outside_its_own_scope_reports_rather_than_raises(
        client, auth, key, db, proj, planner):
    """A planner asking for a contradiction — this seat may only work PRD A, and its bound item
    is in PRD B — is found by the CHILD, after registration has already committed. An exception
    there hands a registered agent a 500 instead of a sentence, and PRD-36 D4 says a refused
    bound claim is reported as a state."""
    mine = _prd(client, auth, proj, "in scope")
    elsewhere = _item(client, key, "other prd's work", ["ops/deploy.sh"])
    seat, seat_code = fleet_svc.issue_enrolment(
        db, project_id=proj, role="worker", wave="w", minted_by=planner,
        item_id=_stored(db, elsewhere).id, prd_id=mine["id"])

    out = _ok(_mcp(client, key, "register_agent",
                   {"label": "child", "enrolment_code": seat_code}))
    assert out["assigned"]["state"] == "taken"
    assert out["assigned"]["reason"] == "out-of-scope"
    assert seat.prd_id == mine["id"]
