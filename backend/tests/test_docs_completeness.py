"""A reference document may not silently fall further behind the app (GRPH-475).

Three documents present themselves as complete inventories. Measured 2026-08-22:

    docs/api-reference.md    87 of 160 routes
    docs/configuration.md    19 of  51 settings
    docs/data-model.md       15 of  48 tables

None rotted through carelessness — they were accurate when written, the app grew, and nothing
connected the two. The contrast is the diagnosis: every fact here that IS ratcheted (the tool
count in `docs/mcp.md` and `AGENTS.md`, the migration range, the PRD index) is correct today.
Accuracy in this repo tracks enforcement, not authorship care.

A RATCHET, NOT A GATE, and the distinction is deliberate. Documenting 138 missing facts is not
a thing to demand of whoever next adds a route, so the known gaps are recorded in
`docs/completeness-baseline.json` and this file asserts only that the list never grows. What
it costs the author of a NEW route, setting or table is one line of prose or one deliberate
line in the baseline — and the second is a visible act in a diff, not an accident.

THE BASELINE IS ALSO CHECKED IN THE OTHER DIRECTION, which is what stops it from becoming the
thing it exists to prevent. An entry that has since been documented must be REMOVED, or the
suite fails. Without that, the file becomes a 138-line permanent exemption nobody reads —
`absence reads as clean` wearing the costume of the fix for it.

WHAT THIS CANNOT CATCH, stated because a silent gap is the subject: it checks that a fact is
MENTIONED, never that the mention is right. A route documented with the wrong verb, a setting
with the wrong default, a table with the wrong purpose all pass. That needs reading, not
counting, and pretending otherwise would be the same defect one level up.
"""
import json
import pathlib
import sys

import pytest

REPO = pathlib.Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(REPO / "scripts"))

import docs_completeness as dc  # noqa: E402

BASELINE = json.loads(dc.BASELINE.read_text(encoding="utf-8"))["gaps"]
KINDS = sorted(dc.KINDS)


@pytest.fixture(scope="module")
def current():
    return dc.gaps()


@pytest.mark.parametrize("kind", KINDS)
def test_the_app_actually_has_things_of_this_kind(kind):
    """The control every other assertion rests on. All of them are set differences, and two
    empty sets agree — so an importer that quietly returned nothing would turn this whole
    file green while checking nothing at all."""
    live = dc.KINDS[kind][0]()

    assert len(live) > 10, f"only {len(live)} {kind} found — the extractor is broken, not the docs"


@pytest.mark.parametrize("kind", KINDS)
def test_the_document_still_names_what_it_used_to(kind, current):
    """A route, setting or table that arrives undocumented fails HERE, on the PR that adds it,
    while the person who added it still knows what it does."""
    new = sorted(set(current[kind]) - set(BASELINE[kind]))

    assert not new, (
        f"{dc.DOCS[kind].name} does not mention {new}. Add a line for each, or — if it "
        "genuinely should not be documented — add it to `docs/completeness-baseline.json` "
        "with `scripts/docs_completeness.py --write`, which makes the omission a visible "
        "act in the diff rather than an accident."
    )


@pytest.mark.parametrize("kind", KINDS)
def test_the_baseline_does_not_claim_gaps_that_are_now_closed(kind, current):
    """The direction that keeps the baseline honest.

    A debt list nobody prunes becomes a permanent exemption, which is the failure this file
    exists to prevent, rebuilt inside the fix for it. Documenting something is therefore not
    finished until it leaves the baseline — and the suite says so.
    """
    stale = sorted(set(BASELINE[kind]) - set(current[kind]))

    assert not stale, (
        f"{stale} are documented now but still listed as gaps. Regenerate with "
        "`scripts/docs_completeness.py --write` so the remaining debt is the real one."
    )


@pytest.mark.parametrize("kind", KINDS)
def test_every_permanent_exemption_is_still_a_real_thing(kind):
    """An exemption for something that no longer exists is a claim about nothing. `GIT_SHA`
    being unset one day should surface as a failing assertion, not as a line nobody re-reads."""
    live = dc.KINDS[kind][0]()

    for name, reason in dc.PERMANENT[kind].items():
        assert name in live, f"{name!r} is exempt from {kind} docs but no longer exists"
        assert len(reason) > 15, f"{name!r} is exempt without a stated reason"


def test_the_baseline_covers_exactly_the_kinds_that_are_checked():
    """A kind added to the checker with no baseline entry would raise a KeyError inside the
    tests above and read as an error rather than as a finding."""
    assert sorted(BASELINE) == KINDS


@pytest.mark.parametrize("kind", KINDS)
def test_the_debt_is_visible(kind, current, capsys):
    """Not an assertion about the code — a report. A number nobody prints is a number nobody
    watches, and the whole point is that this one should be falling."""
    live = len(dc.KINDS[kind][0]())
    missing = len(current[kind])
    with capsys.disabled():
        print(f"\n  {dc.DOCS[kind].name}: {live - missing}/{live} documented "
              f"({missing} in the baseline)")
