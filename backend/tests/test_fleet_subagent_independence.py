"""GRPH-361 — a subagent must not review its parent.

PRD-17 §9 claimed this held for free:

    "D3's self-review ban is keyed on agent id, and a subagent shares its parent's identity —
     so an in-session verifier subagent STRUCTURALLY CANNOT satisfy the reviewer gate."

It did not. `register_agent` mints a row per call — correctly, because "two terminals on one
key are two agents" is the bug D1 exists to fix — so a verifier subagent that registered became
a sibling with its own id and signed off its parent's work. Reproduced before the fix:
`SA-A1` built it, `SA-A2` signed it, both on one key and one host.

**Identity was the wrong lever.** Collapsing parent and child onto `(api_key_id, host)` would
undo D1: two legitimate terminals would collapse too and the server could no longer arbitrate
between them. So independence is asked as a SEPARATE question from identity, only where it
matters — at review.

Two signals, and both are needed:

- a **declared parent**, which covers a subagent honest enough to say so, including one on a
  different host;
- **same credential AND same host**, which covers the common undeclared case, and also catches
  something the original ban missed entirely — two windows of one model on one machine sharing
  one key are two agents by D1's definition and are not two opinions.

Same key on DIFFERENT hosts stays independent. Those are separate machines, and refusing there
would block a real fleet for nothing.
"""
import pytest

from app.models import Agent
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
    return client.post("/api/projects", json={"name": "Subagent"},
                       headers=auth).json()["id"]


@pytest.fixture()
def key(client, auth, proj):
    return client.post("/api/api-keys", json={"name": "orch", "project_id": proj},
                       headers=auth).json()["plaintext"]


def _built_by(client, key, agent):
    _ok(client, key, "create_item", {"title": "the parent's work", "status": "next"})
    c = _ok(client, key, "claim_next", {"agent_id": agent["agent_id"]})
    assert c["claimed"]
    _ok(client, key, "update_item",
        {"id": c["item"]["id"], "status": "review", "agent_id": agent["agent_id"]})
    return c["item"]["id"]


# ---- the reproduction ---------------------------------------------------------------------

def test_a_declared_subagent_cannot_review_its_parent(client, key, db):
    """THE case. Before this, `SA-A2` claimed and signed `SA-A1`'s item."""
    parent = _ok(client, key, "register_agent",
                 {"label": "orchestrator", "capabilities": {"vendor": "anthropic"}})
    child = _ok(client, key, "register_agent",
                {"label": "verifier subagent", "role_hint": "reviewer",
                 "parent_agent_id": parent["agent_id"],
                 "capabilities": {"vendor": "anthropic"}})
    item = _built_by(client, key, parent)

    got = _ok(client, key, "claim_review", {"agent_id": child["agent_id"]})

    assert got["claimed"] is False
    res = _rpc(client, key, "sign_off", {"id": item, "agent_id": child["agent_id"]})
    assert res.get("isError") is True, "and the second gate refuses it too"
    assert "not independent" in res["structuredContent"]["error"]["message"]


def test_an_undeclared_subagent_is_caught_by_credential_and_host(client, key, db):
    """The common case: a subagent inherits its parent's key and runs in its process, and
    nobody remembers to declare anything. Identity cannot see it; co-location can."""
    caps = {"vendor": "anthropic", "host": "macbook"}
    parent = _ok(client, key, "register_agent", {"label": "parent", "capabilities": caps})
    child = _ok(client, key, "register_agent",
                {"label": "child", "role_hint": "reviewer", "capabilities": caps})
    _built_by(client, key, parent)

    got = _ok(client, key, "claim_review", {"agent_id": child["agent_id"]})

    assert got["claimed"] is False
    # Names BOTH remedies. A refusal that explains itself but offers no way out is where an
    # operator stops — and one of the two must be reachable from whatever client they are on.
    assert "instance" in got["reason"] and "per-role credential" in got["reason"], \
        "the refusal has to say how to fix it"


def test_two_windows_of_one_model_on_one_machine_are_not_two_opinions(client, key, db):
    """Something the ORIGINAL ban missed entirely. These are two agents by D1's definition —
    correctly, for arbitration — and they are still one perspective for review."""
    caps = {"vendor": "anthropic", "host": "macbook"}
    a = _ok(client, key, "register_agent", {"label": "window 1", "capabilities": caps})
    b = _ok(client, key, "register_agent",
            {"label": "window 2", "role_hint": "reviewer", "capabilities": caps})
    assert a["agent_id"] != b["agent_id"], "still two agents — D1 is untouched"
    _built_by(client, key, a)

    assert _ok(client, key, "claim_review", {"agent_id": b["agent_id"]})["claimed"] is False


def test_siblings_under_one_parent_cannot_review_each_other(client, key, db):
    """One call tree, two children. Neither built the other's work, but neither is a second
    opinion on it either."""
    parent = _ok(client, key, "register_agent", {"label": "parent"})
    a = _ok(client, key, "register_agent",
            {"label": "child a", "parent_agent_id": parent["agent_id"]})
    b = _ok(client, key, "register_agent",
            {"label": "child b", "role_hint": "reviewer",
             "parent_agent_id": parent["agent_id"]})
    _built_by(client, key, a)

    assert _ok(client, key, "claim_review", {"agent_id": b["agent_id"]})["claimed"] is False


# ---- what must still work -------------------------------------------------------------------

def test_a_real_fleet_still_reviews_itself(client, auth, proj, db):
    """The Fleet view mints a credential per role, so the intended path is unaffected. If this
    broke, the fix would have cured the disease by killing the patient."""
    worker_key = client.post("/api/fleet/keys",
                             json={"project_id": proj, "role": "worker", "wave": "w1"},
                             headers=auth).json()["plaintext"]
    reviewer_key = client.post("/api/fleet/keys",
                               json={"project_id": proj, "role": "reviewer", "wave": "w1"},
                               headers=auth).json()["plaintext"]
    caps = {"vendor": "anthropic", "host": "macbook"}
    w = _ok(client, worker_key, "register_agent", {"label": "w", "capabilities": caps})
    r = _ok(client, reviewer_key, "register_agent", {"label": "r", "capabilities": caps})
    item = _built_by(client, worker_key, w)

    got = _ok(client, reviewer_key, "claim_review", {"agent_id": r["agent_id"]})

    assert got["claimed"] is True and got["item"]["id"] == item


def test_one_key_across_two_machines_stays_independent(client, key, db):
    """Different hosts are genuinely separate processes on separate machines. Refusing there
    would block a real fleet for no gain."""
    a = _ok(client, key, "register_agent",
            {"label": "a", "capabilities": {"vendor": "anthropic", "host": "macbook"}})
    b = _ok(client, key, "register_agent",
            {"label": "b", "role_hint": "reviewer",
             "capabilities": {"vendor": "anthropic", "host": "lan-box"}})
    _built_by(client, key, a)

    assert _ok(client, key, "claim_review", {"agent_id": b["agent_id"]})["claimed"] is True


def test_a_declared_difference_other_than_host_still_earns_independence(client, key, db):
    """One credential, no host reported, but DIFFERENT VENDORS — two different programs, which
    is a real difference and enough. The rule is "show me something that differs", and host is
    only one of the things that can.

    This test previously passed for the opposite reason: absence was read as a difference, so
    two agents declaring nothing at all could review each other. That made the honest agent
    the restricted one, since declaring a matching host was the only way to be refused."""
    a = _ok(client, key, "register_agent", {"label": "a", "capabilities": {"vendor": "x"}})
    b = _ok(client, key, "register_agent",
            {"label": "b", "role_hint": "reviewer", "capabilities": {"vendor": "y"}})
    _built_by(client, key, a)

    assert _ok(client, key, "claim_review", {"agent_id": b["agent_id"]})["claimed"] is True


def test_the_predicate_reads_as_the_rule(db):
    """Unit-level, so the rule is legible without standing a fleet up."""
    def agent(**kw):
        base = {"id": "A", "api_key_id": "k1", "capabilities": {}, "parent_agent_id": None}
        base.update(kw)
        return Agent(**base)

    assert fleet.independent(agent(id="A"), None) is True, "no author recorded"
    assert fleet.independent(agent(id="A"), agent(id="A")) is False, "the original ban"
    assert fleet.independent(agent(id="B", parent_agent_id="A"), agent(id="A")) is False
    assert fleet.independent(agent(id="A"), agent(id="B", parent_agent_id="A")) is False
    assert fleet.independent(
        agent(id="A", capabilities={"host": "h"}),
        agent(id="B", capabilities={"host": "h"})) is False, "same key, same host"
    assert fleet.independent(
        agent(id="A", api_key_id="k1", capabilities={"host": "h"}),
        agent(id="B", api_key_id="k2", capabilities={"host": "h"})) is True, "different keys"


def test_two_agents_that_declare_nothing_cannot_review_each_other(client, key, db):
    """The polarity that was backwards. On ONE credential, an agent that declares nothing is
    indistinguishable from the subagent case this gate exists for — so silence must not buy
    permission. Otherwise laundering a self-review costs exactly nothing: omit the field.

    The remedy has to be reachable, which is why the refusal names it. `instance` exists for
    clients that cannot hold two credentials at once (Cursor stores one MCP config and reuses
    it), so "declare who you are" is something an agent can always do."""
    a = _ok(client, key, "register_agent", {"label": "a"})
    b = _ok(client, key, "register_agent", {"label": "b", "role_hint": "reviewer"})
    _built_by(client, key, a)

    out = _ok(client, key, "claim_review", {"agent_id": b["agent_id"]})

    assert out["claimed"] is False


def test_an_instance_tag_is_enough_to_separate_two_agents_on_one_credential(client, key, db):
    """THE Cursor case. One config, one key, several agents — the fleet posture is unavailable
    there unless an agent can say which one it is. Self-reported, and worth being plain about:
    it buys coordination, not an adversarial boundary. An agent that wants to review its own
    work can claim a different instance; what this stops is the accident."""
    a = _ok(client, key, "register_agent",
            {"label": "a", "capabilities": {"instance": "cursor-worker-1"}})
    b = _ok(client, key, "register_agent",
            {"label": "b", "role_hint": "reviewer",
             "capabilities": {"instance": "cursor-reviewer-1"}})
    _built_by(client, key, a)

    assert _ok(client, key, "claim_review", {"agent_id": b["agent_id"]})["claimed"] is True


def test_the_same_instance_tag_is_not_two_opinions(client, key, db):
    """A tag that never varies is a tag that means nothing — and copy-pasting one prompt into
    four Cursor chats is the likeliest way to end up here."""
    a = _ok(client, key, "register_agent",
            {"label": "a", "capabilities": {"instance": "cursor"}})
    b = _ok(client, key, "register_agent",
            {"label": "b", "role_hint": "reviewer", "capabilities": {"instance": "cursor"}})
    _built_by(client, key, a)

    assert _ok(client, key, "claim_review", {"agent_id": b["agent_id"]})["claimed"] is False


def test_declaring_nothing_does_not_make_you_different_from_someone_who_did(client, key, db):
    """The ASYMMETRIC case, and the sharp edge of the original loophole. One agent honestly
    reports its host; the other reports nothing. If absence counted as "different" the silent
    agent would be the one permitted to review — the honest declaration is what would have
    refused it.

    Missing this left a sabotage undetected: flipping the comparison to a bare `a != b` keeps
    both-absent behaving correctly (None != None is false) and only breaks THIS case, so the
    two-agents-declare-nothing test alone cannot see it."""
    a = _ok(client, key, "register_agent",
            {"label": "honest", "capabilities": {"host": "macbook"}})
    b = _ok(client, key, "register_agent",
            {"label": "silent", "role_hint": "reviewer", "capabilities": {}})
    _built_by(client, key, a)

    assert _ok(client, key, "claim_review", {"agent_id": b["agent_id"]})["claimed"] is False


def test_the_predicate_never_treats_absence_as_a_difference(db):
    """Unit-level and exhaustive over the shapes, because the loop reads as if it compares
    values when what it really encodes is "a difference must be DECLARED on both sides"."""
    def agent(**kw):
        base = {"id": "A", "api_key_id": "k1", "capabilities": {}, "parent_agent_id": None}
        base.update(kw)
        return Agent(**base)

    both_absent = (agent(id="A"), agent(id="B"))
    one_absent = (agent(id="A", capabilities={"host": "h"}), agent(id="B"))
    absent_other_way = (agent(id="A"), agent(id="B", capabilities={"host": "h"}))
    both_same = (agent(id="A", capabilities={"host": "h"}),
                 agent(id="B", capabilities={"host": "h"}))
    genuinely_different = (agent(id="A", capabilities={"host": "h1"}),
                           agent(id="B", capabilities={"host": "h2"}))

    for pair in (both_absent, one_absent, absent_other_way, both_same):
        assert fleet.independent(*pair) is False, f"absence or a match is not independence: {pair}"
    assert fleet.independent(*genuinely_different) is True
