"""GRPH-1055 — a sign-off clause that needs `gh` is not handed to a child who lacks it.

SA-667's last clause required `gh`. The worktree shim refuses that binary to every fleet
child, and `claim_review` did not know. Five reviewers took the item, could not run the
clause, and left it. The queue looked like ordinary review the whole time.

A clause declares the capability (`{needs: gh}` or `[needs: human-only]`). Routing offers
the item only to a reviewer who declared it. When nobody live has, the queue says so —
`needs_human` — instead of reading as an empty hold. Mentioning `gh` in the prose is not
a declaration: inferring one would strand the clause that explains the refusal.
"""
from __future__ import annotations

import pytest

from app.services import fleet as fleet_svc


@pytest.fixture()
def proj(client, auth):
    return client.post("/api/projects", json={"name": "Clauses"}, headers=auth).json()["id"]


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


def _mcp(client, key, name, args=None):
    r = client.post("/api/mcp",
                    json={"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                          "params": {"name": name, "arguments": args or {}}},
                    headers={"X-API-Key": key})
    assert r.status_code == 200, r.text
    return r.json()["result"]


def _ok(res) -> dict:
    assert not res.get("isError"), res
    return res["structuredContent"]


def _agent(client, key, label, **caps) -> str:
    body = {"branch": "gb/test", "label": label}
    if caps:
        body["capabilities"] = caps
    return _ok(_mcp(client, key, "register_agent", body))["agent_id"]


def _to_review(client, key, db, title, description) -> str:
    who = _agent(client, key, title, instance=title)
    item = _ok(_mcp(client, key, "create_item", {
        "title": title, "status": "next", "description": description}))["id"]
    from app.services import items as items_svc
    assert items_svc.claim_item(db, item, who) is not None
    _ok(_mcp(client, key, "update_item",
             {"id": item, "status": "review", "agent_id": who}))
    return item


GH = """\
## Acceptance

- `gh pr view` shows the draft is open {needs: gh}
- the gate names the clause
"""

HUMAN = """\
## Acceptance

- [needs: human-only] a person confirms the deploy
"""

MENTION = """\
## Acceptance

- children are refused `gh` by the worktree shim
"""


def test_a_needs_marker_is_stripped_and_a_mention_is_not():
    """The marker is routing data. The words are what coverage matches. Sabotage: stop
    stripping, and the clause a reviewer quotes no longer equals the stored one; or start
    inferring `gh` from the prose, and the mention clause declares a capability."""
    clauses, unreadable = fleet_svc.parse_acceptance(GH)
    assert unreadable == []
    assert clauses == ["`gh pr view` shows the draft is open", "the gate names the clause"]
    assert fleet_svc.acceptance_needs(GH) == frozenset({"gh"})

    human, _ = fleet_svc.parse_acceptance(HUMAN)
    assert human == ["a person confirms the deploy"]
    assert fleet_svc.acceptance_needs(HUMAN) == frozenset({"human-only"})

    both, _ = fleet_svc.parse_acceptance(
        "## Acceptance\n\n- [needs: gh] the draft is open {needs: human-only}\n")
    assert both == ["the draft is open"]
    assert fleet_svc.acceptance_needs(
        "## Acceptance\n\n- [needs: gh] the draft is open {needs: human-only}\n"
    ) == frozenset({"gh", "human-only"})

    mentioned, _ = fleet_svc.parse_acceptance(MENTION)
    assert mentioned == ["children are refused `gh` by the worktree shim"]
    assert fleet_svc.acceptance_needs(MENTION) == frozenset()


def test_a_child_is_not_handed_a_clause_it_cannot_check(client, key, db, proj):
    """The finding. An ordinary item beside it is still offered, so one gh clause does not
    freeze the queue. Sabotage: drop `reviewer_can_check` from `claim_review` and the child
    is handed the gh item."""
    gh = _to_review(client, key, db, "gh-item", GH)
    plain = _to_review(client, key, db, "plain-item", MENTION)
    child = _agent(client, key, "child", instance="child", tier="cheap", vendor="xai")

    got = fleet_svc.claim_review(db, agent_id=child, project_id=proj)
    assert got is not None and got.id == plain
    assert got.id != gh

    # The gh item is still there. Releasing the review hold (not the build) is what a child
    # does when the only other item was ordinary work it just finished looking at, and the
    # reason must name the capability rather than an empty queue.
    from app.services import items as items_svc
    assert items_svc.release_item(db, plain, child) is not None
    reason = fleet_svc.review_block_reason(db, agent_id=child, project_id=proj)
    assert "gh" in reason and "needs" in reason, reason
    assert "no item awaiting" not in reason

    capable = _agent(client, key, "has-gh", instance="has-gh", tools=["gh"])
    got = fleet_svc.claim_review(db, agent_id=capable, project_id=proj)
    assert got is not None and got.id == gh


def test_human_only_is_not_satisfied_by_declaring_gh(client, key, db, proj):
    """`gh` and `human-only` are different declarations. A child allowed `gh` still cannot
    take a clause that asked for a person."""
    item = _to_review(client, key, db, "human-item", HUMAN)
    child = _agent(client, key, "gh-child", instance="gh-child", tools=["gh"])
    assert fleet_svc.claim_review(db, agent_id=child, project_id=proj) is None

    person = _agent(client, key, "person", instance="person", human=True)
    got = fleet_svc.claim_review(db, agent_id=person, project_id=proj)
    assert got is not None and got.id == item


def test_the_queue_marks_needs_human_until_someone_declares_the_tool(client, auth, key, db, proj):
    """The mark is the other half of the routing skip. An item no live reviewer can check
    must not look like ordinary review. Sabotage: leave `needs_human` false whenever
    `clause_needs` is non-empty and the row reads as a clean empty hold."""
    _to_review(client, key, db, "gh-queue", GH)
    _agent(client, key, "child", instance="queue-child", tier="cheap", vendor="xai")

    row = client.get(f"/api/fleet?project_id={proj}", headers=auth).json()["review_queue"][0]
    assert row["clause_needs"] == ["gh"]
    assert row["needs_human"] is True

    _agent(client, key, "has-gh", instance="queue-gh", tools=["gh"])
    row = client.get(f"/api/fleet?project_id={proj}", headers=auth).json()["review_queue"][0]
    assert row["clause_needs"] == ["gh"]
    assert row["needs_human"] is False


def test_sign_off_refuses_a_child_that_only_quotes_the_clause(client, key, db, proj):
    """THE CALL. `claim_review` skipping the item is not the gate: `sign_off` writes `done`
    itself. A child who quotes the clause must be refused on the wire, as a conflict that
    names `gh`, and the item must stay in review. Sabotage: drop the `ClauseCapability`
    handler in mcp_server and this comes back as an internal error, or drop the raise in
    `sign_off` and the item goes to done."""
    item = _to_review(client, key, db, "sign-gh", GH)
    child = _agent(client, key, "signer", instance="signer", tier="cheap", vendor="xai")
    res = _mcp(client, key, "sign_off", {
        "id": item, "agent_id": child,
        "evidence": [
            {"kind": "test", "detail": "`gh pr view` shows the draft is open"},
            {"kind": "test", "detail": "the gate names the clause"},
        ],
    })
    assert res.get("isError"), res
    err = res["structuredContent"]["error"]
    assert err["code"] == "conflict", err
    assert "`gh`" in err["message"], err["message"]
    assert _ok(_mcp(client, key, "get_item_details", {"id": item}))["status"] == "review"

    capable = _agent(client, key, "signer-gh", instance="signer-gh", tools=["gh"])
    out = _ok(_mcp(client, key, "sign_off", {
        "id": item, "agent_id": capable,
        "evidence": [
            {"kind": "test", "detail": "`gh pr view` shows the draft is open"},
            {"kind": "test", "detail": "the gate names the clause"},
        ],
    }))
    assert out["status"] == "done"


def test_claim_review_over_the_wire_names_the_gap(client, key, db, proj):
    """The reason the child reads is the MCP reply, not the service function. Sabotage:
    have `claim_review` return the old 'no item awaiting' string and this fails."""
    _to_review(client, key, db, "wire-gh", GH)
    child = _agent(client, key, "wire-child", instance="wire-child")
    out = _ok(_mcp(client, key, "claim_review", {"agent_id": child, "wait_seconds": 0}))
    assert out["claimed"] is False
    assert "gh" in out["reason"] and "bounce" in out["reason"], out["reason"]
