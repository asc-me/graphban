"""A review claim is a hold, and the roster has to say so (GRPH-1001).

`list_agents` built each row's `holdings` by grouping items on `claimed_by` alone. A review
claim lives in `review_claimed_by`, so every reviewer in the fleet reported `holdings: []`.
Measured on the live instance: 0 of 3044 agents reported a holding, while an item sitting in
review named its holder on the ITEM and nowhere on the roster.

The cost was not cosmetic. A builder wave spun for the better part of an hour on
`1 cluster(s) held by review; no expiry reported` — no holder, no clock — because the one
surface built to answer "who holds what" could not see that kind of hold.

Two things these tests care about. First, that the roster reports the hold AND says which
kind it is, since a lease and a review claim are cleared by different verbs. Second, that
making the hold visible did not quietly change what it MEANS: `live` attributes files from a
build lease, and a reviewer has reserved no areas.
"""
import pytest
from sqlalchemy import select

from app.models import ApiKey, Item, Project, User
from app.services import fleet as fleet_svc


@pytest.fixture()
def db(_clean_database):
    from app.db import SessionLocal

    s = SessionLocal()
    try:
        yield s
    finally:
        s.close()


@pytest.fixture()
def proj(db):
    db.add(Project(id="fleet", name="Fleet", tag="FL"))
    db.commit()
    return "fleet"


@pytest.fixture()
def key(db, proj):
    owner = User(id="u_fleet", name="Fleet Owner", handle="fleet", email="fleet@example.com",
                 initials="FO", password_hash="x")
    db.add(owner)
    db.flush()
    row = ApiKey(id="k_fleet", user_id=owner.id, project_id=proj, name="fleet-key",
                 prefix="gb_sk_ab12", hashed_key="x", scopes=["read", "write"], roles=[])
    db.add(row)
    db.commit()
    return row


_n = iter(range(1, 99))


def _item(db, proj, item_id, **fields):
    """A real persisted row, so column defaults are the ones the database actually applies."""
    it = Item(id=item_id, project_id=proj, number=next(_n),
              title=f"item {item_id}", status="review")
    for k, v in fields.items():
        setattr(it, k, v)
    db.add(it)
    db.commit()
    db.refresh(it)
    return it


def _row(db, proj, agent_id):
    rows = [a for a in fleet_svc.list_agents(db, proj) if a["id"] == agent_id]
    assert rows, f"{agent_id} is not on the roster at all — the assertion below would be vacuous"
    return rows[0]


def test_an_agent_holding_only_a_review_claim_reports_it(db, proj, key):
    """THE regression. Before GRPH-1001 this row came back `holdings: []`."""
    reviewer = fleet_svc.register_agent(db, project_id=proj, api_key=key, label="reviewer")
    it = _item(db, proj, "i_rev", review_claimed_by=reviewer.id)

    holdings = _row(db, proj, reviewer.id)["holdings"]

    assert [h["id"] for h in holdings] == [it.key], holdings
    assert holdings[0]["hold"] == "review"


def test_one_item_claimed_by_one_agent_and_reviewed_by_another_is_on_both_rows(db, proj, key):
    """The two arms are separate `if`s, not an elif. An item can be held twice over, and
    reporting it against only the builder would hide the claim that blocks the queue."""
    builder = fleet_svc.register_agent(db, project_id=proj, api_key=key, label="builder")
    reviewer = fleet_svc.register_agent(db, project_id=proj, api_key=key, label="reviewer")
    it = _item(db, proj, "i_both", claimed_by=builder.id, review_claimed_by=reviewer.id)

    on_builder = _row(db, proj, builder.id)["holdings"]
    on_reviewer = _row(db, proj, reviewer.id)["holdings"]

    assert [(h["id"], h["hold"]) for h in on_builder] == [(it.key, "build")]
    assert [(h["id"], h["hold"]) for h in on_reviewer] == [(it.key, "review")]


def test_a_build_lease_is_still_reported_and_is_labelled_build(db, proj, key):
    """The half that already worked. A fix that swapped one blindness for another would
    pass every other test in this file."""
    builder = fleet_svc.register_agent(db, project_id=proj, api_key=key, label="builder")
    it = _item(db, proj, "i_build", status="in_progress", claimed_by=builder.id)

    holdings = _row(db, proj, builder.id)["holdings"]

    assert [(h["id"], h["hold"]) for h in holdings] == [(it.key, "build")]


def test_lean_keeps_the_hold_kind(db, proj, key):
    """`lean` drops display-only fields by denylist. Telling a lease from a review claim is
    the point of the field, so it must survive the view a supervisor actually asks for."""
    reviewer = fleet_svc.register_agent(db, project_id=proj, api_key=key, label="reviewer")
    _item(db, proj, "i_lean", review_claimed_by=reviewer.id)

    payload = {"agents": fleet_svc.list_agents(db, proj)}
    lean = fleet_svc.roster_view(payload, "lean")
    holdings = [a for a in lean["agents"] if a["id"] == reviewer.id][0]["holdings"]

    assert holdings and holdings[0]["hold"] == "review"


def test_a_review_hold_does_not_make_a_reviewer_look_like_it_reserved_files(db, proj, key):
    """Visibility must not smuggle in a change of meaning. `_file_state` returns
    `unreserved` for any agent with holdings, and `unreserved` then attributes the item's
    touchpoints to it as declared files. A reviewer has reserved no areas, so a review hold
    has to stay out of that arithmetic."""
    from app.services import live as live_svc

    reviewer = fleet_svc.register_agent(db, project_id=proj, api_key=key, label="reviewer")
    _item(db, proj, "i_files", review_claimed_by=reviewer.id,
          touchpoints=["backend/app/services/fleet.py"])

    board = live_svc.board(db, proj)
    agents = [a for g in board["users"] for a in g.get("agents", [])]
    assert agents, "Live returned no agents — every assertion below would be vacuous"
    row = [a for a in agents if a["id"] == reviewer.id][0]

    assert row["file_state"] != "unreserved", row["file_state"]
    assert not [f for f in row["files"] if f.get("kind") == "declared"], row["files"]
    # …and the hold is still ON the row. Suppressing it entirely would pass the two
    # assertions above and reinstate the bug this file exists for.
    assert [h["hold"] for h in row["holdings"]] == ["review"]


def test_an_agent_holding_nothing_reports_nothing(db, proj, key):
    """The control. Every assertion above reads a non-empty list, so an implementation that
    reported every item for every agent would satisfy them."""
    idle = fleet_svc.register_agent(db, project_id=proj, api_key=key, label="idle")
    other = fleet_svc.register_agent(db, project_id=proj, api_key=key, label="other")
    _item(db, proj, "i_other", review_claimed_by=other.id)

    assert _row(db, proj, idle.id)["holdings"] == []
