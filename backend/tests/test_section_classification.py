"""A section can say what it is, instead of being guessed at by name (GRPH-247).

`_PROSE_SECTIONS` is an allowlist of exact heading names. Three sections on this repo's own
approved PRD-17 — `3. Key decisions`, `4. Roles`, `9. Relationship to in-session
orchestrators` — are pure framing and are not on it, so `prd_coverage` reported each as a gap
that could never close. Two of them already carried hand-retitled "Spec:" items created only
to silence the report.

That is the AL-96 failure surviving in headings nobody thought to add, and it recurs by
construction: **an allowlist cannot keep up with prose sections people invent.**

**The alternative was measured, not assumed.** The ticket proposed inverting the default —
treat a section with no acceptance markers as framing unless it says otherwise. Run against
the PRDs in `docs/`, that reclassifies 30 sections, and most of them (`D2 — Pan, zoom, drag,
find`, `E1`..`E8` of PRD-19) are genuinely buildable. It trades a *visible* false gap for an
*invisible* missing one, which is strictly worse: a gap nobody can close is annoying; work
that quietly stopped being counted is the defect this repo keeps rediscovering.

So the author declares it, and the declaration beats the guess in both directions.
"""
from __future__ import annotations

import pathlib
import re

import pytest

from app.services.prds import (
    _PROSE_SECTIONS,
    classify_section,
    is_implementable_section,
    parse_sections,
    section_bodies,
)

DOCS = pathlib.Path(__file__).resolve().parents[2] / "docs"


# ── the declaration wins ──────────────────────────────────────────────────────

def test_a_marker_makes_an_unlisted_heading_framing():
    """The case the allowlist could not reach: a heading nobody predicted."""
    ok, basis = classify_section("Relationship to in-session orchestrators",
                                 "<!-- framing -->\n\nconstraints on D6/D7.")
    assert ok is False
    assert "framing" in basis


def test_a_marker_makes_a_listed_heading_buildable():
    """The reverse, and it is not symmetry for its own sake. `decisionsfromgrilling` is on
    the allowlist while plain "Decisions" is deliberately not, because that one may be design
    decisions that DO need building. An author whose framing-named section holds real work
    needs a way to say so without editing the allowlist."""
    ok, basis = classify_section("Decisions from grilling",
                                 "<!-- buildable -->\n\nthese ones are real work.")
    assert ok is True
    assert "buildable" in basis


def test_framing_beats_buildable_when_a_section_carries_both():
    """Contradictory markers are an authoring mistake, and the safe reading is the one that
    does not invent work: proposing `Implement: Problem` is the AL-96 failure itself."""
    ok, _ = classify_section("Anything", "<!-- buildable --> <!-- framing -->")
    assert ok is False


@pytest.mark.parametrize("marker", ["<!--framing-->", "<!--  framing  -->", "<!-- FRAMING -->"])
def test_the_marker_tolerates_how_people_actually_type_it(marker):
    """A marker that only works when spaced exactly right is a marker that silently does
    nothing — and doing nothing looks identical to being absent."""
    assert classify_section("Whatever", marker)[0] is False


def test_a_marker_inside_prose_still_counts():
    """No positional rule. Requiring it on the first line is another thing to get wrong
    quietly."""
    assert classify_section("Whatever", "Some text.\n\n<!-- framing -->\n\nMore.")[0] is False


# ── without a marker, nothing changed ─────────────────────────────────────────

def test_the_name_still_decides_when_nothing_is_declared():
    assert is_implementable_section("Problem", "some prose") is False
    assert is_implementable_section("Sync engine", "some prose") is True


def test_the_basis_names_which_rule_fired():
    """A false gap is found by somebody disbelieving a coverage report, and their next
    question is always "why does it think that" — which used to mean opening the allowlist
    and guessing."""
    assert classify_section("Problem", "")[1] == "a conventional framing heading"
    assert "not a conventional" in classify_section("Sync engine", "")[1]


def test_an_empty_body_is_not_a_marker():
    """The degenerate case: a section with nothing in it falls through to its name, and is
    not quietly treated as having declared either way."""
    assert classify_section("Problem", "") == (False, "a conventional framing heading")
    assert classify_section("Sync engine", "")[0] is True
    # The allowlist is keyed on the NORMALISED heading, which is why a test comparing raw
    # titles against it would pass without checking anything.
    assert "problem" in _PROSE_SECTIONS and "Problem" not in _PROSE_SECTIONS


# ── the live PRDs the ticket was opened about ─────────────────────────────────

@pytest.mark.parametrize("title", ["3. Key decisions", "4. Roles",
                                   "9. Relationship to in-session orchestrators"])
def test_prd_17s_framing_sections_are_no_longer_false_gaps(title):
    """The three named on the ticket, asserted against the real document rather than a
    fixture — a mechanism nobody applied fixes nothing, and these had been reporting as
    permanent gaps."""
    body = (DOCS / "prd-17-fleet-roles.md").read_text()
    bodies = section_bodies(body)
    assert title in bodies, f"{title} is no longer a section — update this test"
    ok, basis = classify_section(title, bodies[title])
    assert ok is False, f"{title} still reads as buildable work"
    assert "marked" in basis


def test_marking_those_sections_did_not_silence_the_real_work():
    """The complement, and the one that matters: a marker pass that quietly reclassified
    D-sections would look like a fixed coverage report and BE work that stopped being
    counted."""
    body = (DOCS / "prd-17-fleet-roles.md").read_text()
    bodies = section_bodies(body)
    buildable = [t for t in parse_sections(body) if classify_section(t, bodies[t])[0]]
    assert any(t.startswith("6.") or "D" in t for t in buildable), buildable
    assert len(buildable) >= 4, f"only {len(buildable)} buildable sections left: {buildable}"


def test_no_repo_prd_carries_a_marker_that_does_nothing():
    """Catches the typo class — `<!-- framing–>`, `<!--framing >` — where the author
    believes they declared something and the classifier never saw it."""
    suspicious = re.compile(r"<!--[^>]*\bframing\b[^>]*-->", re.IGNORECASE)
    good = re.compile(r"<!--\s*framing\s*-->", re.IGNORECASE)
    for path in sorted(DOCS.glob("prd-*.md")):
        for m in suspicious.finditer(path.read_text()):
            # A comment that merely mentions the word is fine; one that looks like the
            # marker and is not exactly it is the failure being caught.
            if m.group(0).strip("<!->").strip().lower() == "framing":
                assert good.fullmatch(m.group(0)), f"{path.name}: {m.group(0)!r} is not the marker"


# ── through the surface that reports it ───────────────────────────────────────

def test_coverage_honours_the_marker_and_says_it_did(client, auth):
    """The pure classifier can be perfect and never be consulted. Both of these survived the
    first sabotage pass: `coverage` passing `""` for the body, and the basis being dropped
    from the payload — the marker worked everywhere except the one report it exists to fix.

    That is the third time in three tickets that a mechanism was right and its reporting
    surface was untested (GRPH-360's `section_gone`, GRPH-534's ownership check).
    """
    prd = client.post("/api/prds", json={
        "title": "Marked",
        "body": ("# Marked\n\n"
                 "## Ingest\n\nread the feed\n\n"
                 "## Ground rules\n\n<!-- framing -->\n\nvocabulary only.\n"),
    }, headers=auth).json()
    cov = client.get(f"/api/prds/{prd['id']}/coverage", headers=auth).json()
    by = {s["section"]: s for s in cov["sections"]}

    assert by["Ground rules"]["implementable"] is False
    assert by["Ground rules"]["gap"] is False, "a framing section can never be a gap"
    assert "marked" in by["Ground rules"]["implementable_basis"]

    # And the ordinary section is untouched — a marker pass that silenced everything would
    # look like a clean coverage report and BE work that stopped being counted.
    assert by["Ingest"]["implementable"] is True
    assert by["Ingest"]["gap"] is True
    assert "not a conventional" in by["Ingest"]["implementable_basis"]


def test_decompose_does_not_propose_work_for_a_marked_section(client, auth):
    """The other consumer, and the one that produces the artefact people complain about:
    `Implement: Ground rules` is the AL-96 failure in its original form."""
    prd = client.post("/api/prds", json={
        "title": "Marked2",
        "body": ("# Marked2\n\n"
                 "## Ingest\n\nread the feed\n\n"
                 "## Ground rules\n\n<!-- framing -->\n\nvocabulary only.\n"),
    }, headers=auth).json()
    dry = client.post(f"/api/prds/{prd['id']}/decompose", headers=auth).json()
    assert [p["section"] for p in dry["proposals"]] == ["Ingest"]
