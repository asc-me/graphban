"""PRD-38 PR 1 — the attempt record: what the server derives, what the supervisor posts.

Each test names the acceptance criterion it pins (§7) and, where the criterion names one,
the sabotage it survives. The rows are read through a session opened after the request, so
what is inspected is what the app committed rather than what a test session holds.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select

from app.models import AttemptTelemetry, Delegation, Enrolment, Item
from app.services import delegation as dsvc
from app.services import harness as hsvc
from app.services import items as items_svc


def _mcp(client, key, name, args=None):
    r = client.post(
        "/api/mcp",
        json={"jsonrpc": "2.0", "id": 1, "method": "tools/call",
              "params": {"name": name, "arguments": args or {}}},
        headers={"X-API-Key": key},
    )
    assert r.status_code == 200, r.text
    return r.json()["result"]


def _ok(res) -> dict:
    assert not res.get("isError"), res
    return res["structuredContent"]


@pytest.fixture()
def proj(client, auth):
    return client.post("/api/projects", json={"name": "Telemetry"}, headers=auth).json()["id"]


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


def _agent(client, key, label, **kw) -> str:
    return _ok(_mcp(client, key, "register_agent", {"label": label, **kw}))["agent_id"]


def _item(client, key, title="telemetry", touchpoints=None, **kw) -> str:
    return _ok(_mcp(client, key, "create_item", {
        "title": title, "status": "next", "touchpoints": touchpoints or ["backend/app/x.py"],
        **kw}))["id"]


def _linked(client, key, db, planner, title="work", lane="backend", tier="cheap",
            touchpoints=None, description="", **caps):
    """A delegation with a declared child holding the item, ready for an outcome."""
    item = _item(client, key, title, touchpoints=touchpoints, description=description)
    d = _ok(_mcp(client, key, "delegate", {"id": item, "lane": lane, "tier": tier,
                                           "agent_id": planner}))
    child = _agent(client, key, f"child-{title}", parent_agent_id=planner,
                   capabilities={"instance": title, **caps})
    assert items_svc.claim_item(db, item, child) is not None
    return item, d["delegation_id"], child


def _sign_off(client, key, item, child, label="rev"):
    _ok(_mcp(client, key, "update_item", {"id": item, "status": "review", "agent_id": child}))
    reviewer = _agent(client, key, f"reviewer-{label}", capabilities={"instance": f"rev-{label}"})
    _ok(_mcp(client, key, "sign_off", {"id": item, "agent_id": reviewer,
                                       "evidence": [{"kind": "note", "detail": "read it"}]}))
    return reviewer


def _bounce(client, key, item, child, reason, label="rev"):
    _ok(_mcp(client, key, "update_item", {"id": item, "status": "review", "agent_id": child}))
    reviewer = _agent(client, key, f"reviewer-{label}", capabilities={"instance": f"rev-{label}"})
    _ok(_mcp(client, key, "bounce", {"id": item, "agent_id": reviewer, "reason": reason}))
    return reviewer


def _held(db, item_id, child):
    """A BOUND seat claims its item at registration (PRD-36 D1), so the child already holds
    it and `claim_item` would return None for the right reason. Assert the holding instead of
    re-claiming — a test that claimed here would pass on an unbound seat and prove nothing."""
    db.expire_all()
    stored = db.get(Item, item_id)
    assert stored.claimed_by == child, (stored.claimed_by, child)


def _telemetry(db, delegation_id) -> AttemptTelemetry | None:
    db.expire_all()
    return db.scalar(select(AttemptTelemetry).where(
        AttemptTelemetry.delegation_id == delegation_id))


def _post(client, key, body, expect=200):
    r = client.post("/api/fleet/attempts", json=body, headers={"X-API-Key": key})
    assert r.status_code == expect, r.text
    return r.json()


# ---- 1: one row per FINISHED delegation ------------------------------------------------------

def test_a_finished_delegation_produces_exactly_one_row_with_the_work_and_the_runner(
        client, key, db):
    """1. Sabotage: write the row at claim instead, and the open case below fails."""
    planner = _agent(client, key, "planner")
    item, did, child = _linked(client, key, db, planner, "signed", vendor="anthropic",
                               model="sonnet", tier="cheap")
    _sign_off(client, key, item, child)

    row = _telemetry(db, did)
    assert row is not None
    assert row.lane == "backend" and row.tier_requested == "cheap"
    assert row.vendor == "anthropic" and row.model == "sonnet"
    assert row.task_class == "general" and row.size_band == "S"
    assert row.attempt_no == 1 and row.outcome == "signed_off"
    assert row.bounce_category is None
    assert row.claim_to_finish_s is not None and row.claim_to_finish_s >= 0
    assert row.derived_at is not None and row.reported_at is None
    assert db.scalars(select(AttemptTelemetry)).all() == [row]


def test_an_open_or_expired_delegation_produces_no_row(client, key, db):
    """1. The absence half: a delegation nothing finished teaches nothing."""
    planner = _agent(client, key, "planner")
    item = _item(client, key, "never claimed")
    _ok(_mcp(client, key, "delegate", {"id": item, "lane": "backend", "tier": "cheap",
                                       "agent_id": planner, "lease_seconds": 1}))
    db.expire_all()
    assert db.scalars(select(AttemptTelemetry)).all() == []

    # And an EXPIRED one still produces none once other work finishes around it.
    row = db.scalars(select(Delegation)).all()[0]
    row.created_at = datetime.now(timezone.utc) - timedelta(hours=2)
    db.commit()
    assert dsvc.state(db.get(Delegation, row.id)) == "expired"
    # A CLAIMED delegation that has not ended yet is the case a row written at claim time
    # would catch: the child is working, and nothing is yet known about how it went.
    working, did_working, child_working = _linked(client, key, db, planner, "working")
    db.expire_all()
    assert db.scalars(select(AttemptTelemetry)).all() == []

    item2, did2, child2 = _linked(client, key, db, planner, "other")
    _sign_off(client, key, item2, child2, label="two")
    db.expire_all()
    rows = db.scalars(select(AttemptTelemetry)).all()
    assert [r.delegation_id for r in rows] == [did2]


def test_the_bounce_category_and_the_attempt_number_come_off_the_ledger(client, key, db):
    """1 / 4c. The second attempt on an item is `attempt_no` 2, not 1."""
    planner = _agent(client, key, "planner")
    item, did, child = _linked(client, key, db, planner, "bouncy")
    _bounce(client, key, item, child, "did not run the tests")
    first = _telemetry(db, did)
    assert first.outcome == "bounced" and first.bounce_category == "tests"
    assert first.attempt_no == 1

    stored = db.get(Item, item)
    stored.bounce_pinned_to = None
    stored.bounce_pinned_until = None
    db.commit()
    d2 = _ok(_mcp(client, key, "delegate", {"id": item, "lane": "backend", "tier": "frontier",
                                            "agent_id": planner}))
    child2 = _agent(client, key, "child-two", parent_agent_id=planner,
                    capabilities={"instance": "two"})
    assert items_svc.claim_item(db, item, child2) is not None
    _sign_off(client, key, item, child2, label="two")
    second = _telemetry(db, d2["delegation_id"])
    assert second.attempt_no == 2 and second.outcome == "signed_off"


# ---- 2: the route ----------------------------------------------------------------------------

def test_the_exit_post_enriches_the_row_and_is_idempotent(client, key, db):
    """2."""
    planner = _agent(client, key, "planner")
    item, did, child = _linked(client, key, db, planner, "exit")
    _sign_off(client, key, item, child)

    body = {"delegation_id": did, "binary_version": "0.23.0", "turns_used": 38,
            "turn_budget": 40, "wall_seconds": 812, "tokens_in": 91000, "tokens_out": 4100,
            "exit_meaning": "ok"}
    first = _post(client, key, body)
    assert first["binary_version"] == "0.23.0" and first["turns_used"] == 38
    assert first["report_count"] == 1
    again = _post(client, key, body)
    assert again["report_count"] == 2
    db.expire_all()
    assert len(db.scalars(select(AttemptTelemetry)).all()) == 1
    row = _telemetry(db, did)
    assert row.tokens_in == 91000 and row.wall_seconds == 812


def test_a_session_token_and_a_key_that_cannot_write_the_project_are_refused(
        client, auth, key, db, proj):
    """2. A session is not a supervisor; a key on another project is told nothing."""
    planner = _agent(client, key, "planner")
    item, did, child = _linked(client, key, db, planner, "gated")
    _sign_off(client, key, item, child)

    assert client.post("/api/fleet/attempts", json={"delegation_id": did},
                       headers=auth).status_code == 401

    kate = client.post("/api/auth/login", json={"email": "kate@ascme-labs.com",
                                               "password": "graphban"}).json()["access_token"]
    kate_auth = {"Authorization": f"Bearer {kate}"}
    other = client.post("/api/projects", json={"name": "Elsewhere"},
                        headers=kate_auth).json()["id"]
    stranger = client.post("/api/api-keys", json={"name": "stranger", "project_id": other,
                                                  "scopes": ["read", "write"]},
                           headers=kate_auth).json()["plaintext"]
    # 404, never 403: the refusal must not tell a key that this id exists.
    r = client.post("/api/fleet/attempts", json={"delegation_id": did},
                    headers={"X-API-Key": stranger})
    assert r.status_code == 404, r.text


def test_a_post_that_names_no_address_is_refused(client, key):
    """2. Neither shape, nowhere to land."""
    r = client.post("/api/fleet/attempts", json={"turns_used": 3}, headers={"X-API-Key": key})
    assert r.status_code == 422


def test_an_exit_post_before_the_outcome_is_stored_and_answered_202(client, key, db):
    """2 / D3. The runtime facts are true whether or not an outcome ever arrives."""
    planner = _agent(client, key, "planner")
    item, did, child = _linked(client, key, db, planner, "early")
    early = _post(client, key, {"delegation_id": did, "turns_used": 12}, expect=202)
    assert early["derived"] is False and early["turns_used"] == 12

    _sign_off(client, key, item, child)
    row = _telemetry(db, did)
    assert row.turns_used == 12 and row.derived_at is not None and row.outcome == "signed_off"


# ---- 3: null is not zero ---------------------------------------------------------------------

def test_unreported_tokens_stay_null(client, key, db):
    """3. Sabotage: coalesce the token fields to 0 on write and this fails."""
    planner = _agent(client, key, "planner")
    item, did, child = _linked(client, key, db, planner, "silent")
    _sign_off(client, key, item, child)
    served = _post(client, key, {"delegation_id": did, "binary_version": "1.2.3"})
    assert served["tokens_in"] is None and served["tokens_out"] is None
    assert served["turns_used"] is None
    row = _telemetry(db, did)
    assert row.tokens_in is None and row.tokens_out is None


# ---- 4, 4a, 4b: how it was sampled -----------------------------------------------------------

def _seat(client, key, planner, item=None):
    args = {"role": "worker", "agent_id": planner}
    if item:
        args["item_id"] = item
    return _ok(_mcp(client, key, "mint_enrolment", args))


def test_sampled_is_unknown_without_a_launch_post(client, key, db):
    """4. Sabotage: default `sampled` to first_choice and this fails."""
    planner = _agent(client, key, "planner")
    item, did, child = _linked(client, key, db, planner, "nolaunch", vendor="gbagent",
                               model="qwen3.6")
    _sign_off(client, key, item, child)
    assert _telemetry(db, did).sampled == "unknown"


def test_sampled_reads_the_launch_post(client, key, db):
    """4. first_choice, fallback and explicit each come from what the supervisor resolved."""
    planner = _agent(client, key, "planner")
    for label, winner, runner_up, source, expected in (
            ("first", "gbagent:qwen3.6", "anthropic:sonnet", None, "first_choice"),
            ("second", "anthropic:sonnet", "gbagent:qwen3.6", None, "fallback"),
            ("third", "gbagent:qwen3.6", None, "explicit", "explicit"),
            ("fourth", "cursor:composer", "claude:opus", None, "unknown"),
    ):
        item, did, child = _linked(client, key, db, planner, label, vendor="gbagent",
                                   model="qwen3.6")
        seat = db.scalar(select(Enrolment).where(Enrolment.delegation_id == did))
        assert seat is None  # this child linked by parentage, so post against the delegation
        _sign_off(client, key, item, child, label=label)
        # The launch post lands late here on purpose: order must not decide the answer.
        row = _telemetry(db, did)
        row.chosen_winner, row.chosen_runner_up, row.chosen_source = winner, runner_up, source
        row.sampled = hsvc.sampled_from(declared_vendor=row.vendor, declared_model=row.model,
                                        winner=winner, runner_up=runner_up, source=source)
        db.commit()
        assert _telemetry(db, did).sampled == expected, label


def test_a_launch_post_on_a_seat_is_carried_into_the_row(client, key, db):
    """4 / D3. The seat is how a launch post addresses a row with no delegation yet."""
    planner = _agent(client, key, "planner")
    item = _item(client, key, "seated")
    d = _ok(_mcp(client, key, "delegate", {"id": item, "lane": "backend", "tier": "cheap",
                                           "agent_id": planner, "seat": True}))
    code = d.get("enrolment_code")
    assert code, d
    # 202: a launch post always precedes the outcome, so it is stored and not yet counted.
    launched = _post(client, key, {"enrolment_code": code, "winner": "gbagent:qwen3.6",
                                   "runner_up": "anthropic:sonnet", "adapter": "gbagent"},
                     expect=202)
    assert launched["delegation_id"] is None and launched["enrolment_id"]

    child = _ok(_mcp(client, key, "register_agent", {
        "label": "seated-child", "enrolment_code": code,
        "capabilities": {"vendor": "gbagent", "model": "qwen3.6", "instance": "seated"}}))["agent_id"]
    _held(db, item, child)
    _sign_off(client, key, item, child, label="seat")
    row = _telemetry(db, d["delegation_id"])
    assert row.chosen_winner == "gbagent:qwen3.6"
    assert row.sampled == "first_choice"
    assert row.declaration_mismatch is False


def test_a_post_never_writes_a_null_over_a_value_and_a_change_is_counted(client, key, db):
    """4a. Sabotage: overwrite unconditionally and the null half fails."""
    planner = _agent(client, key, "planner")
    item, did, child = _linked(client, key, db, planner, "merge")
    _sign_off(client, key, item, child)
    _post(client, key, {"delegation_id": did, "turns_used": 20, "tokens_in": 5})
    # A second post that knows less must not erase what the first one knew.
    after = _post(client, key, {"delegation_id": did, "turns_used": 22})
    assert after["tokens_in"] == 5 and after["turns_used"] == 22
    assert after["report_count"] == 2


def test_a_declared_vendor_that_differs_from_the_launched_one_is_flagged_and_still_counts(
        client, key, db):
    """4b."""
    planner = _agent(client, key, "planner")
    item = _item(client, key, "liar")
    d = _ok(_mcp(client, key, "delegate", {"id": item, "lane": "backend", "tier": "cheap",
                                           "agent_id": planner, "seat": True}))
    _post(client, key, {"enrolment_code": d["enrolment_code"], "winner": "gbagent:qwen3.6",
                        "adapter": "gbagent"}, expect=202)
    child = _ok(_mcp(client, key, "register_agent", {
        "label": "liar-child", "enrolment_code": d["enrolment_code"],
        "capabilities": {"vendor": "anthropic", "model": "sonnet", "instance": "liar"}}))["agent_id"]
    _held(db, item, child)
    _sign_off(client, key, item, child, label="liar")
    row = _telemetry(db, d["delegation_id"])
    assert row.declaration_mismatch is True
    assert row.vendor == "anthropic"  # counted, in the cell it declared
    assert row.sampled == "unknown"


def test_an_undeclared_child_is_not_a_mismatch(client, key, db):
    """4b. GRPH-732's other failure has its own cell and must not be relabelled as this one."""
    planner = _agent(client, key, "planner")
    item = _item(client, key, "silent-child")
    d = _ok(_mcp(client, key, "delegate", {"id": item, "lane": "backend", "tier": "cheap",
                                           "agent_id": planner, "seat": True}))
    _post(client, key, {"enrolment_code": d["enrolment_code"], "winner": "gbagent:qwen3.6"},
          expect=202)
    child = _ok(_mcp(client, key, "register_agent", {
        "label": "mute", "enrolment_code": d["enrolment_code"],
        "capabilities": {"instance": "mute"}}))["agent_id"]
    _held(db, item, child)
    _sign_off(client, key, item, child, label="mute")
    row = _telemetry(db, d["delegation_id"])
    assert row.vendor == dsvc.UNDECLARED and row.declaration_mismatch is False


# ---- 4c: the bounce category is total --------------------------------------------------------

@pytest.mark.parametrize("reason,expected", [
    ("did not run the tests", "tests"),
    ("this is out of scope", "scope"),
    ("the retry logic is wrong", "quality"),
    ("wrong branch, and no PR link", "process"),
    ("", "other"),
    ("   ", "other"),
    ("שלום", "other"),
    ("mmmm", "other"),
])
def test_the_bounce_category_is_total_into_the_closed_set(reason, expected):
    """4c. Sabotage: return None for an unmatched reason and this fails."""
    assert hsvc.bounce_category(reason) == expected
    assert hsvc.bounce_category(reason) in hsvc.BOUNCE_CATEGORIES


def test_bounce_category_is_null_exactly_when_the_outcome_is_not_a_bounce(client, key, db):
    """4c."""
    planner = _agent(client, key, "planner")
    item, did, child = _linked(client, key, db, planner, "ok")
    _sign_off(client, key, item, child)
    assert _telemetry(db, did).bounce_category is None

    item2, did2, child2 = _linked(client, key, db, planner, "nope")
    _bounce(client, key, item2, child2, "שלום", label="two")
    assert _telemetry(db, did2).bounce_category == "other"


# ---- the size band ---------------------------------------------------------------------------

def test_the_size_band_reads_what_the_delegator_wrote_down(client, key, db):
    """1 / D2. Two touchpoints and a short description is S; six touchpoints is L."""
    planner = _agent(client, key, "planner")
    big = [f"backend/app/{n}.py" for n in range(6)]
    item, did, child = _linked(client, key, db, planner, "large", touchpoints=big)
    _sign_off(client, key, item, child)
    assert _telemetry(db, did).size_band == "L"

    item2, did2, child2 = _linked(client, key, db, planner, "middling",
                                  touchpoints=["backend/app/a.py", "backend/app/b.py",
                                               "backend/app/c.py"],
                                  description="x" * 700)
    _sign_off(client, key, item2, child2, label="mid")
    assert _telemetry(db, did2).size_band == "M"


# ---- 13: the window and the band -------------------------------------------------------------

def test_measured_counts_only_the_window_and_carries_the_band(client, key, db):
    """13. Sabotage: drop the cutoff and the aged attempt reappears in `n`."""
    planner = _agent(client, key, "planner")
    item, did, child = _linked(client, key, db, planner, "recent", vendor="gbagent",
                               model="qwen3.6")
    _sign_off(client, key, item, child)
    cells = dsvc.measured(db, None)
    assert len(cells) == 1 and cells[0]["quality"]["n"] == 1
    assert cells[0]["bands"] == {"S": {"value": 1.0, "n": 1}}

    row = db.get(Delegation, did)
    row.finished_at = datetime.now(timezone.utc) - timedelta(days=hsvc.WINDOW_DAYS + 1)
    db.commit()
    assert dsvc.measured(db, None) == []
    # And the window is a parameter on the read, not a truth about the row.
    assert dsvc.measured(db, None, window_days=400)[0]["quality"]["n"] == 1


def test_the_bands_split_a_cell_without_splitting_its_key(client, key, db):
    """13 / D9. The resolver joins on four keys and must keep finding one cell."""
    planner = _agent(client, key, "planner")
    small, did1, child1 = _linked(client, key, db, planner, "small", vendor="gbagent",
                                  model="qwen3.6")
    _sign_off(client, key, small, child1)
    big = [f"backend/app/{n}.py" for n in range(6)]
    large, did2, child2 = _linked(client, key, db, planner, "big", vendor="gbagent",
                                  model="qwen3.6", touchpoints=big)
    _bounce(client, key, large, child2, "wrong", label="big")

    cells = dsvc.measured(db, None)
    assert len(cells) == 1, cells
    assert cells[0]["quality"] == {"value": 0.5, "n": 2}
    assert cells[0]["bands"] == {"L": {"value": 0.0, "n": 1}, "S": {"value": 1.0, "n": 1}}
