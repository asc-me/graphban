"""GRPH-1404: the evidence and merge gates follow the project's test-scope mode.

In `agent_targeted` CI's `suite_green` at the item's head (the `ci` job needs the Postgres job)
satisfies `build.engine_evidence`; `agent_and_pr` keeps the local full receipt; `agent_full`
lets local full receipts stand in for a CI answer at merge.
"""
from __future__ import annotations

from datetime import timedelta

import pytest

from app.models import utcnow
from app.services import items as items_svc
from app.services import rule_toggles as T
from app.services import rules as R

RULE = "build.engine_evidence"
HEAD = "a" * 40
OTHER = "b" * 40
MIGRATION = ["backend/alembic/versions/0999_x.py"]
PR = {"kind": "url", "url": "https://github.com/asc-me/graphban/pull/999", "detail": "PR #999"}


def _ci(commit=HEAD, passed=True):
    return {"kind": "attestation", "adapter": "ci", "commit": commit,
            "predicates": [{"name": "suite_green", "passed": passed, "detail": "ci"}]}


def _full(engine):
    return {"kind": "test", "detail": f"{engine} full", "engine": engine, "scope": "full",
            "passed": 10, "failed": 0, "skipped": 0}


@pytest.fixture()
def db(client):
    from app.db import SessionLocal
    s = SessionLocal()
    try:
        yield s
    finally:
        s.close()


@pytest.fixture()
def mode(monkeypatch):
    monkeypatch.setattr(T, "LIBRARY", {})
    T.register(T.LibraryRule(
        id=RULE, stage="build", title="Engine evidence", summary="s",
        enforcement="enforced", enforcer=R.Enforcer("graphban_source", "app/services/items.py"),
        default_on=True, default_params={"path": "alembic/versions/", "engine": "postgres"}))

    def set_mode(m):
        monkeypatch.setattr(T, "test_scope_mode", lambda db, project_id: m)
    return set_mode


def _item(db, evidence=()):
    item = items_svc.create_item(db, project_id="core", title="Migration")
    items_svc.update_item(db, item.id, touchpoints=MIGRATION, evidence=list(evidence))
    return item


def _gate(db, item, incoming=(), head=HEAD):
    items_svc.require_engine_evidence(db, item, list(incoming), head=head)


def test_targeted_passing_ci_at_head_satisfies_the_gate(db, mode):
    mode("agent_targeted")
    _gate(db, _item(db, [_ci()]))


def test_targeted_waits_for_ci_before_it_reports(db, mode):
    mode("agent_targeted")
    with pytest.raises(items_svc.EvidenceRequired) as exc:
        _gate(db, _item(db))
    assert "waiting for CI" in str(exc.value)


def test_targeted_ci_for_a_different_commit_does_not_satisfy(db, mode):
    mode("agent_targeted")
    with pytest.raises(items_svc.EvidenceRequired):
        _gate(db, _item(db, [_ci(commit=OTHER)]))


def test_targeted_failing_ci_does_not_satisfy(db, mode):
    mode("agent_targeted")
    with pytest.raises(items_svc.EvidenceRequired):
        _gate(db, _item(db, [_ci(passed=False)]))


def test_targeted_local_full_receipt_still_satisfies(db, mode):
    mode("agent_targeted")
    _gate(db, _item(db, [_full("postgres")]), head=None)


def test_and_pr_still_requires_the_local_receipt(db, mode):
    mode("agent_and_pr")
    with pytest.raises(items_svc.EvidenceRequired) as exc:
        _gate(db, _item(db, [_ci()]))
    assert "waiting for CI" not in str(exc.value)
    _gate(db, _item(db, [_full("postgres")]))


def test_full_still_requires_the_local_receipt_to_review(db, mode):
    mode("agent_full")
    with pytest.raises(items_svc.EvidenceRequired):
        _gate(db, _item(db, [_ci()]))


# ---- merge ----------------------------------------------------------------------------

def _merge(db, evidence):
    item = _item(db, [PR, *evidence])
    item.pr_linked_at = utcnow() - timedelta(seconds=3600)
    db.commit()
    return items_svc.refuse_if_pr_cooling_down(db, item, [], commit=HEAD)


@pytest.fixture()
def cooldown(monkeypatch):
    from app.config import settings
    monkeypatch.setattr(settings, "pr_cooldown_seconds", 60, raising=False)


def test_full_merge_not_blocked_by_missing_ci_when_local_receipts_exist(db, mode, cooldown):
    mode("agent_full")
    assert _merge(db, [_full("postgres"), _full("sqlite")]) == []


def test_full_merge_still_blocked_without_local_receipts(db, mode, cooldown):
    mode("agent_full")
    with pytest.raises(items_svc.PRCINotGreen):
        _merge(db, [_full("postgres")])


def test_full_merge_still_blocked_by_a_failing_ci(db, mode, cooldown):
    mode("agent_full")
    with pytest.raises(items_svc.PRCINotGreen):
        _merge(db, [_full("postgres"), _full("sqlite"), _ci(passed=False)])


def test_and_pr_merge_requires_ci_even_with_local_receipts(db, mode, cooldown):
    mode("agent_and_pr")
    with pytest.raises(items_svc.PRCINotGreen):
        _merge(db, [_full("postgres"), _full("sqlite")])
