"""A deployment can require a NAMED predicate, not merely any passing one (GRPH-569).

PRD-26 §Conformance and adversarial gates: *"These are predicates inside the completion gate,
not gates of their own. There is exactly one gate, on `done`; conformance and adversarial are
two of the things it checks."*

Before this, `has_valid_attestation` asked whether ANY attestation carried at least one
passing predicate — so every predicate was equally optional and an adapter that quietly
stopped emitting one was indistinguishable from an adapter that never emitted it.

Two things carry these tests:

- **Empty by default changes nothing.** The PRD requires that an install with only the CI
  adapter still completes on one `suite_green` predicate. A guard that broke that would be
  worse than no guard, so it is asserted rather than assumed.
- **Absent is not failing.** They call for opposite actions — run the adapter, versus read
  what it found — and a refusal that says the wrong one sends the reader to the wrong place.
"""
from __future__ import annotations

import pytest

from app.config import settings
from app.services import items as items_svc

SHA = "a1b2c3d4e5f60718293a4b5c6d7e8f9012345678"


def _attestation(*preds, adapter="github-actions", commit=SHA):
    return {"kind": "attestation", "adapter": adapter, "commit": commit,
            "predicates": [{"name": n, "passed": p, "detail": ""} for n, p in preds]}


GREEN = _attestation(("suite_green", True))


@pytest.fixture()
def db(client):
    from app.db import SessionLocal
    s = SessionLocal()
    try:
        yield s
    finally:
        s.close()


@pytest.fixture()
def require(monkeypatch):
    def _set(value: str):
        monkeypatch.setattr(settings, "required_predicates", value, raising=False)
    return _set


def _item(db, **kw):
    return items_svc.create_item(db, project_id="core", title="Built it", **kw)


# ---- the default must not change behaviour -------------------------------------------------

def test_with_nothing_required_one_passing_predicate_still_completes(db, require):
    """THE PRD'S EXPLICIT REQUIREMENT. An install with only the CI adapter must keep working;
    a gate that made completion impossible on a working deployment would be the worse bug."""
    require("")
    item = _item(db)
    items_svc.update_item(db, item.id, evidence=[GREEN])

    assert items_svc.update_item(db, item.id, status="done").status == "done"


def test_the_default_requires_nothing(require):
    """Pinned so the default cannot be changed without a test saying so — turning this on
    globally would break every existing install at once."""
    assert settings.required_predicate_list == [] or settings.required_predicates == ""


# ---- the refusal ---------------------------------------------------------------------------

def test_a_required_predicate_nobody_attested_refuses_completion(db, require):
    require("conformance")
    item = _item(db)
    items_svc.update_item(db, item.id, evidence=[GREEN])

    with pytest.raises(items_svc.MissingAttestation) as exc:
        items_svc.update_item(db, item.id, status="done")

    assert "conformance" in str(exc.value)


def test_the_refusal_says_a_check_never_ran_rather_than_a_check_failed(db, require):
    """Absent and failing call for opposite actions. This one must send the reader to the
    adapter, not to a problem in what already reported."""
    require("adversarial")
    item = _item(db)
    items_svc.update_item(db, item.id, evidence=[GREEN])

    with pytest.raises(items_svc.MissingAttestation) as exc:
        items_svc.update_item(db, item.id, status="done")

    message = str(exc.value)
    assert "nobody has run" in message or "nobody has run" in message.lower() \
        or "check nobody has run" in message
    assert "run it rather than looking for a problem" in message


def test_the_refusal_names_what_IS_attested(db, require):
    """So the operator can see the adapter that works and infer the one that does not —
    "conformance is missing" alone leaves them guessing whether anything ran at all."""
    require("conformance")
    item = _item(db)
    items_svc.update_item(db, item.id, evidence=[GREEN])

    with pytest.raises(items_svc.MissingAttestation) as exc:
        items_svc.update_item(db, item.id, status="done")

    assert "suite_green" in str(exc.value)


def test_supplying_the_required_predicate_completes(db, require):
    """The control. Without it a gate that refused EVERYTHING would pass every test above."""
    require("conformance")
    item = _item(db)
    items_svc.update_item(db, item.id, evidence=[
        GREEN, _attestation(("conformance", True), adapter="reviewer")])

    assert items_svc.update_item(db, item.id, status="done").status == "done"


def test_predicates_are_unioned_across_adapters(db, require):
    """CI attests one thing, a reviewer another, a probe a third. Requiring one adapter to
    carry them all would cap what an install can require at its most capable adapter."""
    require("suite_green,conformance,sabotage_observed")
    item = _item(db)
    items_svc.update_item(db, item.id, evidence=[
        GREEN,
        _attestation(("conformance", True), adapter="reviewer"),
        _attestation(("sabotage_observed", True), adapter="mutation-probe"),
    ])

    assert items_svc.update_item(db, item.id, status="done").status == "done"


# ---- what must not count -------------------------------------------------------------------

def test_a_failing_required_predicate_does_not_satisfy_the_requirement(db, require):
    """It was contradicted, not attested. The existing gate already refuses the receipt; what
    is asserted here is that it cannot ALSO be read as satisfying the requirement."""
    require("conformance")
    item = _item(db)
    items_svc.update_item(db, item.id, evidence=[
        GREEN, _attestation(("conformance", False), adapter="reviewer")])

    with pytest.raises(items_svc.MissingAttestation):
        items_svc.update_item(db, item.id, status="done")


def test_a_predicate_riding_on_a_receipt_with_a_failure_does_not_count(db, require):
    """The subtle one. `conformance` PASSED — but on an attestation whose other predicate
    failed, so the receipt as a whole is not sound and nothing on it may be relied on."""
    require("conformance")
    item = _item(db)
    items_svc.update_item(db, item.id, evidence=[
        GREEN, _attestation(("conformance", True), ("lint", False), adapter="reviewer")])

    with pytest.raises(items_svc.MissingAttestation):
        items_svc.update_item(db, item.id, status="done")


def test_a_predicate_attested_at_another_commit_does_not_count(db, require):
    """Staleness composes with the requirement rather than bypassing it (GRPH-555)."""
    require("conformance")
    item = _item(db)
    items_svc.update_item(db, item.id, evidence=[
        GREEN, _attestation(("conformance", True), adapter="reviewer", commit="b" * 40)],
        head_commit=SHA)

    with pytest.raises(items_svc.MissingAttestation):
        items_svc.update_item(db, item.id, status="done")


# ---- the helpers ---------------------------------------------------------------------------

def test_predicates_are_read_only_from_sound_receipts():
    """`valid_attestations` is the ONLY thing making a failing predicate not count, and this
    is the test that holds it there.

    Written after a sabotage survived: an `if q.get("passed")` filter here looked like a
    second line of defence and was dead code, because a valid attestation cannot contain a
    failing predicate in the first place. Removing it broke nothing — so it carried nothing,
    while reading as though it did. What must be sabotage-able is the precondition, and
    reading these names from `attestation_receipts` instead fails three tests.
    """
    ev = [_attestation(("a", True)), _attestation(("b", True), ("c", False)),
          _attestation(("d", False))]
    assert items_svc.attested_predicates(ev) == {"a"}, (
        "b rode on a receipt whose other predicate failed; d failed outright"
    )


def test_missing_predicates_is_stable_and_empty_when_satisfied():
    ev = [_attestation(("a", True))]
    assert items_svc.missing_predicates(ev, ["b", "a", "c"]) == ["b", "c"]
    assert items_svc.missing_predicates(ev, ["a"]) == []
    assert items_svc.missing_predicates(ev, []) == []


def test_the_setting_parses_a_comma_list(require):
    require(" conformance , adversarial ,, ")
    assert settings.required_predicate_list == ["conformance", "adversarial"]
