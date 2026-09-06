"""PRD-38 PR 2 — rollups and the Harness page's read (criteria 5, 6, 7).

Rows are built through the same path PR 1 pins — a delegation, a declared child, an outcome —
so what is rolled up is what the server really derives, not a fixture's idea of it.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select

from app.models import AttemptTelemetry, HarnessRollup, Item
from app.services import harness as hsvc
from app.services import items as items_svc


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


@pytest.fixture()
def proj(client, auth):
    return client.post("/api/projects", json={"name": "Harness"}, headers=auth).json()["id"]


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


def _attempt(client, key, db, planner, label, *, outcome="signed_off", vendor="gbagent",
             model="qwen3.6", lane="backend", tier="cheap", touchpoints=None,
             version=None, tokens=None, sampled=None, when=None) -> AttemptTelemetry:
    """One finished delegation, then whatever the test needs stamped onto its row.

    The stamping is deliberate and narrow: `derived_at` and `binary_version` are the two facts
    a test cannot produce by living through a week or installing another binary.
    """
    item = _ok(_mcp(client, key, "create_item", {
        "title": label, "status": "next", "touchpoints": touchpoints or ["backend/app/x.py"]}))["id"]
    d = _ok(_mcp(client, key, "delegate", {"id": item, "lane": lane, "tier": tier,
                                           "agent_id": planner}))
    child = _agent(client, key, f"child-{label}", parent_agent_id=planner,
                   capabilities={"instance": label, "vendor": vendor, "model": model})
    assert items_svc.claim_item(db, item, child) is not None
    _ok(_mcp(client, key, "update_item", {"id": item, "status": "review", "agent_id": child}))
    reviewer = _agent(client, key, f"rev-{label}", capabilities={"instance": f"rev-{label}"})
    if outcome == "signed_off":
        _ok(_mcp(client, key, "sign_off", {"id": item, "agent_id": reviewer,
                                           "evidence": [{"kind": "note", "detail": "read"}]}))
    else:
        _ok(_mcp(client, key, "bounce", {"id": item, "agent_id": reviewer,
                                         "reason": "did not run the tests"}))
        stored = db.get(Item, item)
        stored.bounce_pinned_to = None
        stored.bounce_pinned_until = None
        db.commit()
    db.expire_all()
    row = db.scalar(select(AttemptTelemetry).where(AttemptTelemetry.item_id == item))
    assert row is not None, label
    if version is not None:
        row.binary_version = version
    if tokens is not None:
        row.tokens_in, row.tokens_out = tokens
    if sampled is not None:
        row.sampled = sampled
    if when is not None:
        row.derived_at = when
    db.commit()
    return row


def _report(client, auth, proj, **params):
    q = "&".join(f"{k}={v}" for k, v in params.items())
    r = client.get(f"/api/harness?project_id={proj}" + (f"&{q}" if q else ""), headers=auth)
    assert r.status_code == 200, r.text
    return r.json()


def _cell(report, **match):
    hits = [c for c in report["cells"]
            if all(c["key"][k] == v for k, v in match.items())]
    assert len(hits) == 1, (match, [c["key"] for c in report["cells"]])
    return hits[0]


# ---- 5: rollups ------------------------------------------------------------------------------

def test_a_week_of_attempts_rolls_to_one_row_per_cell_and_rerolling_is_idempotent(
        client, key, db, proj):
    """5. Sabotage: fold into the existing row instead of recomputing, and the second roll
    doubles every count."""
    planner = _agent(client, key, "planner")
    for i in range(3):
        _attempt(client, key, db, planner, f"a{i}", outcome="signed_off" if i < 2 else "bounced")

    assert hsvc.roll(db, proj) == 1
    db.commit()
    rows = db.scalars(select(HarnessRollup)).all()
    assert len(rows) == 1
    assert (rows[0].finished, rows[0].signed_off, rows[0].bounced) == (3, 2, 1)
    assert rows[0].median_seconds is not None

    assert hsvc.roll(db, proj) == 1
    db.commit()
    db.expire_all()
    again = db.scalars(select(HarnessRollup)).all()
    assert len(again) == 1
    assert (again[0].finished, again[0].signed_off, again[0].bounced) == (3, 2, 1)


def test_two_cells_in_one_week_roll_to_two_rows(client, key, db, proj):
    """5."""
    planner = _agent(client, key, "planner")
    _attempt(client, key, db, planner, "backend-one")
    _attempt(client, key, db, planner, "frontend-one", lane="frontend",
             touchpoints=["web/src/a.tsx"])
    hsvc.roll(db, proj)
    db.commit()
    lanes = sorted(r.lane for r in db.scalars(select(HarnessRollup)).all())
    assert lanes == ["backend", "frontend"]


def test_a_thin_week_is_written_and_an_empty_week_is_absent(client, key, db, proj):
    """5. A rollup is a fact about a week; the floor is a rule about reading a number.
    Sabotage: skip weeks under the floor when rolling, and the thin week vanishes from the
    history that later lets the cell cross."""
    planner = _agent(client, key, "planner")
    old = datetime.now(timezone.utc) - timedelta(days=21)
    _attempt(client, key, db, planner, "lonely", when=old)
    for i in range(2):
        _attempt(client, key, db, planner, f"now{i}")
    hsvc.roll(db, proj)
    db.commit()

    weeks = sorted(r.week for r in db.scalars(select(HarnessRollup)).all())
    assert len(weeks) == 2, weeks
    assert hsvc.week_of(old) in weeks
    # The week between them had no attempts and is simply not there — the chart draws that
    # as a gap, because no attempts and a zero rate are different claims.
    between = hsvc.week_of(datetime.now(timezone.utc) - timedelta(days=10))
    assert between not in weeks


def test_tokens_are_summed_with_the_count_that_reported_them(client, key, db, proj):
    """5 / D11. Sabotage: store an average and the "not reported is not zero" case fails."""
    planner = _agent(client, key, "planner")
    _attempt(client, key, db, planner, "reported", tokens=(1000, 100))
    _attempt(client, key, db, planner, "silent")
    hsvc.roll(db, proj)
    db.commit()
    row = db.scalars(select(HarnessRollup)).all()[0]
    assert (row.tokens_in, row.tokens_out) == (1000, 100)
    assert row.tokens_reported == 1 and row.finished == 2
    assert row.signed_off_reported == 1


def test_a_read_rolls_the_weeks_whose_rows_have_moved(client, key, db, proj, auth):
    """5 / D11. The nightly job is the ordinary path; a page between runs must not be stale."""
    planner = _agent(client, key, "planner")
    _attempt(client, key, db, planner, "first")
    assert _report(client, auth, proj)["cells"][0]["finished"] == 1
    _attempt(client, key, db, planner, "second")
    assert _report(client, auth, proj)["cells"][0]["finished"] == 2


# ---- 6: the floor and the skew ----------------------------------------------------------------

def test_a_rate_below_the_floor_is_served_as_below_floor(client, key, db, proj, auth):
    """6. Sabotage: pool two cells to cross the floor and the per-cell count is wrong."""
    planner = _agent(client, key, "planner")
    for i in range(4):
        _attempt(client, key, db, planner, f"thin{i}")
    report = _report(client, auth, proj)
    cell = _cell(report, vendor="gbagent", lane="backend")
    assert cell["finished"] == 4 and cell["below_floor"] is True
    assert report["below_floor_count"] == 1
    assert report["floor"] == hsvc.FLOOR

    _attempt(client, key, db, planner, "fifth")
    after = _report(client, auth, proj)
    assert _cell(after, vendor="gbagent")["below_floor"] is False
    assert after["below_floor_count"] == 0


def test_pooling_two_cells_would_cross_the_floor_and_neither_cell_does(
        client, key, db, proj, auth):
    """6, the sabotage stated as its own test: four backend and four frontend attempts are
    eight, and eight is over the floor. Both cells must still read below it."""
    planner = _agent(client, key, "planner")
    for i in range(4):
        _attempt(client, key, db, planner, f"be{i}")
        _attempt(client, key, db, planner, f"fe{i}", lane="frontend",
                 touchpoints=["web/src/a.tsx"])
    report = _report(client, auth, proj)
    assert _cell(report, lane="backend")["below_floor"] is True
    assert _cell(report, lane="frontend")["below_floor"] is True
    assert report["below_floor_count"] == 2


def test_a_lopsided_cell_carries_the_skew_badge_and_keeps_its_denominator(
        client, key, db, proj, auth):
    """6. The badge is visual: it never removes a sample from the rate."""
    planner = _agent(client, key, "planner")
    for i in range(9):
        _attempt(client, key, db, planner, f"pref{i}", sampled="first_choice")
    _attempt(client, key, db, planner, "odd", sampled="explicit", outcome="bounced")

    cell = _cell(_report(client, auth, proj), vendor="gbagent")
    assert cell["sampling"] == {"first_choice": 9, "fallback": 0, "explicit": 1, "unknown": 0}
    assert cell["skew"] == {"reason": "first_choice", "share": 0.9}
    # Ten attempts, nine signed off. The one explicit sample is still in the denominator.
    assert cell["finished"] == 10 and cell["rate"] == 0.9


def test_an_evenly_sampled_cell_carries_no_badge(client, key, db, proj, auth):
    """6. Sabotage: badge every cell and this fails."""
    planner = _agent(client, key, "planner")
    for i in range(3):
        _attempt(client, key, db, planner, f"fc{i}", sampled="first_choice")
        _attempt(client, key, db, planner, f"fb{i}", sampled="fallback")
    assert _cell(_report(client, auth, proj), vendor="gbagent")["skew"] is None


def test_the_cost_proxy_is_suppressed_when_most_attempts_reported_nothing(
        client, key, db, proj, auth):
    """6 / D11. A partial numerator over a full denominator makes a silent vendor look cheap."""
    planner = _agent(client, key, "planner")
    _attempt(client, key, db, planner, "loud", tokens=(1000, 100))
    for i in range(3):
        _attempt(client, key, db, planner, f"quiet{i}")
    cost = _cell(_report(client, auth, proj), vendor="gbagent")["cost"]
    assert cost["comparable"] is False
    assert "1 of 4 attempts reported tokens" in cost["reason"]

    # With coverage, it compares — and divides by the signed-off attempts THAT REPORTED.
    planner2 = _agent(client, key, "planner2")
    for i in range(4):
        _attempt(client, key, db, planner2, f"full{i}", vendor="anthropic", model="sonnet",
                 tokens=(1000, 0))
    cost2 = _cell(_report(client, auth, proj), vendor="anthropic")["cost"]
    assert cost2["comparable"] is True and cost2["tokens_per_signed_off"] == 1000.0


def test_the_cost_proxy_divides_by_the_signed_off_attempts_that_reported(
        client, key, db, proj, auth):
    """6 / D11. Sabotage: divide by `signed_off` and this fails — the number would credit a
    silent attempt's success to the tokens the loud ones spent."""
    planner = _agent(client, key, "planner")
    # Four attempts report 1000 tokens each; three of those signed off. A fifth signed off
    # and reported nothing, so coverage is 4/5 and the denominators differ: 3, not 4.
    for i in range(3):
        _attempt(client, key, db, planner, f"paid{i}", vendor="cursor", model="composer",
                 tokens=(1000, 0))
    _attempt(client, key, db, planner, "paid-bounce", vendor="cursor", model="composer",
             tokens=(1000, 0), outcome="bounced")
    _attempt(client, key, db, planner, "free-win", vendor="cursor", model="composer")

    cost = _cell(_report(client, auth, proj), vendor="cursor")["cost"]
    assert cost["comparable"] is True
    assert cost["reported"] == 4 and cost["finished"] == 5
    assert cost["tokens_per_signed_off"] == round(4000 / 3, 1)


# ---- 7: versions ------------------------------------------------------------------------------

def test_two_binary_versions_are_two_cells_and_current_wins_by_default(
        client, key, db, proj, auth):
    """7. Sabotage: sort versions as strings and 0.100.0 loses to 0.23.0."""
    planner = _agent(client, key, "planner")
    _attempt(client, key, db, planner, "old", version="0.23.0")
    _attempt(client, key, db, planner, "new", version="0.100.0")

    default = _report(client, auth, proj)
    assert len(default["cells"]) == 1
    assert default["cells"][0]["key"]["binary_version"] == "0.100.0"
    assert default["cells"][0]["versions_seen"] == ["0.23.0", "0.100.0"]
    assert default["cells"][0]["is_current_version"] is True

    every = _report(client, auth, proj, versions="all")
    assert sorted(c["key"]["binary_version"] for c in every["cells"]) == ["0.100.0", "0.23.0"]
    assert [c["is_current_version"] for c in every["cells"] if c["key"]["binary_version"] == "0.23.0"] == [False]


def test_the_series_carries_a_floor_verdict_per_week(client, key, db, proj, auth):
    """6 / 7. A thin week is drawn grey rather than joined to a solid one."""
    planner = _agent(client, key, "planner")
    old = datetime.now(timezone.utc) - timedelta(days=14)
    _attempt(client, key, db, planner, "back-then", when=old)
    for i in range(5):
        _attempt(client, key, db, planner, f"lately{i}")
    series = _cell(_report(client, auth, proj), vendor="gbagent")["series"]
    assert len(series) == 2
    assert series[0]["below_floor"] is True and series[0]["finished"] == 1
    assert series[-1]["below_floor"] is False and series[-1]["finished"] == 5


def test_the_window_cuts_the_series_and_the_totals(client, key, db, proj, auth):
    """13 extended to the page: a week outside the window is not in `n`."""
    planner = _agent(client, key, "planner")
    _attempt(client, key, db, planner, "ancient",
             when=datetime.now(timezone.utc) - timedelta(days=200))
    _attempt(client, key, db, planner, "recent")
    assert _cell(_report(client, auth, proj), vendor="gbagent")["finished"] == 1
    assert _cell(_report(client, auth, proj, window_days=365), vendor="gbagent")["finished"] == 2


def test_the_read_is_project_scoped_and_refuses_a_stranger(client, auth, key, db, proj):
    """The gate: a project you cannot read is 404, not an empty report."""
    planner = _agent(client, key, "planner")
    _attempt(client, key, db, planner, "mine")
    kate = client.post("/api/auth/login", json={"email": "kate@ascme-labs.com",
                                               "password": "graphban"}).json()["access_token"]
    r = client.get(f"/api/harness?project_id={proj}",
                   headers={"Authorization": f"Bearer {kate}"})
    assert r.status_code == 404, r.text
