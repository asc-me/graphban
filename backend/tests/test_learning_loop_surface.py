"""The learning loop's agent surface, and the endpoint hooks report to (GRPH-310 / GRPH-344).

PRD-16 asks for the loop to be exposed "through the same service layer the web app calls",
so REST and MCP call the same functions — a second implementation is how the two drift
until one of them is wrong.

The `/used` route matters more than its size suggests. GRPH-309 appends telemetry to every
generated hook, and that line **swallows its own failures** so a telemetry outage can never
break the workflow. A missing route would therefore be completely silent: every hook would
report nothing forever, and zero observed uses on a measurable tier queues a DELETE. The
instrumentation would have retired exactly the hooks it was built to protect.
"""
import json

import pytest

from app.models import ArtifactRecommendation, Project
from app.services import artifacts as art_svc


@pytest.fixture()
def db(client):
    from app.db import SessionLocal

    s = SessionLocal()
    try:
        yield s
    finally:
        s.close()


@pytest.fixture()
def key(client, auth):
    return client.post("/api/api-keys", json={"name": "loop"}, headers=auth).json()["plaintext"]


def _rec(db, tier="skill", status="queued", draft="# artifact", title="A thing"):
    rec = ArtifactRecommendation(project_id="core", tier=tier, scope=title, title=title,
                                 status=status, draft=draft, draft_path="p/x",
                                 install_class="file_additive" if tier == "skill"
                                 else "shared_surgery", lesson_ids=[])
    db.add(rec)
    db.commit()
    db.refresh(rec)
    return rec


def _mcp(client, key, tool, args):
    r = client.post("/api/mcp", headers={"X-API-Key": key}, json={
        "jsonrpc": "2.0", "id": 1, "method": "tools/call",
        "params": {"name": tool, "arguments": args}})
    res = r.json()["result"]
    if res.get("isError"):
        return {"ERROR": res["structuredContent"]["error"]}
    return res["structuredContent"]


# ---- the endpoint hooks report to ----------------------------------------------------------
def test_a_hook_can_report_its_own_firing(client, db, key):
    """THE reason this router exists. Without it every instrumented hook reports nothing,
    silently, and gets retired for it."""
    rec = _rec(db, tier="hook", status="approved")

    r = client.post(f"/api/artifacts/{rec.id}/used", headers={"X-API-Key": key})
    assert r.status_code == 200 and r.json()["ok"] is True

    assert art_svc.stale_artifacts(db, "core") == [] or rec.id not in [
        x.id for x in art_svc.stale_artifacts(db, "core")]


def test_the_route_the_generated_hook_targets_actually_exists(client, db, key):
    """Pins the CONTRACT between the generated script and the server. The hook swallows its
    own failures by design, so a path that drifts here would never surface — it would just
    look like a hook nobody uses."""
    rec = _rec(db, tier="hook", status="approved")
    path = art_svc.HOOK_TELEMETRY.format(rec_id=rec.id)

    assert f"/api/artifacts/{rec.id}/used" in path
    assert client.post(f"/api/artifacts/{rec.id}/used",
                       headers={"X-API-Key": key}).status_code == 200


def test_reporting_use_needs_a_credential(client, db):
    rec = _rec(db, tier="hook", status="approved")
    assert client.post(f"/api/artifacts/{rec.id}/used").status_code == 401


def test_reporting_use_for_an_unknown_artifact_is_a_404(client, key):
    assert client.post("/api/artifacts/99999/used",
                       headers={"X-API-Key": key}).status_code == 404


# ---- REST ------------------------------------------------------------------------------------
def test_pending_recommendations_are_listable(client, auth, db):
    _rec(db)
    out = client.get("/api/artifacts/recommendations?project_id=core", headers=auth).json()
    assert len(out) == 1 and out[0]["tier"] == "skill"


def test_one_recommendation_carries_its_draft_and_install_plan(client, auth, db):
    """The plan travels WITH the artifact, so a caller cannot render an install button for
    something that may never be written."""
    rec = _rec(db, tier="rule")
    out = client.get(f"/api/artifacts/recommendations/{rec.id}", headers=auth).json()

    assert out["draft"] == "# artifact"
    assert out["install"]["allowed"] is False
    assert "by hand" in out["install"]["reason"]


def test_an_undrafted_recommendation_reports_why_rather_than_erroring(client, auth, db):
    rec = _rec(db, draft="")
    out = client.get(f"/api/artifacts/recommendations/{rec.id}", headers=auth).json()

    assert out["install"]["allowed"] is False and "nothing drafted" in out["install"]["reason"]


def test_approving_and_rejecting_are_recorded(client, auth, db):
    rec = _rec(db)
    assert client.post(f"/api/artifacts/recommendations/{rec.id}/review",
                       json={"decision": "approve"}, headers=auth).json()["status"] == "approved"

    other = _rec(db, title="Another")
    assert client.post(f"/api/artifacts/recommendations/{other.id}/review",
                       json={"decision": "reject"}, headers=auth).json()["status"] == "rejected"


def test_an_unknown_decision_is_refused(client, auth, db):
    rec = _rec(db)
    assert client.post(f"/api/artifacts/recommendations/{rec.id}/review",
                       json={"decision": "maybe"}, headers=auth).status_code == 422


def test_usage_reports_null_for_an_unobservable_tier(client, auth, db):
    """The distinction the whole retirement design rests on, carried to the surface: `uses`
    is null, never 0, for something whose use cannot be observed."""
    _rec(db, tier="rule", status="approved")
    out = client.get("/api/artifacts/usage?project_id=core", headers=auth).json()

    assert out["artifacts"][0]["uses"] is None
    assert out["unmeasurable"] == ["A thing"]


def test_stale_never_includes_an_unobservable_tier(client, auth, db):
    _rec(db, tier="rule", status="approved")
    assert client.get("/api/artifacts/stale?project_id=core", headers=auth).json() == []


# ---- MCP, over the same service functions -------------------------------------------------------
def test_an_agent_can_list_and_approve_end_to_end(client, key, db):
    """THE acceptance criterion: an agent lists pending recommendations and approves one
    end-to-end with a scoped key, through the same functions the UI uses."""
    rec = _rec(db)

    listed = _mcp(client, key, "learning_loop", {"view": "recommendations"})
    assert [r["id"] for r in listed["result"]["recommendations"]] == [rec.id]

    out = _mcp(client, key, "review_recommendation", {"id": rec.id, "decision": "approve"})
    assert out["status"] == "approved"

    # Expire first: the MCP handler commits in its OWN session, so this one would otherwise
    # answer from cache and the test would pass on a response rather than on stored state.
    db.expire_all()
    assert db.get(ArtifactRecommendation, rec.id).status == "approved"


@pytest.mark.parametrize("view", ["recommendations", "usage", "stale"])
def test_every_read_view_round_trips(client, key, db, view):
    _rec(db)
    assert "ERROR" not in _mcp(client, key, "learning_loop", {"view": view})


def test_the_artifact_view_carries_the_install_plan(client, key, db):
    rec = _rec(db, tier="rule")
    out = _mcp(client, key, "learning_loop", {"view": "artifact", "id": rec.id})

    assert out["result"]["install"]["allowed"] is False
    assert out["result"]["draft"] == "# artifact"


def test_an_unknown_artifact_is_not_found(client, key):
    err = _mcp(client, key, "learning_loop", {"view": "artifact", "id": 99999})["ERROR"]
    assert err["code"] == "not_found"


def test_an_unknown_view_is_refused_before_dispatch(client, key):
    err = _mcp(client, key, "learning_loop", {"view": "vibes"})["ERROR"]
    assert err["code"] == "validation"


def test_approving_still_writes_nothing_for_a_shared_target(client, key, db):
    """Approval is a decision, not an install. A `shared_surgery` artifact stays proposed
    however it was approved — the human boundary does not move because an agent said yes."""
    rec = _rec(db, tier="rule")
    _mcp(client, key, "review_recommendation", {"id": rec.id, "decision": "approve"})

    db.expire_all()
    rec = db.get(ArtifactRecommendation, rec.id)
    assert art_svc.install_plan(db, rec)["allowed"] is False
