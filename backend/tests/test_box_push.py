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
    box_push.reset_refusal()


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
