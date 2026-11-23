"""The grid payload's `baseline` block, apart from natural and probe (GRPH-1862)."""
from datetime import datetime, timedelta, timezone

from sqlalchemy import select

from app.db import SessionLocal
from app.models import AttemptTelemetry

IDENT = {"vendor": "anthropic", "model": "sonnet", "binary_version": "1.2.3",
         "suite_id": "template", "suite_version": "0.1.0"}


def _env(client, auth):
    proj = client.post("/api/projects", json={"name": "GridSrc"}, headers=auth).json()["id"]
    key = client.post("/api/api-keys", json={"name": "g", "scopes": ["read", "write"]},
                      headers=auth).json()["plaintext"]
    now = datetime.now(timezone.utc)
    with SessionLocal() as db:
        for i, o in enumerate(["signed_off", "signed_off", "bounced"]):
            db.add(AttemptTelemetry(
                id=f"at-g-{i}", project_id=proj, vendor="anthropic", model="sonnet",
                binary_version="1.2.3", capabilities=["A1"], outcome=o,
                tokens_in=10, tokens_out=10, derived_at=now - timedelta(days=1)))
        db.commit()
    return proj, {"X-API-Key": key}


def _leaf(cells, cap="A1"):
    for c in cells:
        for leaf in c.get("leaves") or [c]:
            if leaf["key"]["capability"] == cap:
                return leaf


def _strip(cells):
    out = []
    for c in cells:
        c = {k: v for k, v in c.items() if k != "baseline"}
        c["leaves"] = [{k: v for k, v in x.items() if k != "baseline"}
                       for x in c.get("leaves") or []]
        out.append(c)
    return out


def test_baseline_rides_beside_natural_and_probe_and_changes_neither(client, auth):
    proj, h = _env(client, auth)
    before = client.get(f"/api/harness/grid?project_id={proj}", headers=h).json()["cells"]
    assert _leaf(before)["baseline"] is None
    r = client.post(f"/api/bench/results?project_id={proj}", headers=h, json={
        **IDENT, "outcomes": [{"task_id": "t1", "outcome": "check_green", "capabilities": ["A1"]},
                              {"task_id": "t2", "outcome": "check_red", "capabilities": ["A1"]}]})
    assert r.status_code == 201, r.text
    after = client.get(f"/api/harness/grid?project_id={proj}", headers=h).json()["cells"]
    b = _leaf(after)["baseline"]
    assert b == {"suite_id": "template", "suite_version": "0.1.0", "n": 2, "green": 1,
                 "rate": 0.5, "below_floor": True}
    assert _strip(after) == _strip(before)
    with SessionLocal() as db:
        assert len(db.scalars(select(AttemptTelemetry)).all()) == 3
