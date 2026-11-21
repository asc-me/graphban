"""GRPH-1454: the org-view push is counts and rates, once per sync, replay-stable."""
import httpx
import pytest

from app.models import Project
from app.services import box_push
from app.services.rule_toggles import LibraryRule

RULE = "review.sabotage_required"
_ALLOWED_RULE = {"rule_id", "state", "profile_state", "overrides", "adoption", "observations", "effect"}
_ALLOWED_EFFECT = {"status", "value", "n", "min_n", "reason"}
_SECRET = "SECRET TITLE SHOULD NOT SHIP"
_PROSE = "not enough data (2 of 10 sign-offs after adoption)"


def _db():
    from app.db import SessionLocal
    return SessionLocal()


def _install(monkeypatch):
    rule = LibraryRule(
        id=RULE, stage="review", title=_SECRET, summary="prose that must not leave the box",
        enforcement="advisory", enforcer=None, default_on=True)
    bad = LibraryRule(
        id="Not A Slug", stage="review", title=_SECRET, summary="no",
        enforcement="enforced", enforcer=None, default_on=True)
    monkeypatch.setattr(box_push.rule_toggles, "LIBRARY", {rule.id: rule, bad.id: bad})

    def resolve(db, rule_id, project_id):
        return {"on": {"value": True}}

    def project_counts(db, rule_id, project_id):
        return {"overrides": [object(), object()]}

    def project_effect(db, project_id, rule_id):
        if project_id == "north":
            after = {"status": "insufficient", "value": None, "n": 2, "min_n": 10,
                     "n_band": "1-9", "reason": _PROSE}
        else:
            after = {"status": "measured", "value": 0.25, "n": 40, "min_n": 10,
                     "n_band": "10+", "reason": "measured prose must not ship"}
        return {"rules": [{"title": _SECRET, "escape_rate": {"after": after},
                           "items": ["GRPH-1"]}]}

    monkeypatch.setattr(box_push.rule_toggles, "resolve", resolve)
    monkeypatch.setattr(box_push.rule_measure, "project_counts", project_counts)
    monkeypatch.setattr(box_push.rule_measure, "project_effect", project_effect)
    monkeypatch.setattr(box_push.rule_measure, "project_profile", lambda *a, **k: None)
    _console(monkeypatch, {"hosted_mode": True, "signup_mode": "invite"})
    box_push.reset_refusal()


class _Config:
    status_code = 200

    def __init__(self, body):
        self.body = body

    def json(self):
        return self.body


def _console(monkeypatch, config):
    """What the console's `/api/config` says. The default is an older console."""
    probes = []

    def get(url, headers=None, timeout=None):
        probes.append(url)
        return _Config(config)

    monkeypatch.setattr(box_push.httpx, "get", get)
    return probes


def _projects(db):
    db.add(Project(id="north", name="North", tag="ZZN"))
    db.add(Project(id="south", name="South", tag="ZZS"))
    db.add(Project(id="dup-a", name="Shared", tag="ZZA"))
    db.add(Project(id="dup-b", name="Shared", tag="ZZB"))
    db.commit()


class _Ok:
    status_code = 200

    def raise_for_status(self):
        pass

    def json(self):
        return {"record": "box_push", "published": True}


def test_multi_project_push_is_counts_and_rates_only(client, monkeypatch):
    db = _db()
    _projects(db)
    _install(monkeypatch)
    body = box_push.build_box_push(db)
    assert body["record"] == "box_push"
    names = [p["project"] for p in body["projects"]]
    assert "North" in names and "South" in names
    assert "Shared" not in names
    assert len(names) == len(set(names))
    dumped = str(body)
    assert _SECRET not in dumped and _PROSE not in dumped
    assert "n_band" not in dumped and "GRPH-1" not in dumped
    assert "measured prose" not in dumped
    by_name = {p["project"]: p for p in body["projects"]}
    north = by_name["North"]["rules"]
    assert len(north) == 1 and north[0]["rule_id"] == RULE
    assert set(north[0]) == _ALLOWED_RULE
    assert set(north[0]["effect"]) == _ALLOWED_EFFECT
    assert north[0]["state"] == "advisory"
    assert north[0]["profile_state"] is None
    assert north[0]["overrides"] == 2
    assert north[0]["adoption"] == 1.0
    assert north[0]["observations"] == 0
    assert north[0]["effect"] == {
        "status": "insufficient", "value": None, "n": 2, "min_n": 10,
        "reason": "below_minimum_n"}
    south = by_name["South"]["rules"][0]
    assert south["effect"]["status"] == "measured"
    assert south["effect"]["value"] == 0.25
    assert south["effect"]["reason"] is None
    db.close()


def test_a_box_with_no_projects_does_not_post(monkeypatch):
    posts = []
    monkeypatch.setattr(box_push.httpx, "post", lambda *a, **k: posts.append(1))
    box_push.reset_refusal()
    db = _db()
    try:
        assert box_push.push_box_view(db, cloud_url="https://c", api_key="k") == {
            "pushed": False, "reason": "no projects"}
        assert posts == []
    finally:
        db.close()


def test_replay_reuses_the_idempotency_key(client, monkeypatch):
    db = _db()
    _projects(db)
    _install(monkeypatch)
    sent = []

    def post(url, json=None, headers=None, timeout=None):
        sent.append((url, json, headers))
        return _Ok()

    monkeypatch.setattr(box_push.httpx, "post", post)
    first = box_push.push_box_view(db, cloud_url="https://console.example/", api_key="k")
    second = box_push.push_box_view(db, cloud_url="https://console.example/", api_key="k")
    assert first["pushed"] is True and second["pushed"] is True
    assert len(sent) == 2
    assert sent[0][0] == "https://console.example/api/sync/box-push"
    assert sent[0][2]["X-API-Key"] == "k"
    assert sent[0][2]["Idempotency-Key"] == sent[1][2]["Idempotency-Key"]
    assert sent[0][2]["Idempotency-Key"] == box_push.payload_key(sent[0][1])
    assert first["idempotency_key"] == sent[0][2]["Idempotency-Key"]
    db.close()


def test_a_network_error_is_resent_in_full(client, monkeypatch):
    db = _db()
    _projects(db)
    _install(monkeypatch)
    calls = {"n": 0}

    def post(*a, **k):
        calls["n"] += 1
        raise httpx.ConnectError("down")

    monkeypatch.setattr(box_push.httpx, "post", post)
    with pytest.raises(httpx.ConnectError):
        box_push.push_box_view(db, cloud_url="https://c", api_key="k")
    with pytest.raises(httpx.ConnectError):
        box_push.push_box_view(db, cloud_url="https://c", api_key="k")
    assert calls["n"] == 2
    db.close()


def test_a_refusal_is_logged_and_not_retried_until_the_payload_changes(client, monkeypatch, caplog):
    db = _db()
    _projects(db)
    _install(monkeypatch)
    sent = []

    class Refused:
        status_code = 422

        def json(self):
            return {"detail": "no", "failures": [
                {"project": "North", "field": "rules.0.effect.reason", "detail": "prose"}]}

        def raise_for_status(self):
            raise AssertionError("a 422 is recorded, not raised")

    def post(url, json=None, headers=None, timeout=None):
        sent.append(json)
        return Refused()

    monkeypatch.setattr(box_push.httpx, "post", post)
    caplog.set_level("WARNING", logger="graphban.box_push")
    first = box_push.push_box_view(db, cloud_url="https://c", api_key="k")
    assert first["pushed"] is False and first["refused"] is True
    assert first["failures"][0]["project"] == "North"
    assert "North" in caplog.text
    assert "rules.0.effect.reason" in caplog.text
    again = box_push.push_box_view(db, cloud_url="https://c", api_key="k")
    assert again["repeated"] is True
    assert len(sent) == 1
    db.add(Project(id="later", name="Later", tag="ZZL"))
    db.commit()
    third = box_push.push_box_view(db, cloud_url="https://c", api_key="k")
    assert third["refused"] is True and third.get("repeated") is not True
    assert len(sent) == 2
    assert "Later" in [p["project"] for p in sent[1]["projects"]]
    db.close()


# ---- GRPH-1675: counts and publish flags, only for a console that advertises them ----------

_COUNT_KEYS = {"items", "open_claims", "active_agents"}
_STATUS_KEYS = {"backlog", "next", "in_progress", "review", "done", "blocked"}


def _tracker(db):
    """North: 2 backlog, 1 next, 1 in_progress (live lease), 1 review (live review lease),
    1 in_progress whose lease expired, 1 done. One live agent, one offline, one dismissed."""
    from datetime import datetime, timedelta, timezone

    from app.models import Agent, Item

    now = datetime.now(timezone.utc)
    old = now - timedelta(days=1)
    rows = [("backlog", None, None, None, None), ("backlog", None, None, None, None),
            ("next", None, None, None, None), ("in_progress", "a-live", now, None, None),
            ("review", None, None, "a-live", now), ("in_progress", "a-gone", old, None, None),
            ("done", None, None, None, None)]
    for n, (status, by, at, rby, rat) in enumerate(rows, start=1):
        db.add(Item(id=f"north-{n}", project_id="north", number=n, title=_SECRET,
                    status=status, claimed_by=by, claimed_at=at,
                    review_claimed_by=rby, review_claimed_at=rat))
    db.add(Agent(id="a-live", project_id="north", number=1, label="SECRET AGENT LABEL",
                 last_seen_at=now))
    db.add(Agent(id="a-gone", project_id="north", number=2, label="gone", last_seen_at=old))
    db.add(Agent(id="a-dismissed", project_id="north", number=3, label="d", last_seen_at=now,
                 dismissed_at=now))
    south = db.get(Project, "south")
    south.publish_specs = False
    db.commit()


def _push(monkeypatch, db, config):
    sent = []

    def post(url, json=None, headers=None, timeout=None):
        sent.append(json)
        return _Ok()

    monkeypatch.setattr(box_push.httpx, "post", post)
    probes = _console(monkeypatch, config)
    out = box_push.push_box_view(db, cloud_url="https://console.example/", api_key="k")
    assert out["pushed"] is True and len(sent) == 1
    return sent[0], probes


def test_a_console_advertising_counts_gets_the_trackers_numbers(client, monkeypatch):
    db = _db()
    _projects(db)
    _install(monkeypatch)
    _tracker(db)
    body, probes = _push(monkeypatch, db, {"features": ["box_push_counts"]})
    assert probes == ["https://console.example/api/config"]
    by_name = {p["project"]: p for p in body["projects"]}
    north = by_name["North"]
    assert set(north["counts"]) == _COUNT_KEYS
    assert north["counts"]["items"] == {"backlog": 2, "next": 1, "in_progress": 2,
                                        "review": 1, "done": 1, "blocked": 0}
    assert north["counts"]["open_claims"] == 2
    assert north["counts"]["active_agents"] == 1
    assert north["publishing"] == {"specs": True, "item_digests": True}
    assert by_name["South"]["publishing"] == {"specs": False, "item_digests": True}
    assert by_name["South"]["counts"]["items"] == dict.fromkeys(_STATUS_KEYS, 0)
    for entry in body["projects"]:
        counts = entry["counts"]
        values = list(counts["items"].values()) + [counts["open_claims"], counts["active_agents"]]
        assert all(type(v) is int and v >= 0 for v in values)
        assert all(type(v) is bool for v in entry["publishing"].values())
    dumped = str(body)
    for leak in (_SECRET, "SECRET AGENT LABEL", "north-", "a-live", "a-gone"):
        assert leak not in dumped
    db.close()


@pytest.mark.parametrize("config", [
    {"features": {"box_push_counts": True}},
    {"capabilities": ["box_push_counts"]},
    {"box_push_counts": True},
])
def test_each_advertisement_shape_turns_counts_on(client, monkeypatch, config):
    db = _db()
    _projects(db)
    _install(monkeypatch)
    body, _ = _push(monkeypatch, db, config)
    assert all("counts" in p and "publishing" in p for p in body["projects"])
    db.close()


class _StrictOldConsole:
    """A console that predates GRPH-1675: it refuses any project field it does not know."""
    status_code = 200

    def __init__(self, body):
        known = {"project", "rules"}
        extra = sorted({k for p in body["projects"] for k in p} - known)
        if extra:
            self.status_code = 422
            self.failures = [{"field": f, "detail": "unknown field"} for f in extra]

    def raise_for_status(self):
        pass

    def json(self):
        return {"failures": getattr(self, "failures", [])}


@pytest.mark.parametrize("config", [
    {"hosted_mode": True, "signup_mode": "invite"},
    {"features": ["something_else"]},
    {"features": {"box_push_counts": False}},
    None,
])
def test_an_older_console_gets_the_push_without_the_new_fields(client, monkeypatch, config):
    db = _db()
    _projects(db)
    _install(monkeypatch)
    _tracker(db)
    sent = []

    def post(url, json=None, headers=None, timeout=None):
        sent.append(json)
        return _StrictOldConsole(json)

    monkeypatch.setattr(box_push.httpx, "post", post)
    if config is None:
        def get(*a, **k):
            raise httpx.ConnectError("config unreadable")
        monkeypatch.setattr(box_push.httpx, "get", get)
    else:
        _console(monkeypatch, config)
    out = box_push.push_box_view(db, cloud_url="https://c", api_key="k")
    assert out["pushed"] is True, out
    assert all(set(p) == {"project", "rules"} for p in sent[0]["projects"])
    db.close()


def test_a_console_config_that_is_not_200_means_the_old_shape(client, monkeypatch):
    db = _db()
    _projects(db)
    _install(monkeypatch)

    class NotFound:
        status_code = 404

        def json(self):
            return {"features": ["box_push_counts"]}

    monkeypatch.setattr(box_push.httpx, "get", lambda *a, **k: NotFound())
    assert box_push.console_accepts_counts("https://c") is False
    db.close()
