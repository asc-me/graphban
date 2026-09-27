"""GRPH-970 — a sign_off may not attest the commit its branch was cut from.

**Accept:** when a supervisor has recorded the base of an item's branch, `sign_off` refuses a
reviewer that attests that commit, names why, records the refusal, and leaves the item in
review. Any other commit passes. With no base recorded the sign_off still passes — and the
receipt says the comparison was not made, rather than implying it was.

**Where this came from.** On the p47-ui wave (2026-09-27, qwen-code, eight children) GRPH-954
reached `done` on a `fleet.sign_off` naming `7ea90ebe` — the base commit its own worktree was
cut from, an unrelated `.gbagent.toml` change. Its branch was reaped having pushed nothing and
no commit carrying the item's id existed on any ref. All three gate predicates passed, because
none of them looked at WHICH revision was being vouched for:

* `independent_review` compared two agent ids — both children of the same wave and model.
* `adversarial_evidence` counted receipts. It carried three, each naming a real test file and
  a plausible mutation. None had been run.
* `acceptance_coverage` read the description and found no clauses to check.

The evidence was not thin, it was *convincing*: 853 tests across 78 files, a four-surface
verification walk, three sabotage receipts. Those figures were the real ones another item had
recorded that morning, readable from the ledger.

**Why the base comes from the supervisor.** It is the one fact a child is the worst possible
source for, so a child-reported base would be the first thing a fabricating child omitted. The
supervisor cuts the tree and posts the base before the child starts. Evidence appends and never
removes, so the child cannot delete the receipt that convicts it — and adding one buys it
nothing, because another recorded base only narrows which commits it may attest.

**Why the receipt is a note.** The first version of this posted a `fleet.worktree`
*attestation*, which is what it wants to be. Writing `kind: attestation` needs the `gate` scope
and a supervisor runs on whatever `GBFLEET_API_KEY` holds — `["read", "write"]` on every wave
measured. In production the post would have been refused, the supervisor's `except` would have
logged a wave failure nobody reads, and this gate would have reported "not compared" for ever:
a guard that does nothing, which is the defect it exists to fix. The fixture below posts the
receipt with the same wave-grade key for that reason — a test using a privileged key would pass
while production silently could not write the receipt at all.
"""
import pytest

from app.models import Event


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
    return client.post("/api/projects", json={"name": "BaseCommit"},
                       headers=auth).json()["id"]


@pytest.fixture()
def key(client, auth, proj):
    return client.post("/api/api-keys", json={"name": "base", "project_id": proj},
                       headers=auth).json()["plaintext"]


BASE = "7ea90ebebe2b88d263dcae8ca77f84b69cb00300"
HEAD = "aaf6132e1d4c77b2c9a1e5f0b3d8a7c6e5f4d3c2"

# Effort 1 keeps the adversarial gate out of the way: this file is about the revision, and a
# test that has to satisfy two gates at once stops naming which one it is measuring.
SABOTAGE = {"kind": "sabotage", "claim": "c", "mutation": "m", "tests_failed": 1}


def _ready_for_review(client, key, *, effort=1, base=BASE):
    """One item in review, with its branch's base on the record, as a wave leaves it."""
    worker = _ok(client, key, "register_agent",
                 {"branch": "gb/p47-ui-3", "label": "w", "capabilities": {"instance": "w"}})
    made = _ok(client, key, "create_item",
               {"title": "a slice", "status": "next", "effort": effort})
    c = _ok(client, key, "claim_next", {"agent_id": worker["agent_id"]})
    item = c["item"]["id"]
    if base:
        # What the supervisor posts before the child starts — a NOTE, because writing an
        # attestation needs the `gate` scope and a wave's key does not have it. This fixture
        # uses the same wave-grade key on purpose: a test that posted the receipt with a
        # privileged key would pass while production silently could not write it at all.
        _ok(client, key, "update_item", {
            "id": item, "agent_id": worker["agent_id"],
            "evidence": [{"kind": "note",
                          "detail": f"gbfleet: branch cut from {base} (`gb/p47-ui-3`)"}],
        })
    _ok(client, key, "update_item",
        {"id": item, "status": "review", "agent_id": worker["agent_id"]})
    reviewer = _ok(client, key, "register_agent",
                   {"branch": "gb/p47-ui-3", "label": "r", "role_hint": "reviewer",
                    "capabilities": {"instance": "r"}})
    return item, reviewer


# ---- the gate ---------------------------------------------------------------------------------

def test_attesting_the_base_is_refused(client, key, db):
    """THE criterion. GRPH-954's exact shape: the reviewer names the commit the branch started
    at, so the receipt vouches for a tree with none of the item's work in it."""
    item, reviewer = _ready_for_review(client, key)

    res = _rpc(client, key, "sign_off",
               {"id": item, "agent_id": reviewer["agent_id"], "commit": BASE})

    err = res["structuredContent"]["error"]
    assert err["code"] == "conflict", "permitted to sign off; the revision is wrong"
    assert "cut from" in err["message"]
    assert "bounce" in err["message"], "and it names the correct next move"
    assert err["hint"]
    assert _ok(client, key, "get_item_details", {"id": item})["status"] == "review"


def test_an_abbreviated_base_is_still_the_base(client, key):
    """Git abbreviates and the two sides come from different places — the supervisor's
    `rev-parse` and whatever the reviewer pasted. A full-string comparison would have let
    `7ea90ebe` past a recorded `7ea90ebebe2b…`, which is the exact pair from the wave."""
    item, reviewer = _ready_for_review(client, key)

    res = _rpc(client, key, "sign_off",
               {"id": item, "agent_id": reviewer["agent_id"], "commit": BASE[:8]})

    assert res["structuredContent"]["error"]["code"] == "conflict"


def test_the_head_of_the_branch_passes(client, key):
    """The guard must not make review impossible — it narrows WHICH commit, not whether."""
    item, reviewer = _ready_for_review(client, key)

    out = _ok(client, key, "sign_off",
              {"id": item, "agent_id": reviewer["agent_id"], "commit": HEAD})

    assert out["status"] == "done"


def test_the_receipt_says_the_commit_was_compared(client, key):
    item, reviewer = _ready_for_review(client, key)

    _ok(client, key, "sign_off",
        {"id": item, "agent_id": reviewer["agent_id"], "commit": HEAD})

    got = _ok(client, key, "get_item_details", {"id": item})
    signed = [e for e in got["evidence"]
              if e.get("kind") == "attestation" and e.get("adapter") == "fleet.sign_off"]
    assert signed, "the sign_off receipt is there"
    named = {p["name"]: p for p in signed[-1]["predicates"]}
    assert "commit_is_not_the_base" in named
    assert named["commit_is_not_the_base"]["passed"] is True
    assert "1 base commit" in named["commit_is_not_the_base"]["detail"]


def test_an_unrecorded_base_says_so_instead_of_implying_a_check(client, key):
    """A supervisor that failed to post the receipt degrades to the old behaviour — which is
    acceptable — but the receipt must not read as though a comparison happened. An unrecorded
    base looking identical to a checked one is how this whole class of defect survives.
    """
    item, reviewer = _ready_for_review(client, key, base=None)

    out = _ok(client, key, "sign_off",
              {"id": item, "agent_id": reviewer["agent_id"], "commit": BASE})

    assert out["status"] == "done", "no base recorded, so nothing to compare against"
    got = _ok(client, key, "get_item_details", {"id": item})
    signed = [e for e in got["evidence"]
              if e.get("kind") == "attestation" and e.get("adapter") == "fleet.sign_off"]
    named = {p["name"]: p for p in signed[-1]["predicates"]}
    assert "NOT compared" in named["commit_is_not_the_base"]["detail"]


def test_the_refusal_is_in_the_ledger(client, key, db):
    """A gate whose refusals leave no trace cannot be examined for whether it is being routed
    around — the reason GRPH-321 stayed parked."""
    item, reviewer = _ready_for_review(client, key)

    _rpc(client, key, "sign_off",
         {"id": item, "agent_id": reviewer["agent_id"], "commit": BASE})

    actions = [e.action for e in db.query(Event).all()]
    assert "sign_off_refused" in actions


def test_the_worktree_receipt_does_not_count_as_an_attestation(client, key):
    """The attestation form carries no predicates on purpose, so the completion gate must not
    read it as proof. `valid_attestations` already rejects an empty predicate list; this pins
    that the base receipt cannot become the thing that satisfies completion."""
    from app.services.items import valid_attestations

    receipt = [{"kind": "attestation", "adapter": "fleet.worktree",
                "commit": BASE, "predicates": []}]
    assert valid_attestations(receipt) == []


def test_the_marker_and_its_reader_are_one_pair(client, key):
    """The carrier is prose, so the two halves have to be pinned together. `_record_cut_from`
    in gbfleet writes this string; `cut_from_commits` reads it. Nothing else may."""
    from app.services.items import CUT_FROM_MARKER, cut_from_commits

    detail = f"{CUT_FROM_MARKER}{BASE} (`gb/p47-ui-3`) — the base"
    assert cut_from_commits([{"kind": "note", "detail": detail}]) == [BASE]
    # A note that merely mentions a sha is not a base declaration.
    assert cut_from_commits([{"kind": "note", "detail": f"rebased onto {BASE}"}]) == []
