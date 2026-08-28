"""The attestation port: the receipt, the scope that may write it, and the first adapter.

GRPH-541 / 542 / 544, under PRD GRPH-P26. The problem being closed is that `update_item`
validates a status transition for MEMBERSHIP IN A LIST and nothing else, so an agent working
without a reviewer reaches `done` by writing the string `"done"`. `fleet.sign_off` has a real
gate, but reaching it requires a reviewer role — two paths to `done`, one guarded.

**These tests do not yet assert that `done` is refused.** That refusal is GRPH-543 and lands
separately, because ~71 call sites across 36 test files transition an item to `done` as setup
for testing something else. What is asserted here is everything that refusal will read: that
an attestation can be neither faked in structure nor written by a key that should not have it,
and that the in-tree adapter produces one. A gate is only as good as the receipt it reads.

Every test below is written against the failure rather than the fix — each puts the system in
a state the guard exists to catch and requires it to say so.
"""
from __future__ import annotations

import pytest

from app.services import items as items_svc

ATTESTED_SHA = "a1b2c3d4e5f60718293a4b5c6d7e8f9012345678"


def _mint(client, auth, scopes):
    return client.post("/api/api-keys", json={"name": "t", "scopes": scopes},
                       headers=auth).json()["plaintext"]


def _rpc(client, key, name, arguments):
    return client.post("/api/mcp", json={
        "jsonrpc": "2.0", "id": 1, "method": "tools/call",
        "params": {"name": name, "arguments": arguments},
    }, headers={"X-API-Key": key}).json()


def _error(out):
    """The tool error, or None. MCP reports a refused CALL inside a successful RESPONSE —
    `result.isError` with the detail in `structuredContent` — so a test that looked for a
    top-level `error` key would read every refusal as a success."""
    result = out.get("result") or {}
    if not result.get("isError"):
        return None
    return (result.get("structuredContent") or {}).get("error") or {}


def _attestation(**over):
    row = {"kind": "attestation", "adapter": "ci", "commit": ATTESTED_SHA,
           "predicates": [{"name": "suite_green", "passed": True, "detail": "2363 passed"}]}
    row.update(over)
    return row


# ---- the receipt (GRPH-542) ----------------------------------------------------------

def test_a_well_formed_attestation_keeps_its_structure():
    """The control. Every refusal below is meaningless if nothing is ever accepted — a
    normaliser that rejected everything would pass all of them."""
    [row] = items_svc.normalize_evidence([_attestation()])

    assert row["kind"] == "attestation"
    assert row["adapter"] == "ci"
    assert row["commit"] == ATTESTED_SHA
    assert row["predicates"] == [
        {"name": "suite_green", "passed": True, "detail": "2363 passed"}]
    assert items_svc.has_valid_attestation([row]) is True


@pytest.mark.parametrize("missing", ["adapter", "commit", "predicates"])
def test_an_incomplete_attestation_is_demoted_not_accepted(missing):
    """A structured kind that accepts unstructured input is the free-text field with a
    stronger name, and the completion gate would then be checking a label rather than a
    fact. Same bargain `sabotage` already makes."""
    [row] = items_svc.normalize_evidence([_attestation(**{missing: None})])

    assert row["kind"] == "note", f"an attestation with no {missing} was accepted as one"
    assert items_svc.has_valid_attestation([row]) is False


def test_a_demoted_attestation_still_says_what_it_tried_to_claim():
    """Demoted, never DISAPPEARED. A receipt that vanished would mean the adapter believed
    it attested something, the server discarded it, and nothing anywhere says so."""
    [row] = items_svc.normalize_evidence(
        [{"kind": "attestation", "adapter": "ci", "commit": ""}])

    assert row["kind"] == "note"
    assert "attestation" in row["detail"], "the discarded claim left no trace"
    assert "ci" in row["detail"]


def test_a_string_passed_is_not_a_passed_predicate():
    """The truthy-string bypass, and the reason `passed` is checked with isinstance.

    A JSON client that sends `"passed": "false"` stores a non-empty string. Every plain
    truth test in Python reads that as True — so a FAILING predicate would satisfy a gate
    asking `all(p["passed"])`, and the attestation would certify the opposite of what the
    adapter measured.
    """
    [row] = items_svc.normalize_evidence(
        [_attestation(predicates=[{"name": "suite_green", "passed": "false"}])])

    assert row["kind"] == "note", "a string `passed` was accepted as a boolean"
    assert items_svc.has_valid_attestation([row]) is False


def test_an_attestation_with_no_predicates_attests_nothing():
    """`all([])` is True. Without the emptiness check in `valid_attestations`, a stored
    attestation carrying `predicates: []` satisfies "nothing failed" vacuously.

    **Built as a raw dict, deliberately — and the first version of this test was vacuous
    for exactly the reason it exists to catch.** Routed through `normalize_evidence` the
    receipt is demoted to a `note` before it ever reaches this check, so the assertion held
    no matter what `valid_attestations` did: deleting the check left the test green. That is
    GRPH-466's shape reproduced inside the guard against it.

    A stored row is the real case anyway. `valid_attestations` is what the completion gate
    calls on `item.evidence`, which holds rows written before this validation existed and
    rows from any other writer — so it has to defend itself rather than trust its input.
    """
    stored = [{"kind": "attestation", "adapter": "ci", "commit": ATTESTED_SHA,
               "predicates": []}]

    assert items_svc.attestation_receipts(stored), \
        "precondition: this must still LOOK like an attestation, or the check is untested"
    assert not items_svc.valid_attestations(stored), \
        "an attestation carrying zero predicates was treated as proof"
    assert items_svc.has_valid_attestation(stored) is False


def test_a_failing_predicate_invalidates_the_attestation():
    """The whole point. A recorded failure must not read as a pass just because a receipt
    exists."""
    rows = items_svc.normalize_evidence([_attestation(predicates=[
        {"name": "suite_green", "passed": True},
        {"name": "mutation_probe", "passed": False, "detail": "broke nothing"},
    ])])

    assert rows[0]["kind"] == "attestation", "precondition: it is still a well-formed receipt"
    assert items_svc.has_valid_attestation(rows) is False, \
        "an attestation with a failing predicate satisfied the gate"


def test_an_attestation_binds_to_the_commit_it_names():
    """Stops 'attested, then kept pushing'. The receipt vouches for one revision; asked
    about any other it must not answer for it."""
    rows = items_svc.normalize_evidence([_attestation()])

    assert items_svc.has_valid_attestation(rows, commit=ATTESTED_SHA) is True
    assert items_svc.has_valid_attestation(rows, commit="0" * 40) is False, \
        "an attestation vouched for a commit it never named"


def test_an_attestation_never_displaces_an_existing_receipt():
    """`append_evidence` is the one appender. An attestation that overwrote a builder's
    sabotage receipt would leave the item unprovable by its own proof."""
    stored = items_svc.normalize_evidence(
        [{"kind": "sabotage", "claim": "c", "mutation": "m", "tests_failed": 3}])
    merged = items_svc.append_evidence(stored, [_attestation()])

    assert items_svc.has_effective_sabotage(merged), "the sabotage receipt was displaced"
    assert items_svc.has_valid_attestation(merged)


# ---- the scope (GRPH-541) ------------------------------------------------------------

def test_a_write_key_cannot_write_an_attestation(client, auth):
    """The refusal the whole port rests on. If an ordinary agent key can mint an
    attestation then it can certify its own work, and the gate reading it later is
    checking a receipt the gated party wrote about itself."""
    item = client.post("/api/items", json={"title": "gated"}, headers=auth).json()
    key = _mint(client, auth, ["read", "write"])

    err = _error(_rpc(client, key, "update_item",
                      {"id": item["id"], "evidence": [_attestation()]}))

    assert err, "a write-scoped key wrote an attestation"
    assert err["code"] == "unauthorized", err
    assert "gate" in err["message"].lower(), \
        f"the refusal does not name the scope that would satisfy it: {err}"
    assert err.get("hint"), "a refused agent gets no machine-readable next step"


def test_the_gate_scope_ALONE_is_not_enough(client, auth):
    """A `["gate"]`-only key is minted successfully and cannot attest — the shape of an
    unusable credential that looks correct.

    `gate` is in `API_KEY_SCOPES`, so `POST /api-keys` returns 201 and a plausible key. It
    then fails at the only thing it was minted for, because attesting goes through
    `update_item`, which mutates, which needs `write`. Nothing said so: `fleet.mint` carries
    read+write+gate and every test above mints all three, so the combination that does NOT
    work was the one nobody had written down.

    Found building the Settings picker (GRPH-580), where sending `["gate"]` was the obvious
    reading of "mint a gate key" — and would have produced a key stored as a CI secret that
    403s on its first real attestation, months later, in CI.
    """
    item = client.post("/api/items", json={"title": "gated"}, headers=auth).json()
    key = _mint(client, auth, ["gate"])

    err = _error(_rpc(client, key, "update_item",
                      {"id": item["id"], "evidence": [_attestation()]}))

    assert err, "a gate-only key attested; this test's premise is wrong, not the code"
    assert "write" in err["message"], \
        f"the refusal does not name the scope it is actually missing: {err}"


def test_a_gate_key_may_write_one(client, auth):
    """The control for the refusal above. Without it, a check that refused everyone would
    pass that test — and the port would be unusable rather than protected."""
    item = client.post("/api/items", json={"title": "gated"}, headers=auth).json()
    key = _mint(client, auth, ["read", "write", "gate"])

    out = _rpc(client, key, "update_item",
               {"id": item["id"], "evidence": [_attestation()]})

    assert not _error(out), f"a gate-scoped key was refused: {_error(out)}"
    stored = client.get(f"/api/items/{item['id']}", headers=auth).json()["evidence"]
    assert items_svc.has_valid_attestation(stored, commit=ATTESTED_SHA), \
        f"the attestation did not survive the write: {stored}"


def test_an_ordinary_write_is_untouched_by_the_gate_check(client, auth):
    """The check reads ARGS, not the tool name. Gating all of `update_item` would take
    heartbeats and status moves from every agent to close a hole in one field."""
    item = client.post("/api/items", json={"title": "gated"}, headers=auth).json()
    key = _mint(client, auth, ["read", "write"])

    out = _rpc(client, key, "update_item", {
        "id": item["id"], "status": "in_progress",
        "evidence": [{"kind": "test", "detail": "suite green"}]})

    assert not _error(out), f"an ordinary evidence write was caught by the gate: {_error(out)}"


def test_the_attestation_fields_are_advertised_only_to_gate_keys(client, auth):
    """The manifest was measured at 13579 of a 13600 ceiling BEFORE this feature, so these
    fields cannot be carried for everyone. Injecting them for the keys that can use them is
    what keeps the number every ordinary agent pays exactly where it was."""
    def evidence_props(key):
        tools = client.post("/api/mcp", json={"jsonrpc": "2.0", "id": 1,
                                              "method": "tools/list"},
                            headers={"X-API-Key": key}).json()["result"]["tools"]
        u = next(t for t in tools if t["name"] == "update_item")
        return u["inputSchema"]["properties"]["evidence"]["items"]["properties"]

    plain = evidence_props(_mint(client, auth, ["read", "write"]))
    gated = evidence_props(_mint(client, auth, ["read", "write", "gate"]))

    assert "commit" not in plain, "every key pays for fields only a gate key may use"
    assert "attestation" not in plain["kind"]["enum"]
    assert {"adapter", "commit", "predicates"} <= set(gated), \
        "a gate key cannot see the fields it is the only one allowed to write"
    assert "attestation" in gated["kind"]["enum"]


def test_advertising_to_one_key_does_not_leak_into_the_next(client, auth):
    """TOOLS is module-level and shared. Mutating it in place would widen every subsequent
    caller's manifest — including the ordinary agents this exists to keep it away from —
    and the ceiling guard would only notice if someone re-ran it afterwards."""
    gate_key = _mint(client, auth, ["read", "write", "gate"])
    plain_key = _mint(client, auth, ["read", "write"])

    def props(key):
        tools = client.post("/api/mcp", json={"jsonrpc": "2.0", "id": 1,
                                              "method": "tools/list"},
                            headers={"X-API-Key": key}).json()["result"]["tools"]
        u = next(t for t in tools if t["name"] == "update_item")
        return u["inputSchema"]["properties"]["evidence"]["items"]["properties"]

    props(gate_key)                      # the gate key asks FIRST
    assert "commit" not in props(plain_key), \
        "a gate-scoped connection widened the manifest for every key after it"


# ---- the first adapter (GRPH-544) ----------------------------------------------------

@pytest.fixture()
def db(client):
    from app.db import SessionLocal
    s = SessionLocal()
    try:
        yield s
    finally:
        s.close()


def _reviewable(db, *, effort=1):
    """An item in `review`, built by somebody other than the agent that will sign it."""
    it = items_svc.create_item(db, title="built elsewhere", project_id="core",
                               effort=effort)
    it.status = "review"
    it.built_by = "builder-agent"
    db.commit()
    db.refresh(it)
    return it


def test_sign_off_with_a_commit_mints_a_valid_attestation(db):
    """The in-tree adapter, and the reason the offline guarantee survives: a default
    install always has one, so completion can never become unreachable for want of CI."""
    from app.services import fleet as fleet_svc

    it = _reviewable(db)
    out = fleet_svc.sign_off(db, item_id=it.id, agent_id="reviewer-1",
                             commit=ATTESTED_SHA)

    assert out.status == "done"
    assert items_svc.has_valid_attestation(out.evidence, commit=ATTESTED_SHA), \
        f"sign_off completed an item without attesting it: {out.evidence}"
    [att] = items_svc.valid_attestations(out.evidence)
    assert att["adapter"] == "fleet.sign_off"
    assert {p["name"] for p in att["predicates"]} == {
        "independent_review", "adversarial_evidence"}


def test_sign_off_without_a_commit_attests_nothing_and_adds_nothing(db):
    """Inventing a commit — a sentinel, or the item's PR string — would produce a receipt
    that looks binding and vouches for nothing, and a gate reading it later could not tell
    the difference. So no commit means no attestation.

    **And no explanatory note either.** The first version wrote one, and
    `test_fleet_review.py::test_the_full_record_carries_its_evidence` caught it: no caller in
    the tree passes a commit yet, so the note fired on every sign_off there is. That is the
    always-firing warning this repo already refuses elsewhere. The gap stays countable
    without it — an un-attested completion is an item whose `attestation_receipts` are
    empty, which is a query, not a receipt on every row.
    """
    from app.services import fleet as fleet_svc

    it = _reviewable(db)
    before = list(it.evidence or [])
    out = fleet_svc.sign_off(db, item_id=it.id, agent_id="reviewer-1")

    assert out.status == "done", "sign_off stopped working without a commit"
    assert not items_svc.attestation_receipts(out.evidence), \
        "an attestation was minted for a revision nobody named"
    assert out.evidence == before, \
        f"sign_off added a receipt on a path where nothing was proved: {out.evidence}"


def test_the_minted_attestation_records_why_adversarial_evidence_was_not_required(db):
    """Recording WHICH predicates ran is what makes a later weakening visible — the history
    shows something that stopped being checked, rather than an unbroken row of passes."""
    from app.services import fleet as fleet_svc

    it = _reviewable(db, effort=1)          # below ADVERSARIAL_EFFORT_THRESHOLD
    out = fleet_svc.sign_off(db, item_id=it.id, agent_id="reviewer-1",
                             commit=ATTESTED_SHA)

    [att] = items_svc.valid_attestations(out.evidence)
    adv = next(p for p in att["predicates"] if p["name"] == "adversarial_evidence")
    assert "not required" in adv["detail"], \
        f"the receipt does not say why the predicate passed: {adv}"


# ---- the refusal (GRPH-543) ----------------------------------------------------------
#
# These must NOT use tests/attest.py. It exists so the rest of the suite can step past this
# gate; a test of the gate that used it would be asserting the helper works.

def _item(db, **kw):
    from app.services import items as svc
    return svc.create_item(db, title=kw.pop("title", "work"), project_id="core", **kw)


def test_done_is_refused_when_nothing_has_attested_it(db):
    """The defect, pinned. `update_item` used to validate `done` for membership in a list
    and nothing else, so an agent reached it by writing the string."""
    it = _item(db)

    with pytest.raises(items_svc.MissingAttestation) as e:
        items_svc.update_item(db, it.id, status="done")

    assert "attestation" in str(e.value)
    assert "gate" in str(e.value), \
        "the refusal does not name what would satisfy it, so an agent routes around it"
    db.refresh(it)
    assert it.status != "done", "the refusal still moved the row"


def test_done_is_allowed_once_something_has_attested_it(db):
    """The control. A gate that refused everything would satisfy the test above and make
    the tracker unusable."""
    it = _item(db)
    items_svc.update_item(db, it.id, evidence=[_attestation()])

    out = items_svc.update_item(db, it.id, status="done")

    assert out.status == "done"


def test_an_attestation_and_the_completion_may_arrive_together(db):
    """An adapter attesting and completing in one write must not need two round trips to
    satisfy a gate it is itself satisfying."""
    it = _item(db)

    out = items_svc.update_item(db, it.id, status="done", evidence=[_attestation()])

    assert out.status == "done"


def test_a_failing_predicate_does_not_open_the_gate(db):
    """A recorded failure must not read as a pass because a receipt exists."""
    it = _item(db)
    items_svc.update_item(db, it.id, evidence=[_attestation(predicates=[
        {"name": "suite_green", "passed": False, "detail": "3 failed"}])])

    with pytest.raises(items_svc.MissingAttestation) as e:
        items_svc.update_item(db, it.id, status="done")

    assert "FAILING" in str(e.value), \
        "the refusal does not say the attestation it has is a failure, so the agent will " \
        "read it as absent and attest again rather than fix the failure"


def test_every_other_transition_is_untouched(db):
    """Only `done` is gated. Claiming, reviewing and blocking are reversible, and gating
    the states agents pass through constantly is how a gate teaches people to route
    around it."""
    it = _item(db)
    for status in ("in_progress", "review", "blocked", "next", "backlog"):
        assert items_svc.update_item(db, it.id, status=status).status == status


def test_an_item_already_done_is_not_re_gated(db):
    """Gated on the TRANSITION, not the state. Items completed before this existed carry no
    attestation, and re-saving one must not refuse — a migration that invalidates history
    makes every old item unusable."""
    it = _item(db)
    it.status = "done"                      # as a pre-migration row would be
    db.commit()

    out = items_svc.update_item(db, it.id, status="done", title="renamed")

    assert out.status == "done" and out.title == "renamed"


def test_only_two_places_in_the_app_may_write_a_done_status():
    """The ratchet, and the reason the gate holds rather than merely intends to.

    A gate on `update_item` is worth exactly as much as the guarantee that nothing else
    writes the column. Two writers exist deliberately — `update_item`, gated here, and
    `fleet.sign_off`, which reaches `done` through the reviewer path and its own
    independence and adversarial gates. A third added later would reopen the hole in
    silence, which is how the original defect survived: `fleet.sign_off` had a real gate
    and nobody noticed the other path had none.
    """
    import pathlib
    import re

    app = pathlib.Path(__file__).resolve().parent.parent / "app"
    allowed = {"services/fleet.py"}          # sign_off, deliberately
    offenders = []
    for path in app.rglob("*.py"):
        rel = str(path.relative_to(app))
        if rel in allowed:
            continue
        for i, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if re.search(r'\.status\s*=\s*["\']done["\']', line):
                offenders.append(f"{rel}:{i}")

    assert not offenders, (
        "these write a `done` status without passing the completion gate: "
        + ", ".join(offenders)
        + " — route them through items.update_item, or add them to `allowed` with the "
          "gate they enforce instead"
    )


# ---- refusals become memory (GRPH-550) -----------------------------------------------

def _shards_for(db, item):
    """REFUSAL shards only.

    Scoped to `gate refusal:` deliberately — the first version matched every shard on the
    item and the control below caught it: a successful completion fires `extract_lessons`,
    which writes a lesson shard, and a broad match read that as a refusal. A helper that
    cannot tell the two apart would have made every assertion here mean something else.
    """
    from sqlalchemy import select

    from app.models import MemoryShard
    return db.scalars(select(MemoryShard).where(
        MemoryShard.item_id == item.id,
        MemoryShard.source.like("gate refusal:%"))).all()


def test_a_refusal_survives_the_exception_that_carries_it(db):
    """The transaction trap. The gate raises immediately after recording, and an exception
    unwinds the session — so a refusal written without committing vanishes with the failure
    it describes, which is the same defect one level down."""
    it = _item(db)

    with pytest.raises(items_svc.MissingAttestation):
        items_svc.update_item(db, it.id, status="done")

    # A SEPARATE SESSION, and that is the whole test. The first version queried `db` — the
    # same session the gate wrote through — where autoflush surfaces pending objects that
    # were never committed. Removing the commit left it green, so it proved nothing about
    # durability. Only a second connection can tell "committed" from "still in this
    # session's memory".
    from app.db import SessionLocal
    other = SessionLocal()
    try:
        assert _shards_for(other, it), "the refusal was rolled back with the exception"
        stored = other.get(type(it), it.id).evidence
        assert any("refused at `done`" in (e.get("detail") or "") for e in stored), \
            "the item carries no record of having been refused"
    finally:
        other.close()


def test_the_shard_reaches_the_next_agent_without_asking_for_candidates(db):
    """The decision this item turns on. `search_memory` returns published shards only by
    default; filed as a candidate the refusal would sit in the triage queue, never surface,
    and the compounding this exists for would not happen.

    A gate refusal is not an agent self-report — the server refused and knows why — so the
    trusted-publication boundary is not what it would be crossing.
    """
    from app.services import memory as memory_svc

    it = _item(db)
    with pytest.raises(items_svc.MissingAttestation):
        items_svc.update_item(db, it.id, status="done")

    hits = memory_svc.search_memory(db, "why was completion refused", top_k=20,
                                    project_id="core")
    assert any(s.item_id == it.id for s, _ in hits), \
        "the refusal does not surface to an ordinary search — the loop is broken"


def test_the_shard_says_what_would_satisfy_the_gate(db):
    """A shard reading 'completion refused' teaches nothing the error message did not. The
    point is that the NEXT agent plans differently, which needs the remedy, not the verdict."""
    it = _item(db)
    with pytest.raises(items_svc.MissingAttestation):
        items_svc.update_item(db, it.id, status="done")

    [shard] = _shards_for(db, it)
    assert "attestation" in shard.text and "gate" in shard.text, \
        f"the shard does not name what would satisfy the gate: {shard.text}"
    assert it.key in shard.text, "the shard does not say which item it is about"


def test_the_same_refusal_twice_is_one_shard(db):
    """Without this the memory layer fills with the same refusal and semantic search gets
    worse, not better — the feature degrading the thing it exists to improve."""
    it = _item(db)
    for _ in range(3):
        with pytest.raises(items_svc.MissingAttestation):
            items_svc.update_item(db, it.id, status="done")

    assert len(_shards_for(db, it)) == 1, "each refusal wrote its own shard"


def test_two_different_refusals_are_two_shards(db):
    """The other direction, and the half a dedupe rule usually gets wrong. Swallowing a
    genuinely different refusal would be silent under any test that only counts down."""
    it = _item(db)
    with pytest.raises(items_svc.MissingAttestation):
        items_svc.update_item(db, it.id, status="done")          # attestation_missing

    items_svc.update_item(db, it.id, evidence=[_attestation(predicates=[
        {"name": "suite_green", "passed": False, "detail": "3 failed"}])])
    with pytest.raises(items_svc.MissingAttestation):
        items_svc.update_item(db, it.id, status="done")          # attestation_failing

    sources = {s.source for s in _shards_for(db, it)}
    assert len(sources) == 2, f"a different refusal was swallowed by the dedupe: {sources}"


def test_a_successful_completion_records_no_refusal(db):
    """The control. A recorder that fired unconditionally would satisfy every test above
    and fill memory with refusals that never happened."""
    it = _item(db)

    items_svc.update_item(db, it.id, status="done", evidence=[_attestation()])

    assert not _shards_for(db, it), "completing an item recorded a refusal"


def test_a_refusal_that_changes_updates_its_shard_rather_than_adding_one(db):
    """The update branch, and the reason it is not dead code.

    A refusal of the same KIND can say something different — a different predicate started
    failing — and the shard has to follow, or the memory the next agent reads describes a
    failure that has already been fixed. Still one shard: the dedupe key is
    (item, predicate), and `suite_green` failing then `mutation_probe` failing are the same
    predicate class with different content.
    """
    it = _item(db)
    items_svc.update_item(db, it.id, evidence=[_attestation(predicates=[
        {"name": "suite_green", "passed": False}])])
    with pytest.raises(items_svc.MissingAttestation):
        items_svc.update_item(db, it.id, status="done")

    [first] = _shards_for(db, it)
    assert "suite_green" in first.text

    items_svc.update_item(db, it.id, evidence=[_attestation(
        adapter="ci2", predicates=[{"name": "mutation_probe", "passed": False}])])
    with pytest.raises(items_svc.MissingAttestation):
        items_svc.update_item(db, it.id, status="done")

    # Read across a CONNECTION, not just the session that wrote it. Within one session an
    # uncommitted attribute change is visible anyway, so a same-session read cannot tell an
    # updated shard from a persisted one — and the update path is the only place
    # `record_refusal`'s own commit is load-bearing (`add_memory` commits the create path
    # itself).
    from app.db import SessionLocal
    other = SessionLocal()
    try:
        shards = _shards_for(other, it)
        assert len(shards) == 1, "a changed refusal added a second shard instead of updating"
        assert "mutation_probe" in shards[0].text, \
            "the shard still describes a failure that has since changed"
    finally:
        other.close()


# ---- an attestation goes stale (GRPH-555) --------------------------------------------

HEAD_A = "a" * 40
HEAD_B = "b" * 40


def test_an_attestation_for_an_older_commit_no_longer_opens_the_gate(db):
    """The hole this closes. Attest at A, push B, complete — and A's receipt used to be
    enough, because the gate never read the commit it names."""
    it = _item(db)
    items_svc.update_item(db, it.id, evidence=[_attestation(commit=HEAD_A)],
                          head_commit=HEAD_A)
    items_svc.update_item(db, it.id, head_commit=HEAD_B)          # a push happened

    with pytest.raises(items_svc.MissingAttestation) as e:
        items_svc.update_item(db, it.id, status="done")

    assert HEAD_A[:12] in str(e.value) and HEAD_B[:12] in str(e.value), \
        f"the refusal does not say which commit was attested vs which is current: {e.value}"


def test_the_same_commit_still_completes(db):
    """The control. A check that refused every commit would satisfy the test above and make
    completion impossible."""
    it = _item(db)
    items_svc.update_item(db, it.id, evidence=[_attestation(commit=HEAD_A)],
                          head_commit=HEAD_A)

    assert items_svc.update_item(db, it.id, status="done").status == "done"


def test_a_failing_run_that_writes_no_receipt_still_expires_the_old_one(db):
    """THE case, and the reason the head is reported whether or not the run passed.

    A passing run writes a receipt for the new head, so staleness never arises. The
    dangerous sequence is: attested at A, push B, CI FAILS on B, no receipt for B is
    written — and without a head move, A's receipt goes on vouching for code that has since
    broken.
    """
    it = _item(db)
    items_svc.update_item(db, it.id, evidence=[_attestation(commit=HEAD_A)],
                          head_commit=HEAD_A)
    items_svc.update_item(db, it.id, head_commit=HEAD_B)   # what a FAILED run records

    with pytest.raises(items_svc.MissingAttestation):
        items_svc.update_item(db, it.id, status="done")

    [shard] = [s for s in _shards_for(db, it) if "stale" in s.source]
    assert HEAD_B[:12] in shard.text, "the lesson does not name the commit that moved"


def test_with_no_head_reported_the_weaker_check_still_applies(db):
    """The fallback, argued rather than defaulted. Refusing here would make completion
    impossible for every install without a head-reporting adapter — including the offline
    one this product promises, and `fleet.sign_off` reports no head either.

    The guarantee is opt-in, and not silently: an item completed under the weak check is
    exactly one whose `head_commit` is empty, which is a query.
    """
    it = _item(db)
    items_svc.update_item(db, it.id, evidence=[_attestation(commit=HEAD_A)])

    out = items_svc.update_item(db, it.id, status="done")

    assert out.status == "done"
    assert out.head_commit == "", "precondition: no adapter reported a head"


def test_a_building_agent_cannot_move_the_head(client, auth):
    """`head_commit` is gate-scoped for the same reason the receipt is: an agent that could
    set it to the commit its stale attestation names would walk straight through the check
    this item adds."""
    item = client.post("/api/items", json={"title": "gated"}, headers=auth).json()
    key = _mint(client, auth, ["read", "write"])

    err = _error(_rpc(client, key, "update_item",
                      {"id": item["id"], "head_commit": HEAD_B}))

    assert err, "a write-scoped key moved the head"
    assert "head_commit" in err["message"], \
        f"the refusal does not name what was refused: {err}"


def test_two_commits_sharing_a_prefix_are_not_the_same_commit(db):
    """Prefix matching is the tempting shortcut — short SHAs are everywhere, so comparing
    with `startswith` feels tolerant. It is not: two distinct commits routinely share a
    leading run of characters, and a receipt for one would then vouch for the other.

    Written with SHAs that share 12 characters because the earlier staleness tests use
    `aaa…` and `bbb…`, which cannot fail under a prefix comparison — the mutation passed
    against them and this is what caught it.
    """
    shared = "c0ffee123456"
    old, new = shared + "1" * 28, shared + "2" * 28
    it = _item(db)
    items_svc.update_item(db, it.id, evidence=[_attestation(commit=old)],
                          head_commit=new)

    with pytest.raises(items_svc.MissingAttestation):
        items_svc.update_item(db, it.id, status="done")
