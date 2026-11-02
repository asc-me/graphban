"""Auto-update: opt-in windows, one check per window, one install per tag (GRPH-1299)."""
import threading
from datetime import datetime, timedelta, timezone

import pytest

from app.models import AutoUpdateConfig
from app.services import auto_update as au

UTC = timezone.utc
ALL = [0, 1, 2, 3, 4, 5, 6]
IN_WINDOW = datetime(2026, 11, 2, 3, 0, tzinfo=UTC)   # a Monday, 03:00 UTC
OUTSIDE = datetime(2026, 11, 2, 12, 0, tzinfo=UTC)


@pytest.fixture()
def db(client):
    from app.db import SessionLocal

    s = SessionLocal()
    try:
        yield s
    finally:
        s.close()


@pytest.fixture(autouse=True)
def _old_version(monkeypatch):
    """The suite runs as the real package version; pin an older one so a tag is 'available'."""
    from app.services import instance_update
    monkeypatch.setattr(instance_update, "running",
                        lambda: {"version": "2026.10.0", "git_sha": "abc"})


@pytest.fixture()
def box(db):
    """An enabled config; tests pass hosted/can_apply explicitly."""
    au.update_config(db, {"enabled": True})
    return db


class Spy:
    def __init__(self, state="available", tag="2026.11.1"):
        self.checks = 0
        self.applies = []
        self.state, self.tag = state, tag

    def check(self):
        self.checks += 1
        return {"state": self.state, "latest": {"tag": self.tag}}

    def apply(self, tag):
        self.applies.append(tag)
        return {"ok": True}


def run(db, spy, now, **kw):
    kw.setdefault("hosted", False)
    kw.setdefault("can_apply", True)
    kw.setdefault("progress_fn", lambda: {"state": "idle"})
    return au.tick(db, now=now, check_fn=spy.check, apply_fn=spy.apply, **kw)


# ── defaults ──

def test_defaults_are_disabled_nightly_utc(db):
    c = au.get_config(db)
    assert c.enabled is False and c.timezone == "UTC"
    for pre in ("check", "install"):
        assert getattr(c, f"{pre}_days") == ALL
        assert getattr(c, f"{pre}_start") == "02:00" and getattr(c, f"{pre}_end") == "05:00"
    assert c.last_check_at is None and c.last_auto_install_tag == ""


# ── window math ──

def occ(now, days=ALL, start="02:00", end="05:00", tz="UTC"):
    return au.open_occurrence(now, days, start, end, tz)


def test_inside_and_outside():
    assert occ(IN_WINDOW) == datetime(2026, 11, 2, 2, 0, tzinfo=UTC)
    assert occ(OUTSIDE) is None
    assert occ(datetime(2026, 11, 2, 5, 0, tzinfo=UTC)) is None  # end is exclusive
    assert occ(datetime(2026, 11, 2, 2, 0, tzinfo=UTC)) is not None


def test_window_crossing_midnight_belongs_to_its_start_day():
    late = datetime(2026, 11, 2, 23, 0, tzinfo=UTC)    # Monday
    early = datetime(2026, 11, 3, 1, 0, tzinfo=UTC)    # Tuesday
    assert occ(late, start="22:00", end="02:00") == datetime(2026, 11, 2, 22, 0, tzinfo=UTC)
    assert occ(early, start="22:00", end="02:00") == datetime(2026, 11, 2, 22, 0, tzinfo=UTC)
    assert occ(datetime(2026, 11, 3, 3, 0, tzinfo=UTC), start="22:00", end="02:00") is None
    # Monday-only: Tuesday 01:00 is still Monday's window; Wednesday 01:00 is not
    assert occ(early, days=[0], start="22:00", end="02:00") is not None
    assert occ(datetime(2026, 11, 4, 1, 0, tzinfo=UTC), days=[0], start="22:00", end="02:00") is None


def test_day_of_week_filter():
    assert occ(IN_WINDOW, days=[0]) is not None
    assert occ(IN_WINDOW, days=[1, 2, 3, 4, 5, 6]) is None


def test_non_utc_timezone():
    # 03:00 UTC is 22:00 the previous evening in New York (EST): outside 02-05 local.
    assert occ(IN_WINDOW, tz="America/New_York") is None
    # 08:00 UTC is 03:00 EST.
    got = occ(datetime(2026, 11, 2, 8, 0, tzinfo=UTC), tz="America/New_York")
    assert got == datetime(2026, 11, 2, 7, 0, tzinfo=UTC)


def test_dst_change_day_follows_the_wall_clock():
    # US fall-back 2026-11-01: 02:00 local is EDT (06:00Z) before, EST after. A 01:30-03:00
    # window starting on 11-01 spans the repeated hour; local 02:30 (EST, 07:30Z) is inside.
    assert occ(datetime(2026, 11, 1, 7, 30, tzinfo=UTC), start="01:30", end="03:00",
               tz="America/New_York") is not None
    # Next day the 02:00 start is EST (07:00Z), not EDT (06:00Z).
    assert occ(datetime(2026, 11, 2, 7, 30, tzinfo=UTC), tz="America/New_York") == \
        datetime(2026, 11, 2, 7, 0, tzinfo=UTC)
    # Spring forward 2026-03-08: 02:00 does not exist; the window start still resolves.
    assert occ(datetime(2026, 3, 8, 7, 30, tzinfo=UTC), start="02:30", end="05:00",
               tz="America/New_York") is not None


def test_next_start():
    assert au.next_start(OUTSIDE, ALL, "02:00", "UTC") == datetime(2026, 11, 3, 2, 0, tzinfo=UTC)
    assert au.next_start(datetime(2026, 11, 2, 1, 0, tzinfo=UTC), ALL, "02:00", "UTC") == \
        datetime(2026, 11, 2, 2, 0, tzinfo=UTC)
    # Wednesdays only, from Monday
    assert au.next_start(OUTSIDE, [2], "02:00", "UTC") == datetime(2026, 11, 4, 2, 0, tzinfo=UTC)
    # strictly after: exactly at the start asks for the next one
    assert au.next_start(datetime(2026, 11, 2, 2, 0, tzinfo=UTC), ALL, "02:00", "UTC") == \
        datetime(2026, 11, 3, 2, 0, tzinfo=UTC)
    # New York: 02:00 EST is 07:00Z
    assert au.next_start(OUTSIDE, ALL, "02:00", "America/New_York") == \
        datetime(2026, 11, 3, 7, 0, tzinfo=UTC)
    assert au.next_start(OUTSIDE, [], "02:00", "UTC") is None


def test_status_next_check(box):
    s = au.status(box, now=OUTSIDE, hosted=False, can_apply=True)
    assert s["next_check_at"] == "2026-11-03T02:00:00+00:00"
    s = au.status(box, now=IN_WINDOW, hosted=False, can_apply=True)
    assert s["next_check_at"] == IN_WINDOW.isoformat()  # due now


# ── check ──

def test_tick_outside_check_window_makes_no_call(box):
    spy = Spy()
    out = run(box, spy, OUTSIDE)
    assert spy.checks == 0 and out["checked"] is False


def test_one_check_per_window_across_ticks_and_restart(box):
    from app.db import SessionLocal

    spy = Spy(state="current")
    for i in range(10):
        run(box, spy, IN_WINDOW + timedelta(minutes=i))
    assert spy.checks == 1
    fresh = SessionLocal()  # a "restart": new session, state only from the row
    try:
        run(fresh, spy, IN_WINDOW + timedelta(minutes=30))
        assert spy.checks == 1
        run(fresh, spy, IN_WINDOW + timedelta(days=1))  # next day's window
        assert spy.checks == 2
    finally:
        fresh.close()
    assert au.get_config(box).last_check_state == "current"


# ── install ──

def test_install_starts_exactly_once(box):
    spy = Spy()
    for i in range(5):
        run(box, spy, IN_WINDOW + timedelta(minutes=i))
    assert spy.applies == ["2026.11.1"]
    c = au.get_config(box)
    assert c.last_auto_install_tag == "2026.11.1" and c.last_auto_install_outcome == "started"


def test_two_concurrent_passes_start_it_at_most_once(box):
    from app.db import SessionLocal

    spy = Spy()
    run(box, spy, IN_WINDOW)  # check + first attempt would already install; reset the attempt
    spy.applies.clear()
    box.query(AutoUpdateConfig).update({"last_auto_install_tag": ""}, synchronize_session=False)
    box.commit()
    barrier = threading.Barrier(2)

    def worker():
        s = SessionLocal()
        try:
            barrier.wait()
            run(s, spy, IN_WINDOW + timedelta(minutes=1))
        finally:
            s.close()

    ts = [threading.Thread(target=worker) for _ in range(2)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    assert len(spy.applies) == 1


@pytest.mark.parametrize("case", [
    "disabled", "hosted", "no_apply", "running", "failed_tag", "current", "unknown", "outside",
])
def test_no_install_when(db, case):
    spy = Spy(state={"current": "current", "unknown": "unknown"}.get(case, "available"))
    kw = {}
    now = IN_WINDOW
    if case != "disabled":
        au.update_config(db, {"enabled": True})
    if case == "hosted":
        kw["hosted"] = True
    if case == "no_apply":
        kw["can_apply"] = False
    if case == "running":
        kw["progress_fn"] = lambda: {"state": "running", "tag": "2026.11.1"}
    if case == "failed_tag":
        db.query(AutoUpdateConfig).update({
            "last_auto_install_tag": "2026.11.1", "last_auto_install_outcome": "failed"})
        db.commit()
    if case == "outside":
        # window for checks open, installs only at 10:00-11:00
        au.update_config(db, {"install_start": "10:00", "install_end": "11:00"})
    out = run(db, spy, now, **kw)
    assert spy.applies == [], out


def test_failed_tag_retries_for_a_newer_tag(box):
    spy = Spy()
    run(box, spy, IN_WINDOW)
    box.query(AutoUpdateConfig).update({"last_auto_install_outcome": "failed"})
    box.commit()
    run(box, spy, IN_WINDOW + timedelta(minutes=5))
    assert spy.applies == ["2026.11.1"]
    spy.tag = "2026.11.2"
    run(box, spy, IN_WINDOW + timedelta(days=1))
    assert spy.applies == ["2026.11.1", "2026.11.2"]


def test_attempt_is_recorded_before_apply(box):
    seen = {}

    class Boom(Spy):
        def apply(self, tag):
            from app.db import SessionLocal
            s = SessionLocal()
            try:
                seen["tag"] = au.get_config(s).last_auto_install_tag
            finally:
                s.close()
            raise RuntimeError("process died")

    spy = Boom()
    run(box, spy, IN_WINDOW)
    assert seen["tag"] == "2026.11.1"
    c = au.get_config(box)
    assert c.last_auto_install_tag == "2026.11.1" and c.last_auto_install_outcome == "failed"
    spy2 = Spy()
    run(box, spy2, IN_WINDOW + timedelta(minutes=5))
    assert spy2.applies == []  # no retry loop


def test_started_attempt_settles_from_progress(box):
    spy = Spy()
    run(box, spy, IN_WINDOW)
    run(box, spy, IN_WINDOW + timedelta(minutes=5),
        progress_fn=lambda: {"state": "failed", "tag": "2026.11.1", "error": "!! build broke"})
    c = au.get_config(box)
    assert c.last_auto_install_outcome == "failed" and "build broke" in c.last_auto_install_error


# ── REST ──

def test_patch_validates(client, auth):
    url = "/api/platform/auto-update"
    assert client.patch(url, headers=auth, json={"timezone": "Mars/Base"}).status_code == 422
    assert client.patch(url, headers=auth, json={"check_start": "25:00"}).status_code == 422
    assert client.patch(url, headers=auth, json={"check_start": "2am"}).status_code == 422
    assert client.patch(url, headers=auth, json={"check_days": [7]}).status_code == 422
    assert client.patch(url, headers=auth, json={"check_days": []}).status_code == 422
    assert client.patch(url, headers=auth,
                        json={"install_start": "03:00", "install_end": "03:00"}).status_code == 422
    r = client.patch(url, headers=auth, json={"timezone": "America/New_York",
                                              "check_start": "22:00", "check_end": "02:00"})
    assert r.status_code == 200, r.text
    assert r.json()["timezone"] == "America/New_York"
    assert client.get(url, headers=auth).json()["check"]["start"] == "22:00"


def test_patch_cannot_write_runtime_fields(client, auth):
    r = client.patch("/api/platform/auto-update", headers=auth,
                     json={"last_auto_install_tag": "9.9.9"})
    assert r.status_code == 422
    assert client.get("/api/platform/auto-update", headers=auth).json()[
        "last_auto_install_tag"] == ""


def test_cannot_enable_when_unavailable(client, auth):
    # The test box has neither a compose helper nor a native install.
    r = client.patch("/api/platform/auto-update", headers=auth, json={"enabled": True})
    assert r.status_code == 400
    got = client.get("/api/platform/auto-update", headers=auth).json()
    assert got["available"] is False and got["enabled"] is False and got["unavailable_reason"]


def test_can_enable_with_an_apply_path(client, auth, monkeypatch):
    from app.services import instance_update
    monkeypatch.setattr(instance_update, "helper_present", lambda path=None: True)
    r = client.patch("/api/platform/auto-update", headers=auth, json={"enabled": True})
    assert r.status_code == 200 and r.json()["enabled"] is True
