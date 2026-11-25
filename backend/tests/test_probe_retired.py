"""GRPH-P98 §9: the probe panel is retired; its history is kept."""
from __future__ import annotations

from app.services import harness as hsvc


def _proj(client, auth) -> str:
    return client.post("/api/projects", json={"name": "probe-retired"}, headers=auth).json()["id"]


def test_probe_routes_return_410_with_the_pointer(client, auth):
    proj = _proj(client, auth)
    got = client.get(f"/api/harness/probe/candidates?project_id={proj}", headers=auth)
    assert got.status_code == 410
    assert "gbfleet bench run" in got.json()["detail"]
    post = client.post("/api/harness/probe/runs", json={
        "project_id": proj, "vendor": "v", "model": "m", "capability": "A4",
        "item_ids": ["x"]}, headers=auth)
    assert post.status_code == 410
    assert post.json()["use"] == "gbfleet bench run"


def test_start_probe_run_and_probe_candidates_are_removed():
    assert not hasattr(hsvc, "start_probe_run")
    assert not hasattr(hsvc, "probe_candidates")


def test_existing_probe_cells_still_report_under_the_probe_label(client, auth):
    proj = _proj(client, auth)
    report = client.get(f"/api/harness?project_id={proj}", headers=auth).json()
    # the probe side of the sampling split survives for history
    assert "probe_suggestions" in report
