"""PRD-47 S14 / GRPH-965 — deployment-wide usage aggregate."""
from datetime import datetime, timedelta, timezone

import pytest


def _login(client, email="alex@ascme-labs.com"):
    r = client.post("/api/auth/login", json={"email": email, "password": "graphban"})
    return {"Authorization": f"Bearer {r.json()['access_token']}"}


def test_usage_aggregate_shape(client):
    auth = _login(client)
    r = client.get("/api/usage", headers=auth)
    assert r.status_code == 200
    body = r.json()
    assert body["identity"]["mode"] == "self-host"
    assert "kpis" in body and len(body["kpis"]) == 5
    assert body["chart"]["buckets"]
    assert isinstance(body["by_project"], list)
    assert len(body["limits"]) == 4


def test_self_host_limits_are_undeclared_not_percentages(client):
    """A5 / D5: no declared cap → undeclared row, never a computed percentage."""
    auth = _login(client)
    body = client.get("/api/usage", headers=auth).json()
    for row in body["limits"]:
        assert row["declared"] is False
        assert row["limit"] is None
        assert row["used"] is None


def test_usage_csv_export(client):
    auth = _login(client)
    r = client.get("/api/usage", params={"format": "csv"}, headers=auth)
    assert r.status_code == 200
    assert "project_tag" in r.text
    assert "text/csv" in r.headers.get("content-type", "")


def test_usage_rejects_unknown_range(client):
    auth = _login(client)
    body = client.get("/api/usage", params={"range_days": 14}, headers=auth).json()
    assert body["range_days"] == 30


def test_usage_by_project_lists_readable_only(client, auth):
    """Rows are limited to projects the caller can read (seed gives alex the core project)."""
    body = client.get("/api/usage", headers=auth).json()
    tags = {r["tag"] for r in body["by_project"]}
    assert "GRPH" in tags or "core" in {r["id"] for r in body["by_project"]}


def test_usage_requires_auth(client):
    assert client.get("/api/usage").status_code == 401


def test_partial_coverage_when_range_exceeds_retention(client, auth, monkeypatch):
    """CALL-side: if aggregate ignored retention, coverage would stay 'full' on a 90d ask."""
    from app.services import usage as usage_svc

    monkeypatch.setattr(usage_svc.settings, "agent_call_retention_days", 7)
    body = client.get("/api/usage", params={"range_days": 90}, headers=auth).json()
    assert body["coverage"] == "partial"
    assert body["chart"]["note"]


# ── GRPH-1002: the Model usage panel's feed ────────────────────────────────
# `Usage.dc.html` draws the panel and PRD-47 D5 says the column is real, because PRD-38
# attempt records carry tokens. What those records mostly do NOT carry is a token count, so
# these pin the absence semantics rather than the arithmetic: an unmeasured pair has to stay
# absent all the way to the pixel, or the panel is the table of zeroes GRPH-980 refused.


@pytest.fixture()
def db(_clean_database):
    from app.db import SessionLocal

    s = SessionLocal()
    try:
        yield s
    finally:
        s.close()


@pytest.fixture()
def proj(client, auth):
    return client.post("/api/projects", json={"name": "Usage Models"}, headers=auth).json()["id"]


def _attempt(db, ident, project_id, *, vendor, model, tin=None, tout=None, when=None):
    from app.models import AttemptTelemetry

    now = when or datetime.now(timezone.utc)
    db.add(AttemptTelemetry(
        id=f"at-usage-{ident}", project_id=project_id, vendor=vendor, model=model,
        tokens_in=tin, tokens_out=tout, derived_at=now, reported_at=now,
    ))


def _rows(body):
    return {(r["vendor"], r["model"]): r for r in body["model_usage"]["rows"]}


def test_model_usage_separates_measured_from_unmeasured(client, auth, db, proj):
    from app.models import AttemptTelemetry

    for i in range(2):
        _attempt(db, f"anth-{i}", proj, vendor="anthropic", model="claude-sonnet-4",
                 tin=1000, tout=500)
    _attempt(db, "local", proj, vendor="gbagent", model="qwen3-8b", tin=100, tout=50)
    for i in range(3):
        _attempt(db, f"cur-{i}", proj, vendor="cursor", model=None)
    # A launch that never reached an outcome has no derived half, so it is not a finished
    # delegation and must not be priced as one.
    db.add(AttemptTelemetry(id="at-usage-runtime", project_id=proj, vendor="cursor",
                            reported_at=datetime.now(timezone.utc)))
    db.commit()

    usage = client.get("/api/usage", headers=auth).json()["model_usage"]
    rows = _rows({"model_usage": usage})

    measured = rows[("anthropic", "claude-sonnet-4")]
    assert measured["spawns"] == 2
    assert measured["tokens"] == 3000
    assert measured["tokens_reported"] == 2
    # claude-sonnet list price: 3 in / 15 out per Mtoken, over 2000 in and 1000 out.
    assert measured["cost_usd"] == pytest.approx(0.021)

    # Local compute is a REAL zero — a different claim from "unpriced" below.
    assert rows[("gbagent", "qwen3-8b")]["cost_usd"] == 0.0

    unmeasured = rows[("cursor", "undeclared")]
    assert unmeasured["spawns"] == 3, "the runtime-only row is not a finished delegation"
    assert unmeasured["tokens"] is None
    assert unmeasured["tokens_reported"] == 0
    assert unmeasured["cost_usd"] is None

    assert usage["spawns"] == 6
    assert usage["tokens_reported"] == 3
    assert "3 of 6" in usage["note"]


def test_model_usage_never_prices_an_unmeasured_pair_at_zero(client, auth, db, proj):
    """A model that HAS a list price but reported no tokens must stay unpriced. Pricing it
    would multiply that price by a fabricated zero token count and print $0.00 — money this
    deployment did not spend, in the one column people budget against."""
    _attempt(db, "unmeasured", proj, vendor="anthropic", model="claude-sonnet-4")
    db.commit()

    row = _rows(client.get("/api/usage", headers=auth).json())[("anthropic", "claude-sonnet-4")]
    assert row["spawns"] == 1
    assert row["tokens"] is None
    assert row["cost_usd"] is None


def test_model_usage_is_present_and_empty_with_no_attempts(client, auth):
    """n=0 is a present empty histogram, not an absent key: the panel has to render its empty
    state, and it cannot do that from a key that is not there."""
    body = client.get("/api/usage", headers=auth).json()
    assert "model_usage" in body
    assert body["model_usage"]["rows"] == []
    assert body["model_usage"]["spawns"] == 0
    assert body["model_usage"]["note"] is None


def test_model_usage_honours_the_range_window(client, auth, db, proj):
    now = datetime.now(timezone.utc)
    _attempt(db, "old", proj, vendor="anthropic", model="claude-sonnet-4",
             tin=1000, tout=1000, when=now - timedelta(days=20))
    db.commit()

    inside = _rows(client.get("/api/usage", params={"range_days": 30}, headers=auth).json())
    assert ("anthropic", "claude-sonnet-4") in inside
    outside = _rows(client.get("/api/usage", params={"range_days": 7}, headers=auth).json())
    assert outside == {}
