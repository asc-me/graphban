"""GRPH-1343: the claim-time routing gate reads the plan grant on self-host, not org.plan.

`routing.gate` is the other consumer of the plan (besides `require_capacity_routing`); without
this test, reverting its call to `quotas.capacity_routing_on` back to `plan_of(org)` would pass.
"""
import time

import pytest

from test_entitlements import _ok, _url, hosted, org  # noqa: F401  (fixtures)
from test_plan_grant import PUB, _grant

from app.services import plan_grant as G


@pytest.fixture()
def selfhost(client, org, monkeypatch):
    from app.config import settings
    from app.db import SessionLocal
    from app.models import InstalledPlanGrant, RuleDecisionPull

    r = client.post(_url(org), json={"vendor": "anthropic", "kind": "api_key",
                                     "secret_ref": "KEY_A"}, headers=org["owner"])
    assert r.status_code == 201, r.text
    agent = _ok(client, org["key"], "register_agent",
                {"label": "r", "entitlements": [r.json()["id"]],
                 "capabilities": {"vendor": "anthropic", "model": "m"}})["agent_id"]
    monkeypatch.setattr(settings, "hosted_mode", False)
    monkeypatch.setattr(G, "TRUSTED_PLAN_GRANT_KEYS", (PUB,))
    G._state.update(reason="missing", plan=None, issued_at=None, expires_at=None)
    db = SessionLocal()
    try:
        db.query(InstalledPlanGrant).delete()
        db.merge(RuleDecisionPull(id="instance", org_id=org["id"]))
        db.commit()
    finally:
        db.close()
    yield {**org, "agent": agent}
    G._state.update(reason="missing", plan=None, issued_at=None, expires_at=None)


def _gate(org):
    from app.db import SessionLocal
    from app.services import items, routing

    db = SessionLocal()
    try:
        item = items.create_item(db, title="w", status="next", project_id=org["project"]["id"])
        return routing.gate(db, item, org["agent"])
    finally:
        db.close()


def test_self_host_gate_ignores_org_plan_without_a_grant(client, selfhost):
    # org.plan is "team" (set by the `org` fixture); with no grant routing does not apply.
    assert _gate(selfhost) is None


def test_self_host_gate_routes_with_a_valid_grant(client, selfhost):
    from app.db import SessionLocal

    db = SessionLocal()
    try:
        assert G.install(db, None, *_grant(selfhost["id"]), now=time.time())["ok"] is True
    finally:
        db.close()
    assert _gate(selfhost) is not None


def _axis(org):
    from app.db import SessionLocal
    from app.services import routing

    db = SessionLocal()
    try:
        return routing.axis(db, org["project"]["id"], [org["agent"]])[org["agent"]]
    finally:
        db.close()


def test_self_host_axis_ignores_org_plan_without_a_grant(client, selfhost):
    # The proposal's read-only axis agrees with the gate: no grant, no routing shown.
    assert _axis(selfhost) is None


def test_self_host_axis_reads_a_valid_grant(client, selfhost):
    from app.db import SessionLocal

    db = SessionLocal()
    try:
        assert G.install(db, None, *_grant(selfhost["id"]), now=time.time())["ok"] is True
    finally:
        db.close()
    assert _axis(selfhost) is not None
