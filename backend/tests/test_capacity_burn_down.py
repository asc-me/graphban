"""Capacity burn-down: the current budget period and the server's forecast for it.

Pins: the period is placed only from a stated budget + stated next reset + a daily / weekly /
monthly cadence, and every other case says why; a stated reset in the past rolls forward; the
series is budget minus REPORTED tokens; the forecast grades runs-out-early / on-track /
stopped / run-out on the server; unreachable keeps its last known state; and the endpoint
serves `burn_down`, `next_reset_at` and `items_routed`.
"""
from datetime import datetime, timedelta, timezone

from app.models import Entitlement, EntitlementEvent
from app.services.headroom import burn_down
from tests.test_capacity_forecast import _cap, _set
from tests.test_entitlements import _ev, _new_ent, hosted, org  # noqa: F401

NOW = datetime(2026, 11, 9, 14, 0, tzinfo=timezone.utc)


def _row(**kw):
    base = {"id": "ent_x", "org_id": "o", "vendor": "anthropic", "kind": "api_key",
            "token_budget": 20_000_000, "reset_cadence": "monthly",
            "next_reset_at": datetime(2026, 12, 1, tzinfo=timezone.utc)}
    return Entitlement(**{**base, **kw})


def _spend(at, tokens):
    return EntitlementEvent(entitlement_id="ent_x", event="spend", tokens=tokens, created_at=at,
                            vendor_text="")


def _reg(at):
    return EntitlementEvent(entitlement_id="ent_x", event="registered", tokens=None,
                            created_at=at, vendor_text="")


def test_the_period_is_placed_only_from_stated_facts():
    assert burn_down(_row(token_budget=None), [], NOW, state="available") == {
        "available": False, "reason": "no token_budget stated"}
    assert burn_down(_row(next_reset_at=None), [], NOW, state="available")["reason"] == (
        "no next reset stated")
    assert burn_down(_row(reset_cadence=""), [], NOW, state="available")["reason"] == (
        "no reset cadence stated")
    rolling = burn_down(_row(reset_cadence="5h rolling"), [], NOW, state="available")
    assert rolling["available"] is False and "'5h rolling'" in rolling["reason"]


def test_a_monthly_period_runs_from_the_last_reset_to_the_stated_one():
    b = burn_down(_row(), [], NOW, state="available")
    assert (b["period"], b["step"]) == ("monthly", "day")
    assert b["start"] == "2026-11-01T00:00:00+00:00" and b["end"] == "2026-12-01T00:00:00+00:00"
    assert b["forecast"]["verdict"] == "no_spend" and b["left"] == b["budget"]


def test_a_stated_reset_in_the_past_rolls_forward_by_the_cadence():
    b = burn_down(_row(reset_cadence="Daily", next_reset_at=datetime(2026, 11, 1, tzinfo=timezone.utc)),
                  [], NOW, state="available")
    assert (b["period"], b["step"]) == ("daily", "hour")
    assert b["start"] == "2026-11-09T00:00:00+00:00" and b["end"] == "2026-11-10T00:00:00+00:00"
    assert len(b["series"]) == 15  # 00:00 … 13:00 hourly, then now


def test_runs_out_early_is_graded_on_the_last_three_days():
    ev = [_spend(datetime(2026, 11, 2, tzinfo=timezone.utc), 1_000_000),
          _spend(datetime(2026, 11, 7, tzinfo=timezone.utc), 3_000_000),
          _spend(datetime(2026, 11, 8, tzinfo=timezone.utc), 3_000_000)]
    b = burn_down(_row(token_budget=10_000_000), ev, NOW, state="available")
    assert b["spent"] == 7_000_000 and b["left"] == 3_000_000
    assert b["series"][0]["left"] == 10_000_000 and b["series"][-1]["left"] == 3_000_000
    fc = b["forecast"]
    assert fc["verdict"] == "runs_out_early" and fc["recent_rate"] == 2_000_000.0
    assert fc["runs_out_at"] == "2026-11-11T02:00:00+00:00"  # 3M left at 2M/day


def test_on_track_says_what_resets_unused():
    ev = [_spend(datetime(2026, 11, 8, tzinfo=timezone.utc), 300_000)]
    fc = burn_down(_row(), ev, NOW, state="available")["forecast"]
    assert fc["verdict"] == "on_track" and fc["runs_out_at"] is None
    assert 0 < fc["left_at_reset"] < 20_000_000


def test_broken_burn_has_stopped_rather_than_forecast():
    at = datetime(2026, 11, 5, 9, 12, tzinfo=timezone.utc)
    fc = burn_down(_row(), [_spend(at, 2_000_000)], NOW, state="broken")["forecast"]
    assert fc["verdict"] == "stopped" and fc["stopped_at"] == at.isoformat()
    assert fc["runs_out_at"] is None


def test_an_unreported_spawn_is_counted_not_spent():
    ev = [_reg(datetime(2026, 11, 8, tzinfo=timezone.utc)),
          _reg(datetime(2026, 11, 8, 1, tzinfo=timezone.utc)),
          _spend(datetime(2026, 11, 8, 2, tzinfo=timezone.utc), 500)]
    b = burn_down(_row(), ev, NOW, state="available")
    assert b["spent"] == 500 and b["unreported"] == 1


def test_the_endpoint_serves_burn_down_reset_and_items_routed(client, org):
    ent = _new_ent(client, org)
    reset = (datetime.now(timezone.utc) + timedelta(days=3)).replace(microsecond=0)
    _set(client, org, ent, token_budget=1_000_000, next_reset_at=reset.isoformat())
    _ev(client, org, ent, "registered")
    _ev(client, org, ent, "spend", tokens=4_000)
    cap = _cap(client, org)
    (row,) = [e for e in cap["entitlements"] if e["id"] == ent]
    assert row["next_reset_at"] == reset.isoformat()
    assert row["last_known_state"] is None
    b = row["burn_down"]
    assert b["available"] is True and b["period"] == "weekly"  # _subscription says weekly
    assert b["spent"] == 4_000 and b["left"] == 996_000
    assert b["end"] == reset.isoformat()
    assert cap["utilization"]["items_routed"] == 0


def test_unreachable_keeps_the_state_it_last_reported(client, org):
    from app.db import SessionLocal
    from app.models import Agent
    from app.services import headroom
    from tests.test_entitlements import _HOLDER

    ent = _new_ent(client, org)
    _ev(client, org, ent, "registered")
    db = SessionLocal()
    try:
        row = db.get(Entitlement, ent)
        assert headroom.headroom(db, row)["last_known_state"] is None, "reachable: no override"
        db.get(Agent, _HOLDER[ent]).last_seen_at = datetime(2020, 1, 1, tzinfo=timezone.utc)
        db.flush()
        h = headroom.headroom(db, row)
        assert (h["state"], h["last_known_state"]) == ("unreachable", "available")
    finally:
        db.rollback()
        db.close()
