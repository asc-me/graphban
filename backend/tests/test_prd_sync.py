"""The repo's PRDs must agree with the ledger's (GRPH-424).

A PRD lives twice — in the ledger and in `docs/prd-*.md` — and nothing compared them. Measured
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
happened three times — not the ledger moving underneath. A ledger-only PRD (there are four) has
no repo copy to disagree with and is invisible here by construction.
"""
import json
import pathlib
import re
import subprocess

import pytest

REPO = pathlib.Path(__file__).resolve().parent.parent.parent
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
    repo = {f for f in repo if re.match(r"prd-\d+", pathlib.Path(f).name)}
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
