"""The repo's PRDs must agree with the ledger's (GRPH-424).

**The ledger is where a PRD lives. A `docs/prd-*.md` copy is optional** — AGENTS.md, "PRDs
live in the ledger", settles that and GRPH-465 is the decision. So this file checks ONE
direction: a repo copy must exist in the ledger and agree with it.

That is the direction where drift is silent and expensive, and nothing compared them. Measured
2026-08-20 across ten: ONE agreed, and only because it had been repaired by hand a week before.

- PRD-17 said `draft` in the repo for eleven days while the ledger had it `approved` — through
  the whole D1-D9 build, the acceptance walk, and nine defects found and fixed against it.
- PRD-19's ledger copy was missing its E9 section entirely, so `prd_coverage` reported **100%**
  while omitting the newest slice and orphaning the item filed against it. That number is what
  a human reads to decide a PRD is finished.

Every one of those was found by accident, weeks later, by somebody doing something else.

**WHAT THIS CANNOT CATCH, stated because a silent gap is the thing being fixed.** The snapshot
in `docs/prd-index.json` is only as fresh as the last `scripts/gen_prd_index.py` run. If the
ledger moves and nobody regenerates, these tests pass while repo and ledger disagree. What they
catch is the repo drifting from the last known ledger state, which is the failure that actually
happened three times — not the ledger moving underneath.

A ledger-only PRD has no repo copy to disagree with and is invisible here BY DESIGN rather than
by omission — that is the rule, not a gap in it. There are fourteen, eight of them past `draft`
(measured 2026-08-25). The previous version of this sentence said "there are four", which was
true on 2026-08-22 and is the exact species of drift this file exists to catch, in the one
place it cannot: a count written in prose.
"""
import json
import pathlib
import re
import subprocess
import sys
import tempfile

import pytest

REPO = pathlib.Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(REPO / "scripts"))

import gen_prd_index  # noqa: E402

SNAPSHOT = json.loads((REPO / "docs" / "prd-index.json").read_text(encoding="utf-8"))
INDEX = SNAPSHOT["prds"]

# PRD-17's two copies are structurally different documents — 35 headings in the repo against 18
# sections in the ledger, numbered sections plus D-slices versus a flatter shape. Reconciling
# them is not a text edit: `prd_coverage` joins items to sections BY NAME, so rewriting either
# body detaches every PRD-17 item from its section and turns a real coverage number into a
# wrong one. Named here with its reason rather than quietly excluded, so the exemption is a
# reviewable act and not a hole. Tracked by GRPH-424.
KNOWN_BODY_DIVERGENCE = {"GRPH-P17"}


def _status_line(path: pathlib.Path) -> str:
    m = re.search(r"^\*\*Status:\*\*\s*\**\s*(\w+)", path.read_text(encoding="utf-8"), re.M)
    return m.group(1).lower() if m else ""


def _headings(path: pathlib.Path) -> list[str]:
    return [h.strip() for h in
            re.findall(r"^##\s+(.+?)\s*$", path.read_text(encoding="utf-8"), re.M)]


@pytest.mark.parametrize("prd_id", sorted(INDEX))
def test_the_repo_status_line_matches_the_ledger(prd_id):
    """The ledger owns status: it is EARNED by finishing the grill and cannot be set directly
    (the API returns 409 for the attempt). The repo copy is what an implementer reads, so a
    stale line there sends someone to build against a document that says `draft`."""
    entry = INDEX[prd_id]
    path = REPO / entry["file"]

    assert _status_line(path) == entry["status"], (
        f"{entry['file']} says {_status_line(path)!r}; the ledger says {entry['status']!r}. "
        "The ledger is authoritative for status — fix the doc, not the ledger."
    )


@pytest.mark.parametrize("prd_id", sorted(
    {k for k, v in INDEX.items() if v["status"] != "draft"} - KNOWN_BODY_DIVERGENCE))
def test_every_ledger_section_exists_in_the_repo_doc(prd_id):
    """The overlap that MUST agree, because it is what `prd_coverage` joins on. A section in
    the ledger with no matching heading in the repo is a coverage number computed against a
    document nobody reviewed — which is precisely how PRD-19 reported 100% while missing E9.

    One-directional on purpose: the repo may carry prose headings the ledger's synopsis does
    not, and that costs nothing. It is the ledger's sections that items are filed against.

    DRAFTS ARE EXCLUDED, and that is a rule rather than an exemption: a draft's body is in flux
    by definition, and nothing is filed against its sections yet. PRD-22 was written into the
    ledger while its repo draft was still being typed — the first version of this test failed
    on it within the hour, which is the right signal aimed at the wrong moment. The check earns
    its keep from the transition to `review`, when items start joining to sections and somebody
    starts reading a coverage number."""
    entry = INDEX[prd_id]
    headings = set(_headings(REPO / entry["file"]))

    missing = [s for s in entry["sections"] if s not in headings]

    assert not missing, (
        f"{entry['file']} is missing sections the ledger has: {missing}. Items filed against "
        "them join to nothing, so coverage counts a slice that the repo cannot show."
    )


def test_the_snapshot_accounts_for_every_repo_prd():
    """Every `docs/prd-*.md` is either indexed or explicitly recorded as absent from the ledger.

    Asserted against what the GENERATOR recorded, never a literal list: the first version of
    this test hardcoded `[22, 24]` and PRD-22 was filed into the ledger minutes later, so the
    assertion encoded a fact with a shelf life of an hour. A new PRD file now fails here until
    somebody regenerates, which is the intent — an unaccounted PRD is one nothing checks.

    Asked of GIT, not the filesystem: an untracked draft exists on one machine and is not part
    of the repo. The first version globbed `docs/` and indexed two PRDs the fleet had not
    committed, so in a clean checkout the file it named was simply absent."""
    repo = set(subprocess.run(["git", "-C", str(REPO), "ls-files", "docs/prd-*.md"],
                              capture_output=True, text=True, check=True).stdout.split())
    repo = {f for f in repo if gen_prd_index.PRD_FILE.match(pathlib.Path(f).name)}
    accounted = {v["file"] for v in INDEX.values()} | set(SNAPSHOT["unindexed"])

    assert repo == accounted, (
        f"unaccounted: {sorted(repo - accounted)}; stale entries: {sorted(accounted - repo)}. "
        "Regenerate with scripts/gen_prd_index.py"
    )


def test_the_known_divergence_is_still_real():
    """An exemption that quietly becomes unnecessary is a lie in the other direction. If
    PRD-17's copies ever agree, this fails and the exemption comes out."""
    entry = INDEX["GRPH-P17"]
    headings = set(_headings(REPO / entry["file"]))

    missing = [s for s in entry["sections"] if s not in headings]

    assert missing, (
        "GRPH-P17's bodies now agree — delete it from KNOWN_BODY_DIVERGENCE so the real check "
        "applies to it."
    )


# ---- the join key (GRPH-425) ----------------------------------------------------------------
#
# The index used to pair a document with a ledger row by the digits in its FILENAME. That is not
# the repo's to choose: numbering is per-project and issued by the ledger, so a document named
# before its row exists carries whatever number its author expected.
# `docs/prd-22-org-administration-plane.md` was named that way while the ledger issued 22 to the
# fleet supervisor PRD, and the index bound the two together. Neither was past `draft`, so the
# section check never compared them and nothing failed — the gap this whole file exists to close,
# reappearing inside the tool built to close it.


def test_the_filename_is_not_the_join_key():
    """Written against a file whose name and declaration DISAGREE, because live data cannot tell
    the two rules apart: every committed PRD's filename happens to match its id, so an assertion
    over `docs/` passes under the old generator and proves nothing."""
    with tempfile.TemporaryDirectory() as d:
        doc = pathlib.Path(d) / "prd-99-a-number-nobody-issued.md"
        doc.write_text("# PRD-99 — misnamed\n\n**Ledger id:** GRPH-P17\n", encoding="utf-8")

        assert gen_prd_index._declared_id(doc) == "GRPH-P17"


def test_a_document_that_declares_nothing_is_not_guessed_at():
    """No declaration means no pair. The old code fell back to the filename here, which is how a
    document the ledger had never seen became a confident mis-pairing rather than an unindexed
    row somebody would have noticed."""
    with tempfile.TemporaryDirectory() as d:
        doc = pathlib.Path(d) / "prd-17-looks-familiar.md"
        doc.write_text("# PRD-17 — no declaration\n\n**Status:** draft\n", encoding="utf-8")

        assert gen_prd_index._declared_id(doc) is None


@pytest.mark.parametrize("prd_id", sorted(INDEX))
def test_every_indexed_doc_declares_the_id_it_is_indexed_under(prd_id):
    """The integration half: the snapshot's pairing agrees with the documents themselves, so a
    regenerated index that quietly re-paired something fails here rather than at `review`."""
    entry = INDEX[prd_id]
    declared = gen_prd_index._declared_id(REPO / entry["file"])

    assert declared == prd_id, (
        f"{entry['file']} declares {declared!r} but the snapshot indexes it under {prd_id!r}. "
        "Regenerate with scripts/gen_prd_index.py, or fix the doc's `**Ledger id:**` line."
    )


def test_the_pairing_asks_the_document_not_the_filename():
    """The discriminating test: `build` is handed a file whose NAME says 99 and whose DECLARATION
    says GRPH-P17, and the ledger must be asked for GRPH-P17.

    Testing `_declared_id` alone would not catch a regression — the old generator could keep that
    helper and go on pairing by filename, and every assertion over `docs/` would still pass,
    because every committed PRD's name happens to match its id. This one fails."""
    with tempfile.TemporaryDirectory() as d:
        doc = pathlib.Path(d) / "prd-99-a-number-nobody-issued.md"
        doc.write_text("# PRD-99 — misnamed\n\n**Ledger id:** GRPH-P17\n", encoding="utf-8")
        asked = []

        def lookup(prd_id):
            asked.append(prd_id)
            return {"status": "approved", "sections": [{"section": "Overview"}]}

        index, unindexed = gen_prd_index.build([doc], lookup)

    assert asked == ["GRPH-P17"], f"the ledger was asked for {asked}, so the filename decided it"
    assert set(index) == {"GRPH-P17"} and not unindexed


def test_an_unnumbered_draft_is_still_a_prd_doc():
    """A draft whose number the ledger has not issued must still be SELECTED, or renaming it
    to stop claiming a number quietly removes it from every check in this file.

    `docs/prd-22-org-administration-plane.md` claimed 22 while the ledger issued 22 to the
    fleet supervisor. The honest name carries no number — PRD-21 D9 has already reserved 23
    for integrations and 24 for analytics, so this document's number is whatever `create_prd`
    returns and not a digit sooner. Under a digits-only selector that rename would have made
    the file invisible: not indexed, not recorded as unindexed, and
    `test_the_snapshot_accounts_for_every_repo_prd` green either way.

    So the selector takes `draft` where the number goes, and a document that names no row
    lands in `unindexed` where a human is asked about it."""
    assert gen_prd_index.PRD_FILE.match("prd-draft-org-administration-plane.md")
    assert gen_prd_index.PRD_FILE.match("prd-22-fleet-supervisor.md")
    assert not gen_prd_index.PRD_FILE.match("prd-index.json")
    assert not gen_prd_index.PRD_FILE.match("prd-notes.md"), (
        "the slot is for a number or the literal `draft`, not for any word at all — "
        "otherwise every prd-*.md becomes a PRD document"
    )

    with tempfile.TemporaryDirectory() as d:
        doc = pathlib.Path(d) / "prd-draft-org-administration-plane.md"
        doc.write_text("# PRD — not filed yet\n\n**Status:** draft\n", encoding="utf-8")

        index, unindexed = gen_prd_index.build([doc], lambda _: pytest.fail(
            "the ledger was asked about a document that declares no row"))

    assert index == {} and unindexed == ["docs/prd-draft-org-administration-plane.md"]


def test_a_declared_id_the_ledger_does_not_have_is_recorded_not_paired():
    """A claim the ledger cannot confirm is not a pairing. It lands in `unindexed`, where
    `test_the_snapshot_accounts_for_every_repo_prd` can see it."""
    with tempfile.TemporaryDirectory() as d:
        doc = pathlib.Path(d) / "prd-40-not-filed-yet.md"
        doc.write_text("# PRD-40\n\n**Ledger id:** GRPH-P40\n", encoding="utf-8")

        def lookup(prd_id):
            raise gen_prd_index.Missing("no such prd")

        index, unindexed = gen_prd_index.build([doc], lookup)

    assert index == {} and unindexed == ["docs/prd-40-not-filed-yet.md"]
