"""AL-96: PRD sections are classified before they become work.

Found by dogfooding: decompose_prd proposed "Implement: Problem" / "Implement: Goals"
and prd_coverage reported those prose sections as gaps, so a fully-covered PRD read as
half-covered. Framing sections describe the work; they aren't work.
"""
import pytest

from app.services.prds import is_implementable_section

BODY = """## Problem

Users can't do the thing.

## Goals

- Let them do the thing.

## Non-goals (v1)

- Not doing the other thing.

## Widget API

Build the widget endpoints.

## Auditing

Record every widget change.

## Success criteria

- Users do the thing.
"""


@pytest.mark.parametrize("title", [
    "Problem", "Goals", "Non-goals", "Non-goals (v1)", "NON GOALS", "non_goals",
    "Success criteria", "Success Metrics", "Out of scope", "Background", "Context",
    "Overview", "Motivation", "Summary", "Open questions", "Appendix", "Glossary",
    "References", "Prior art",
    # AL-198: planning/risk framing sections must not decompose into "Implement: …" tasks
    "Risks", "Risks and Open Questions", "Risks & Open Questions",
    "Risks and Mitigations", "Phasing", "Phases", "Rollout", "Rollout plan",
    "Milestones", "Timeline", "FAQ",
])
def test_prose_sections_are_not_implementable(title):
    assert is_implementable_section(title) is False


@pytest.mark.parametrize("title", [
    "Widget API", "Auditing", "Registration model", "Admin console",
    "Admin visibility (isolation boundary)", "Platform invites",
    "Additional-org requests & entitlement", "Data model", "Migration",
])
def test_buildable_sections_are_implementable(title):
    assert is_implementable_section(title) is True


@pytest.mark.parametrize("title", [
    "1. Overview", "2. Goals", "7. Non-goals", "8. Risks and open questions",
    "1.2) Background", "10. Open questions", "3. Success criteria",
])
def test_a_numbered_framing_section_is_still_framing(title):
    """The number carries no meaning and used to survive normalisation, which silently
    defeated the whole classification: EVERY PRD in this repo numbers its headings, so
    "1. Overview" keyed as `1overview`, missed the prose set, and read as buildable.

    Two consequences, and the second is the expensive one. `decompose_prd` proposed
    "Implement: 1. Overview" — visible noise a human would catch. But the same predicate feeds
    PRD-12's completeness rollups, so Overview, Goals and Non-goals were counted as sections
    OWING DELIVERY, understating completeness with a number nobody could see was wrong. An
    unnumbered PRD classified correctly, so nothing ever looked broken."""
    assert is_implementable_section(title) is False


@pytest.mark.parametrize("title,key", [
    ("2xx responses", "2xxresponses"),
    ("3D export", "3dexport"),
    ("0-downtime migration", "0downtimemigration"),
])
def test_a_heading_that_genuinely_starts_with_digits_keeps_them(title, key):
    """The strip requires a real separator (`1. ` / `2.1) `), so a section actually named
    after a number is untouched.

    Asserted on the KEY, not on implementability — which is the version that works. An
    over-eager strip renames "2xx responses" to "xx responses", and BOTH are implementable, so
    a check on the classification passes while the section quietly changes what it is about.
    That first draft passed against exactly this sabotage."""
    from app.services.prds import _section_key

    assert _section_key(title) == key
    assert is_implementable_section(title) is True


def _make_prd(client, auth):
    r = client.post("/api/prds", json={"title": "Widget PRD", "body": BODY, "project_id": "core"},
                    headers=auth)
    assert r.status_code == 201, r.text
    return r.json()["id"]


def test_decompose_skips_prose_sections(client, auth):
    """The headline fix: only buildable sections become proposals."""
    prd_id = _make_prd(client, auth)
    proposals = client.post(f"/api/prds/{prd_id}/decompose", headers=auth).json()["proposals"]
    sections = [p["section"] for p in proposals]
    assert sections == ["Widget API", "Auditing"]
    assert not any("Implement: Problem" == p["title"] for p in proposals)


def test_decompose_can_opt_into_prose(client, auth):
    """Escape hatch for a PRD that genuinely uses a framing heading for scope."""
    prd_id = _make_prd(client, auth)
    proposals = client.post(f"/api/prds/{prd_id}/decompose?include_prose=true",
                            headers=auth).json()["proposals"]
    sections = [p["section"] for p in proposals]
    assert "Problem" in sections and "Widget API" in sections


def test_coverage_does_not_report_prose_as_gaps(client, auth):
    prd_id = _make_prd(client, auth)
    cov = client.get(f"/api/prds/{prd_id}/coverage", headers=auth).json()
    assert set(cov["gaps"]) == {"Widget API", "Auditing"}
    assert cov["section_count"] == 6          # every heading, for continuity
    assert cov["implementable_sections"] == 2  # the buildable denominator
    prose = next(s for s in cov["sections"] if s["section"] == "Problem")
    assert prose["implementable"] is False and prose["gap"] is False


def test_full_coverage_reads_as_no_gaps(client, auth):
    """A PRD whose buildable sections are all tracked has zero gaps — previously the
    prose sections kept it looking permanently incomplete."""
    prd_id = _make_prd(client, auth)
    from tests.prd_approve import approve_id
    approve_id(prd_id)
    created = client.post(f"/api/prds/{prd_id}/decompose?create=true", headers=auth).json()["created"]
    assert len(created) == 2
    cov = client.get(f"/api/prds/{prd_id}/coverage", headers=auth).json()
    assert cov["gaps"] == []
    assert cov["sections_with_tasks"] == cov["implementable_sections"] == 2


def test_decompose_create_makes_only_real_tasks(client, auth):
    prd_id = _make_prd(client, auth)
    from tests.prd_approve import approve_id
    approve_id(prd_id)
    client.post(f"/api/prds/{prd_id}/decompose?create=true", headers=auth)
    titles = [i["title"] for i in client.get("/api/items?project_id=core", headers=auth).json()
              if i.get("prd_id") == prd_id]
    assert sorted(titles) == ["Implement: Auditing", "Implement: Widget API"]
