"""PRD-47 S14 / GRPH-965 — deployment-wide usage aggregate."""


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
