"""GRPH-985 — a narrowing touchpoints write is accepted and discarded in silence.

**Accept:** `update_item` says what it did with a touchpoints payload — `{sent, added,
retained}` — and `retained` names the stored paths the call did not, which is the only signal
that a narrowing did not happen. Reported only when touchpoints were actually sent.

**What went wrong.** `union_touchpoints` cannot remove a path, deliberately: a write that
dropped a declared area would read as "this item collides with nothing", which is the absence
that looks like a clean partition. But a narrowing write is ACCEPTED and then ignored, with
nothing in the reply to say so, while `evidence` has reported `{sent, added, dropped}` since
GRPH-839. That asymmetry is the defect.

It cost a wave. Widening two items' touchpoints to stop an `UNDECLARED` proposal refusal
collapsed three independent clusters into one — 34 points of effort behind a single child — and
the narrowing write sent to undo it was accepted and thrown away:

    sent:   [projecthome/, SyncLinkPanel.tsx, project-home.test.tsx, sync-link.test.tsx]
    stored: [...those four..., web/src/lib/api.ts, queries.ts, types.ts]

No error, no warning, no diff in the reply. The item had to be re-filed as GRPH-986 to get a
correct list, which lost its history — the same unrecoverable shape as GRPH-955's false
attestation.
"""
from __future__ import annotations

import pytest

from app.services import items as items_svc


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
def proj(client, auth):
    return client.post("/api/projects", json={"name": "Intake"}, headers=auth).json()["id"]


@pytest.fixture()
def key(client, auth, proj):
    return client.post("/api/api-keys", json={"name": "tp", "project_id": proj},
                       headers=auth).json()["plaintext"]


# ---- the unit, where the polarity lives --------------------------------------------------

def test_a_narrowing_write_reports_what_it_kept():
    """THE ONE THAT MATTERS, in GRPH-968's exact shape. The caller sends four paths meaning
    "only these"; three others stay. `retained` is the only place that is said."""
    stored = ["web/src/features/projecthome/", "web/src/features/settings/SyncLinkPanel.tsx",
              "web/src/lib/api.ts", "web/src/lib/queries.ts", "web/src/lib/types.ts"]
    sent = ["web/src/features/projecthome/", "web/src/features/settings/SyncLinkPanel.tsx"]

    got = items_svc.touchpoint_intake(stored, sent)["touchpoint_intake"]

    assert got["sent"] == 2
    assert got["added"] == 0, "nothing new was named, so nothing was added"
    assert got["retained"] == ["web/src/lib/api.ts", "web/src/lib/queries.ts",
                               "web/src/lib/types.ts"]


def test_a_widening_write_retains_nothing_and_says_what_it_added():
    """The ordinary case — a reap sending measured paths on top of declared ones. `retained` is
    empty because the call named everything already stored, and empty here means "nothing was
    held back", which is true."""
    got = items_svc.touchpoint_intake(["a.py"], ["a.py", "b.py"])["touchpoint_intake"]

    assert got == {"sent": 2, "added": 1, "retained": []}


def test_an_identical_resend_is_a_retry_not_a_refusal():
    """Same discipline as `evidence_intake`: a retry must not read as a rejection, or an agent
    re-runs work already recorded."""
    got = items_svc.touchpoint_intake(["a.py"], ["a.py"])["touchpoint_intake"]

    assert got["retained"] == [], "the call named the stored path, so nothing was kept back"
    assert got["sent"] == 1


def test_a_first_write_onto_an_empty_list_retains_nothing():
    got = items_svc.touchpoint_intake([], ["a.py"])["touchpoint_intake"]

    assert got == {"sent": 1, "added": 1, "retained": []}


def test_the_report_agrees_with_what_union_touchpoints_actually_stores():
    """The two must not drift: a receipt that describes a different write than the one performed
    is worse than no receipt. Computed from `union_touchpoints` itself for that reason."""
    stored, sent = ["a.py", "b.py"], ["b.py", "c.py"]

    after = items_svc.union_touchpoints(stored, sent)
    got = items_svc.touchpoint_intake(stored, sent)["touchpoint_intake"]

    assert set(after) == {"a.py", "b.py", "c.py"}
    assert got["added"] == 1            # c.py
    assert got["retained"] == ["a.py"]  # stored, and this call did not name it


# ---- over the wire ----------------------------------------------------------------------

def test_update_item_returns_the_intake_when_touchpoints_are_sent(client, key):
    """The CALL. A correct service function nobody wires up reports nothing, and the silence
    this fixes is in the reply."""
    item = _ok(client, key, "create_item",
               {"title": "a slice", "touchpoints": ["a.py", "b.py"]})["id"]

    out = _ok(client, key, "update_item", {"id": item, "touchpoints": ["a.py"]})

    assert "touchpoint_intake" in out, "the reply said nothing about the discarded narrowing"
    assert out["touchpoint_intake"]["retained"] == ["b.py"]
    assert "b.py" in out["touchpoints"], "and b.py is indeed still stored"


def test_no_intake_is_reported_when_no_touchpoints_were_sent(client, key):
    """`retained: []` on a call that carried none would say "nothing was kept back" when the
    truth is that nobody looked — the exact reading this field exists to prevent, so the field
    must be ABSENT rather than empty."""
    item = _ok(client, key, "create_item", {"title": "a slice", "touchpoints": ["a.py"]})["id"]

    out = _ok(client, key, "update_item", {"id": item, "status": "next"})

    assert "touchpoint_intake" not in out


def test_the_property_is_advertised_to_the_fleet_tier(client, auth, proj):
    """Declared behind the fleet tier, not on every manifest: adding it unconditionally
    measured 14257 tokens against a 14250 ceiling, and the footprint test's own message names
    this remedy. Advertisement, never a boundary — the dispatcher returns the field to any
    caller that sends touchpoints, which the test above proves with an ordinary key."""
    fleet_key = client.post("/api/api-keys",
                            json={"name": "fleet", "project_id": proj,
                                  "scopes": ["read", "write"],
                                  "tool_tiers": ["fleet"]}, headers=auth).json()["plaintext"]

    listed = client.post("/api/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
                         headers={"X-API-Key": fleet_key}).json()["result"]["tools"]
    update = next(t for t in listed if t["name"] == "update_item")

    assert "touchpoint_intake" in update["outputSchema"]["properties"]
