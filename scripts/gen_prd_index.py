#!/usr/bin/env python3
"""Snapshot each PRD's ledger status and sections into `docs/prd-index.json` (GRPH-424).

A PRD lives twice — in the ledger and in `docs/prd-*.md` — and nothing compared them, so they
drifted. Measured 2026-08-20 across ten PRDs: one agreed, and only because it had been repaired
by hand a week earlier. PRD-17 said `draft` in the repo for eleven days while the ledger had it
`approved`; PRD-19's ledger copy was missing a whole section, so `prd_coverage` reported 100%
while silently omitting the newest slice.

Sections come from `prd_coverage` rather than from parsing a body, deliberately: coverage is
what joins items to sections BY NAME, so recording anything else would be a second definition
of the thing that already drifted once.

WHAT THIS CANNOT DO. The snapshot is only as fresh as the last run. If the ledger moves and
nobody regenerates, the test built on it passes while the repo and the ledger disagree — the
same absence-reads-as-clean shape this whole effort is about. It catches the repo drifting from
the last known ledger state, which is the failure that actually happened three times; it does
not catch the ledger moving underneath. Regenerate when a PRD's status or sections change.

Each doc must carry a `**Ledger id:** GRPH-Pnn` line naming the row it is a copy of. That
line, not the filename, is the join key (GRPH-425).

Usage:  GRAPHBAN_KEY=... [GRAPHBAN_URL=...] scripts/gen_prd_index.py
"""
from __future__ import annotations

import json
import os
import pathlib
import re
import subprocess
import sys
import urllib.request

ROOT = pathlib.Path(__file__).resolve().parent.parent
INDEX = ROOT / "docs" / "prd-index.json"
URL = os.environ.get("GRAPHBAN_URL", "http://ubuntu-srv:8080/api/mcp")
KEY = os.environ.get("GRAPHBAN_KEY", "")


def _tracked_prds() -> list[pathlib.Path]:
    """PRD docs that are IN GIT, asked of git rather than of the filesystem.

    An untracked draft is not part of the repo yet: it exists on one machine, and indexing it
    writes a path that does not resolve in CI or in a fresh clone. Found by running these tests
    in a clean worktree, where two PRDs the fleet had not committed simply were not there and
    the test died on a missing file instead of reporting drift.
    """
    out = subprocess.run(["git", "-C", str(ROOT), "ls-files", "docs/prd-*.md"],
                         capture_output=True, text=True, check=True).stdout.split()
    return sorted(ROOT / f for f in out if PRD_FILE.match(pathlib.Path(f).name))


#: Which FILES are PRD docs. NOT which row each is compared against — that is `_declared_id`.
#:
#: `draft` sits where the number goes, for a document whose row does not exist yet. The
#: selector used to require digits, which meant an unnumbered draft was not a PRD doc as far
#: as this tool was concerned: it would not be indexed, would not be recorded as unindexed,
#: and `test_the_snapshot_accounts_for_every_repo_prd` would pass without ever having seen
#: it. Renaming `prd-22-org-administration-plane.md` to stop it claiming a number the ledger
#: issued elsewhere would have bought a visible collision for an invisible gap.
PRD_FILE = re.compile(r"prd-(\d+|draft)-")

LEDGER_ID = re.compile(r"^\*\*Ledger id:\*\*\s*([A-Za-z][A-Za-z0-9]*-P\d+)", re.M)


def _declared_id(path: pathlib.Path) -> str | None:
    """The ledger key this document CLAIMS, or None if it does not say.

    The filename used to decide this — `prd-22-*.md` was read as `GRPH-P22` — and a filename is
    a guess at a number the repo does not control. Numbering is per-project and issued by the
    ledger (`services/keys.py:mint`), so a document named before its row exists names whatever
    number its author expected. `docs/prd-22-org-administration-plane.md` was written that way
    and the ledger's next number went to a different PRD entirely; the index then bound two
    unrelated documents together, and neither being a draft, the section check never compared
    them and nothing failed.

    So the join key is a claim the document makes and the ledger can confirm, and a document
    that makes no claim is recorded rather than guessed at. The filename is now decoration:
    renaming a file must not change what it is compared against, which is what the regression
    test asserts.
    """
    m = LEDGER_ID.search(path.read_text(encoding="utf-8"))
    return m.group(1) if m else None


class Missing(Exception):
    """The ledger has no such PRD."""


def call(tool: str, args: dict) -> dict:
    req = urllib.request.Request(
        URL,
        data=json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                         "params": {"name": tool, "arguments": args}}).encode(),
        headers={"Content-Type": "application/json", "X-API-Key": KEY},
    )
    with urllib.request.urlopen(req, timeout=60) as r:
        out = json.loads(r.read())
    if "error" in out:
        raise Missing(f"{tool} failed: {out['error']}")
    result = out["result"]
    # A tool-level failure arrives INSIDE result with isError, not as a JSON-RPC error — an
    # unknown PRD lands here, and parsing its message as JSON is how the first run crashed.
    if result.get("isError"):
        raise Missing(result["content"][0]["text"])
    return json.loads(result["content"][0]["text"])


def build(docs, lookup):
    """Pair each document with the ledger row IT DECLARES, and return (index, unindexed).

    Split out of `main` so the pairing rule can be tested without a ledger — `lookup` is the only
    thing here that talks to one. The rule is the part that was wrong, so it is the part that
    needs a test which fails when it changes back; asserting `_declared_id` on its own would not,
    because the old code never called it.
    """
    index: dict[str, dict] = {}
    unindexed: list[str] = []
    for f in docs:
        prd_id = _declared_id(f)
        if prd_id is None:
            # A document that does not name its row cannot be compared to one. Recorded, so a
            # test can see it, rather than paired with whatever the filename suggests.
            unindexed.append(f"docs/{f.name}")
            print(f"  {f.name}: no `**Ledger id:**` line — recorded as unindexed")
            continue
        try:
            cov = lookup(prd_id)
        except Missing:
            # RECORDED, not merely skipped. A repo PRD the ledger has never seen is one nothing
            # compares — writing it down is what lets a test notice when that stops being true,
            # instead of a literal list in the test encoding whatever was true the day it was
            # written.
            unindexed.append(f"docs/{f.name}")
            print(f"  {f.name}: declares {prd_id}, which the ledger does not have — unindexed")
            continue
        index[prd_id] = {
            "file": f"docs/{f.name}",
            "status": cov["status"],
            "sections": [s["section"] for s in cov["sections"]],
        }
        print(f"  {prd_id}: {cov['status']}, {len(index[prd_id]['sections'])} sections")
    return index, unindexed


def main() -> int:
    if not KEY:
        print("GRAPHBAN_KEY is required — this reads the ledger", file=sys.stderr)
        return 2

    # The repo's own files decide which PRDs to check. A ledger-only PRD has no repo copy to
    # disagree with, so it is out of this tool's reach by construction — noted rather than
    # silently skipped. Measured for GRPH-486 on 2026-08-25: 19 PRDs in the ledger, 5 with a
    # repo document, so this file has never been more than a quarter of them.
    index, unindexed = build(_tracked_prds(),
                             lambda pid: call("prd_coverage", {"prd_id": pid}))

    INDEX.write_text(
        json.dumps({
            # THE ARTEFACT SAYS WHAT IT COVERS (GRPH-486). The docstring above has always been
            # clear that a ledger-only PRD is invisible here by construction — and a consumer
            # reads the JSON, not the generator. A changelog tool built its PRD map from this
            # file and reported "166 of 200 PRs reference a ticket no PRD claims"; measured
            # across all the PRDs the real figure was 46 of 114. The inflated number was
            # nearly filed.
            #
            # No ledger total is stated because this tool cannot obtain one — there is no
            # list-PRDs call on the MCP surface — and a number it cannot check is exactly the
            # kind of confident wrong answer this note exists to prevent.
            "scope": {
                "covers": "PRDs with a document in docs/ that names its ledger row",
                "indexed": len(index),
                "note": "NOT the list of PRDs. A ledger-only PRD has no repo copy to compare "
                        "and is absent here by construction; anything reading `prds` as "
                        "complete is seeing a fraction of them.",
            },
            "prds": index,
            "unindexed": unindexed,
        }, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8")
    print(f"wrote {INDEX.relative_to(ROOT)} ({len(index)} indexed, {len(unindexed)} unindexed)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
