"""D9 — the adversarial evidence gate (GRPH-341 / PRD-17).

**Accept:** `sign_off` on an above-threshold item without adversarial evidence is refused,
naming what is missing. A reviewer that dispatches two opposing-lens critics and records their
receipts passes. A below-threshold item signs off without one. The refusal is in the ledger.

The argument for a precondition rather than a practice: **reviewer and adversary are different
jobs and must not become one habit.** A reviewer CONVERGES — the queue is three deep, and an
agent that blocks everything is a bad reviewer. An adversary DIVERGES — the job is one more
failure mode, and finding nothing is failure. Merge them and the convergent incentive wins
under queue pressure, which is the audit pack's self-congratulation problem moved one seat
over.

The threshold is not a convenience. `a gate nobody satisfies is a gate people route around` is
what kept GRPH-321 parked, and firing on a one-line fix is exactly how a gate earns that. Below
the threshold, agent-distinct review is sufficient on its own.
"""
import pytest

from app.models import Event, Item
from app.services import fleet


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
    return client.post("/api/projects", json={"name": "Adversarial"},
                       headers=auth).json()["id"]


@pytest.fixture()
def key(client, auth, proj):
    return client.post("/api/api-keys", json={"name": "adv", "project_id": proj},
                       headers=auth).json()["plaintext"]


SABOTAGE = {"kind": "sabotage", "claim": "the veto holds back an accept",
            "mutation": "return True from may_auto_publish", "tests_failed": 2}


def _ready_for_review(client, key, effort):
    """An item built by one agent and waiting for another — the state a reviewer acts on."""
    worker = _ok(client, key, "register_agent", {"label": "w"})
    made = _ok(client, key, "create_item",
               {"title": "some work", "status": "next", "effort": effort})
    c = _ok(client, key, "claim_next", {"agent_id": worker["agent_id"]})
    _ok(client, key, "update_item",
        {"id": c["item"]["id"], "status": "review", "agent_id": worker["agent_id"]})
    reviewer = _ok(client, key, "register_agent", {"label": "r", "role_hint": "reviewer"})
    return c["item"]["id"], reviewer


# ---- the gate ---------------------------------------------------------------------------------

def test_substantial_work_cannot_be_signed_off_unchallenged(client, key, db):
    """THE criterion. Without it the reviewer role is a second opinion and nothing more — and
    a second opinion under queue pressure converges on yes."""
    item, reviewer = _ready_for_review(client, key, effort=5)

    res = _rpc(client, key, "sign_off", {"id": item, "agent_id": reviewer["agent_id"]})

    err = res["structuredContent"]["error"]
    assert err["code"] == "conflict", "permitted, just not accounted for"
    assert "sabotage" in err["message"] and "tests_failed" in err["message"]
    assert err["hint"], "and it says how to satisfy it"
    assert _ok(client, key, "get_item_details", {"id": item})["status"] == "review"


def test_a_sabotage_receipt_lets_it_through(client, key):
    item, reviewer = _ready_for_review(client, key, effort=5)

    out = _ok(client, key, "sign_off",
              {"id": item, "agent_id": reviewer["agent_id"], "evidence": [SABOTAGE]})

    assert out["status"] == "done"


def test_two_opposing_lenses_are_two_receipts(client, key):
    """The shape the PRD describes: subagents with opposing lenses, each recording its own
    receipt. Adversarial multiplicity does not need a fourth fleet role competing for the
    human's attention."""
    item, reviewer = _ready_for_review(client, key, effort=8)

    out = _ok(client, key, "sign_off", {
        "id": item, "agent_id": reviewer["agent_id"],
        "evidence": [
            dict(SABOTAGE, claim="the reservation blocks a colliding claim"),
            dict(SABOTAGE, claim="the pin lapses", mutation="never expire", tests_failed=1),
        ],
    })

    assert out["status"] == "done"


def test_trivial_work_is_not_taxed(client, key):
    """A gate that fires on a one-line fix is a gate people route around — the AL-96 trust
    failure, and the objection that kept GRPH-321 parked. The threshold answers it directly:
    the cheapest way to satisfy this gate is never to avoid it."""
    item, reviewer = _ready_for_review(client, key, effort=1)

    out = _ok(client, key, "sign_off", {"id": item, "agent_id": reviewer["agent_id"]})

    assert out["status"] == "done"


def test_a_sabotage_that_broke_nothing_does_not_satisfy_it(client, key):
    """The distinction the whole receipt exists for. `tests_failed: 0` means the TEST cannot
    fail — evidence the guard is absent. Accepting it would let precisely the condition the
    gate detects satisfy the gate."""
    item, reviewer = _ready_for_review(client, key, effort=5)

    res = _rpc(client, key, "sign_off", {
        "id": item, "agent_id": reviewer["agent_id"],
        "evidence": [dict(SABOTAGE, tests_failed=0)]})

    err = res["structuredContent"]["error"]
    assert err["code"] == "conflict"
    assert "broke NOTHING" in err["message"], "and it says which way it failed"


def test_prose_claiming_a_sabotage_does_not_satisfy_it(client, key):
    """If a `note` saying "I sabotaged it" passed, the structure would be decorative and the
    gate would be checking a sentence."""
    item, reviewer = _ready_for_review(client, key, effort=5)

    res = _rpc(client, key, "sign_off", {
        "id": item, "agent_id": reviewer["agent_id"],
        "evidence": [{"kind": "note", "detail": "ran six sabotages, all caught"}]})

    assert res["structuredContent"]["error"]["code"] == "conflict"


def test_the_workers_own_receipts_count(client, key, db):
    """Adversarial evidence is adversarial evidence whoever recorded it. Requiring the reviewer
    to re-run sabotages the author already recorded would be tax rather than rigour — and the
    author is better placed to break their own claim."""
    item, reviewer = _ready_for_review(client, key, effort=5)
    _ok(client, key, "update_item", {"id": item, "evidence": [SABOTAGE]})

    out = _ok(client, key, "sign_off", {"id": item, "agent_id": reviewer["agent_id"]})

    assert out["status"] == "done"


def test_the_refusal_is_in_the_ledger(client, key, db):
    """A gate whose refusals leave no trace cannot be audited for whether it is being routed
    around — which is the exact failure mode it was parked over."""
    item, reviewer = _ready_for_review(client, key, effort=5)

    _rpc(client, key, "sign_off", {"id": item, "agent_id": reviewer["agent_id"]})

    ev = db.query(Event).filter(
        Event.action == "sign_off_refused").order_by(Event.id.desc()).first()
    assert ev is not None, "the refusal left no trace"
    assert "sabotage" in ev.meta["reason"]
    assert ev.meta["principal"]["id"], "and names the human behind the key"


def test_the_threshold_is_a_named_constant(client, key):
    """Pick one number and let somebody hit it, rather than a per-project slider nobody tunes
    and everybody sets to infinity the first time it is inconvenient."""
    assert fleet.ADVERSARIAL_EFFORT_THRESHOLD == 3
    assert fleet.needs_adversarial_evidence(Item(effort=3)) is True
    assert fleet.needs_adversarial_evidence(Item(effort=2)) is False
    assert fleet.needs_adversarial_evidence(Item(effort=None)) is False
