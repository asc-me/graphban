"""GRPH-1809: the observation summary names who measures the would-have."""
from datetime import timedelta

from app.db import SessionLocal
from app.models import Item
from app.services import items as items_svc
from app.services import rule_observations as RO
from app.models import utcnow


def _pid(client):
    r = client.post("/api/auth/login", json={"email": "alex@ascme-labs.com", "password": "graphban"})
    h = {"Authorization": f"Bearer {r.json()['access_token']}"}
    return client.post("/api/projects", json={"name": "Rocket"}, headers=h).json()["id"]


def test_measured_by():
    assert RO.measured_by("review.distinct_vendor") == "server"
    assert RO.measured_by("build.generated_files") == "supervisor"
    assert RO.measured_by("review.staff_every_row") == "supervisor"
    assert RO.measured_by("build.test_scope") == "brief"


def test_server_rule_zero_carries_checks_and_supervisor_rule_reports(client):
    pid = _pid(client)
    db = SessionLocal()
    try:
        since = utcnow() - timedelta(days=1)
        it = items_svc.create_item(db, title="x", project_id=pid, touchpoints=["a.py"],
                                   status="in_progress")
        db.get(Item, it.id).status = "done"
        db.commit()
        out = RO.summary(db, pid, "review.distinct_vendor", since)
        assert out["measured_by"] == "server" and out["would_have"] == 0
        assert out["checks"] == 1 and out["checks_noun"] == "sign-offs"
        # A server rule checked at claim, not at sign-off, does not claim a sign-off denominator.
        RO.record(db, pid, "merge.exclusive_areas", "would_hold", item_id=it.id)
        db.commit()
        ex = RO.summary(db, pid, "merge.exclusive_areas", since)
        assert ex["measured_by"] == "server" and ex["would_hold"] == 1
        assert ex["checks"] is None and ex["checks_noun"] is None
        sup = RO.summary(db, pid, "build.generated_files", since)
        assert sup["measured_by"] == "supervisor" and sup["last_supervisor_report"] is None
        RO.record(db, pid, "build.generated_files", "would_allow", item_id=it.id)
        db.commit()
        sup = RO.summary(db, pid, "build.generated_files", since)
        assert sup["last_supervisor_report"] is not None and sup["would_allow"] == 1
        assert RO.summary(db, pid, "build.test_scope", since)["measured_by"] == "brief"
    finally:
        db.close()
