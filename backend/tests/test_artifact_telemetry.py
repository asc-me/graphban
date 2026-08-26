"""Usage telemetry and retirement (GRPH-309 / PRD-16).

The measurement gap was settled per-tier on 2026-08-10 rather than uniformly, because the
two halves fail differently: a stale **rule** is clutter, while a stale **hook** still runs,
still costs time, and may still block something.

    skill / agent — first-party signal, already metered by name
    hook          — instrumented AT GENERATION: we render the script, so it reports its own
                    firing rather than us inferring use from transcript silence
    rule          — UNMEASURABLE, accepted. A rule that works is one an agent silently
                    complied with, and compliance leaves no trace by construction

Almost every test here is about the same refusal. PRD-16: *"A fabricated signal here deletes
working hooks."* An artifact that works produces no evidence it works, so absence of a usage
row means "not observed", never "not used" — and only one of those is a reason to delete
something.
"""
from datetime import datetime, timedelta, timezone

import pytest

from app.models import ArtifactRecommendation, ArtifactTombstone, Project
from app.services import artifacts as art_svc
from app.services.platform import Resolved


@pytest.fixture()
def db(client):
    from app.db import SessionLocal

    s = SessionLocal()
    try:
        yield s
    finally:
        s.close()


@pytest.fixture()
def proj(db):
    db.add(Project(id="telemetry", name="Telemetry", tag="TE"))
    db.commit()
    return "telemetry"


def _artifact(db, proj, tier="skill", days_old=90, title="A thing", status="approved"):
    rec = ArtifactRecommendation(project_id=proj, tier=tier, scope=title, title=title,
                                 status=status, draft="# artifact", draft_path=f"p/{title}",
                                 lesson_ids=[])
    rec.created_at = datetime.now(timezone.utc) - timedelta(days=days_old)
    db.add(rec)
    db.commit()
    db.refresh(rec)
    return rec


# ---- what may be measured, and what may not -------------------------------------------------
@pytest.mark.parametrize("tier,expected", [
    ("skill", True), ("agent", True), ("hook", True),
    ("rule", False), ("allowlist", False), ("fact", False),
])
def test_only_observable_tiers_are_measurable(db, proj, tier, expected):
    assert art_svc.measurable(_artifact(db, proj, tier=tier, title=tier)) is expected


def test_an_unmeasurable_artifact_reports_null_uses_not_zero(db, proj):
    """A zero would be a measurement nobody took. "No uses recorded" and "uses cannot be
    recorded" are opposite claims, and the quiet one reads as a reason to delete."""
    _artifact(db, proj, tier="rule", title="A rule")
    row = art_svc.usage_report(db, proj)["artifacts"][0]

    assert row["uses"] is None and row["measurable"] is False
    assert "compliance leaves no trace" in row["reason"]


def test_unmeasurable_artifacts_still_appear_in_the_population(db, proj):
    """They exist and they cost context. A report that omitted them would understate the
    corpus — the point is that they are visible AND excluded from staleness, not hidden."""
    _artifact(db, proj, tier="rule", title="A rule")
    _artifact(db, proj, tier="skill", title="A skill")

    report = art_svc.usage_report(db, proj)
    assert report["population"] == 2 and report["measurable"] == 1
    assert report["unmeasurable"] == ["A rule"]


# ---- staleness never touches what cannot be observed ------------------------------------------
def test_an_unused_measurable_artifact_is_proposed_for_retirement(db, proj):
    stale = art_svc.stale_artifacts(db, proj)
    assert stale == []

    rec = _artifact(db, proj, tier="skill", title="Unused skill")
    assert [r.id for r in art_svc.stale_artifacts(db, proj)] == [rec.id]


def test_an_unused_RULE_is_never_proposed_for_retirement(db, proj):
    """THE acceptance criterion. An unused hook surfaces a retire recommendation; an unused
    rule does not, and reports "not measurable" rather than zero."""
    _artifact(db, proj, tier="rule", title="An old rule")
    assert art_svc.stale_artifacts(db, proj) == []


def test_retiring_an_unmeasurable_artifact_is_refused_outright(db, proj):
    """A hard refusal, not a filter that happens to exclude them. A later refactor dropping
    the sweep's filter must not make it possible to retire a rule by passing it in."""
    rec = _artifact(db, proj, tier="rule", title="A rule")

    with pytest.raises(art_svc.InstallRefused, match="cannot be observed"):
        art_svc.retire(db, rec)


def test_a_recently_installed_artifact_is_not_stale(db, proj):
    """It has not had a chance to be used. Proposing its retirement would train a reviewer
    to dismiss the whole queue."""
    _artifact(db, proj, tier="skill", days_old=1, title="Brand new")
    assert art_svc.stale_artifacts(db, proj) == []


def test_an_observed_use_keeps_an_artifact_alive(db, proj):
    rec = _artifact(db, proj, tier="skill", title="Used skill")
    art_svc.record_use(db, rec.id, signal="skill-invocation")

    assert art_svc.stale_artifacts(db, proj) == []


def test_only_approved_artifacts_are_swept(db, proj):
    """A queued recommendation was never installed, so it cannot have gone unused."""
    _artifact(db, proj, tier="skill", status="queued", title="Never installed")
    assert art_svc.stale_artifacts(db, proj) == []


# ---- hooks report their own firing ---------------------------------------------------------------
def test_a_generated_hook_is_instrumented(db, proj, monkeypatch):
    """What makes hooks measurable when rules cannot be: we render the script, so it can
    report what it did rather than us inferring from silence."""
    from app.services import platform as platform_svc

    class Chat:
        def chat(self, **kw):
            return "#!/bin/sh\necho hello"

    monkeypatch.setattr(platform_svc, "resolve_chat", lambda db, pid: Resolved("openai", Chat()))
    rec = art_svc.draft(db, _artifact(db, proj, tier="hook", title="A hook", status="queued"))

    assert f"/api/artifacts/{rec.id}/used" in rec.draft
    assert "#!/bin/sh" in rec.draft


def test_the_hook_telemetry_cannot_break_the_hook(db, proj, monkeypatch):
    """A hook that fails because telemetry is unreachable is a hook someone deletes by hand
    — which is the outcome this whole feature exists to make deliberate rather than
    accidental."""
    from app.services import platform as platform_svc

    class Chat:
        def chat(self, **kw):
            return "#!/bin/sh\necho hello"

    monkeypatch.setattr(platform_svc, "resolve_chat", lambda db, pid: Resolved("openai", Chat()))
    rec = art_svc.draft(db, _artifact(db, proj, tier="hook", title="A hook", status="queued"))

    assert "|| true" in rec.draft, "telemetry failure must not fail the hook"
    assert "-m 2" in rec.draft, "and must not hang it either"


def test_a_skill_is_not_instrumented(db, proj, monkeypatch):
    """Skills are observable from first-party metering — instrumenting them too would put a
    network call in a file that never needed one."""
    from app.services import platform as platform_svc

    class Chat:
        def chat(self, **kw):
            return "# Skill"

    monkeypatch.setattr(platform_svc, "resolve_chat", lambda db, pid: Resolved("openai", Chat()))
    rec = art_svc.draft(db, _artifact(db, proj, tier="skill", title="A skill",
                                      status="queued"))

    assert "/used" not in rec.draft


# ---- retirement is always reversible ---------------------------------------------------------------
def test_retirement_keeps_the_contents_in_full(db, proj):
    """A retirement that discarded them would make the decision irreversible on the strength
    of a usage count — and a usage count is exactly the kind of evidence that turns out to
    have been measuring the wrong thing."""
    rec = _artifact(db, proj, tier="skill", title="Doomed")
    rec.draft = "# The whole artifact\n\nWith all of its content."
    db.commit()

    stone = art_svc.retire(db, rec)
    assert stone.contents == "# The whole artifact\n\nWith all of its content."
    assert stone.path == rec.draft_path and stone.use_count == 0


def test_a_retired_artifact_can_be_restored_in_one_step(db, proj):
    rec = _artifact(db, proj, tier="skill", title="Doomed")
    stone = art_svc.retire(db, rec)

    restored = art_svc.restore(db, stone)
    assert restored["contents"] == rec.draft and restored["path"] == rec.draft_path
    assert db.get(ArtifactRecommendation, rec.id).status == "approved"


def test_the_tombstone_records_why_and_how_much_it_was_used(db, proj):
    rec = _artifact(db, proj, tier="skill", title="Barely used")
    art_svc.record_use(db, rec.id, signal="skill-invocation")

    stone = art_svc.retire(db, rec, reason="superseded by a broader skill")
    assert stone.use_count == 1 and "superseded" in stone.reason


def test_nothing_is_hard_deleted(db, proj):
    rec = _artifact(db, proj, tier="skill", title="Doomed")
    art_svc.retire(db, rec)

    assert db.get(ArtifactRecommendation, rec.id) is not None
    assert db.query(ArtifactTombstone).count() == 1
