"""A seat whose child died before registering is still redeemable (GRPH-1040).

Measured: a review-only seat's child hit a vendor 429 (`insufficient_quota`) and exited 0
before `register_agent`. A seat is consumed AT registration and nowhere else, so nothing the
dead child did can have spent it — and nothing the supervisor records about that exit may
spend it either. The replacement child redeems the same code inside its expiry.

The other half stays exactly as it was: a seat an agent DID redeem is single-use, because two
agents sharing an enrolment cannot review each other.
"""
import pytest

from app.models import Enrolment
from app.services import fleet


def _rpc(client, key, tool, args=None):
    return client.post(
        "/api/mcp",
        json={"jsonrpc": "2.0", "id": 1, "method": "tools/call",
              "params": {"name": tool, "arguments": args or {}}},
        headers={"X-API-Key": key},
    ).json()["result"]


@pytest.fixture()
def db(_clean_database):
    from app.db import SessionLocal

    s = SessionLocal()
    try:
        yield s
    finally:
        s.close()


@pytest.fixture()
def proj(client, auth):
    return client.post("/api/projects", json={"name": "Never registered"},
                       headers=auth).json()["id"]


@pytest.fixture()
def key(client, auth, proj):
    return client.post("/api/api-keys", json={"name": "shared", "project_id": proj,
                                              "scopes": ["read", "write", "gate"]},
                       headers=auth).json()["plaintext"]


def _state(db, seat_id):
    db.expire_all()
    return fleet.enrolment_state(db.get(Enrolment, seat_id))


def _record_child_exit(db, proj, seat_id):
    """What the supervisor has once its child exits unregistered: the seat on the roster
    read (`list_enrolments`) and the child absent from `fleet_status`. Both are reads; the
    exit itself writes nothing against the seat."""
    rows = {r["id"]: r for r in fleet.list_enrolments(db, project_id=proj)}
    assert rows[seat_id]["state"] == "unused"
    assert not [a for a in fleet.list_agents(db, project_id=proj)
                if a.get("enrolment_id") == seat_id]
    db.rollback()  # end the read so the replacement's registration is not locked out


def test_a_seat_whose_child_exited_unregistered_is_redeemed_by_a_replacement(
        client, db, proj, key):
    """Sabotage: mark the seat consumed when the supervisor reads it after the exit (or at
    mint) and the replacement's `register_agent` is refused `already consumed`."""
    row, code = fleet.issue_enrolment(db, project_id=proj, role="worker", review_only=True)

    _record_child_exit(db, proj, row.id)
    assert _state(db, row.id) == "unused"
    db.rollback()

    res = _rpc(client, key, "register_agent",
               {"branch": "gb/replacement", "label": "replacement", "enrolment_code": code})
    assert not res.get("isError"), res
    me = res["structuredContent"]
    assert me["enrolled"] is True and me["review_only"] is True
    assert _state(db, row.id) == "consumed"
    db.expire_all()
    assert db.get(Enrolment, row.id).consumed_by == me["agent_id"]


def test_a_redeemed_seat_stays_single_use(client, db, proj, key):
    row, code = fleet.issue_enrolment(db, project_id=proj, role="worker")
    first = _rpc(client, key, "register_agent",
                 {"branch": "gb/a", "label": "first", "enrolment_code": code})
    assert not first.get("isError"), first

    second = _rpc(client, key, "register_agent",
                  {"branch": "gb/b", "label": "second", "enrolment_code": code})

    assert second.get("isError")
    assert "consumed" in str(second)
    db.expire_all()
    assert db.get(Enrolment, row.id).consumed_by == first["structuredContent"]["agent_id"]
