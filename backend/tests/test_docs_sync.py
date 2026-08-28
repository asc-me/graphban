"""AL-46: code is the single owner of the tool catalog; docs must not silently
drift (review finding F5). These ratchets fail the moment docs/mcp.md and the
live TOOLS list disagree — the durable fix, not a one-time sweep.

Extended to AGENTS.md's migration range, which had drifted to "0001-0016" while the
chain reached 0044. The tool count in the SAME file stayed correct the whole time
because a ratchet guarded it and nothing guarded the range — which is the argument for
ratchets over sweeps, made by accident.
"""
import re
import subprocess
from pathlib import Path

from app.mcp_server import LIVE_TOOL_COUNT, TOOLS

_REPO = Path(__file__).resolve().parents[2]
_DOCS = _REPO / "docs" / "mcp.md"
_AGENTS = _REPO / "AGENTS.md"
_MIGRATIONS = _REPO / "backend" / "alembic" / "versions"


def _doc_tool_rows() -> list[str]:
    """Tool names from the `| `name` | ... |` table under the 'The N tools'
    heading (only that section — other backtick tables exist in the file)."""
    names, in_section = [], False
    for line in _DOCS.read_text().splitlines():
        if re.match(r"##\s+The \d+ tools", line):
            in_section = True
            continue
        if in_section:
            m = re.match(r"\|\s*`([a-z_]+)`\s*\|", line)
            if m:
                names.append(m.group(1))
            elif names and not line.startswith("|"):
                break  # the (contiguous) tool table has ended
    return names


def test_docs_table_covers_every_tool_exactly_once():
    doc_names = _doc_tool_rows()
    code_names = [t["name"] for t in TOOLS]
    assert set(doc_names) == set(code_names), (
        f"docs/mcp.md drift — only in code: {set(code_names) - set(doc_names)}; "
        f"only in docs: {set(doc_names) - set(code_names)}"
    )
    assert len(doc_names) == LIVE_TOOL_COUNT


def test_docs_heading_states_the_live_count():
    assert f"## The {LIVE_TOOL_COUNT} tools" in _DOCS.read_text(), (
        f"the 'The N tools' heading in docs/mcp.md must say {LIVE_TOOL_COUNT}"
    )


def test_agents_md_states_the_live_tool_count():
    """The same guard as `test_docs_heading_states_the_live_count`, pointed at the other file
    that states the number.

    Only `docs/mcp.md` was checked, so AGENTS.md drifted to **36** while the live manifest had
    53 — and AGENTS.md is the file every agent is told to read first, so it is the worse of the
    two to be wrong. Found while adding a tool, not by anything that was watching (GRPH-430)."""
    match = re.search(r"(\d+) MCP tools", _AGENTS.read_text())
    assert match, "AGENTS.md must state the tool count as `<N> MCP tools`"
    assert int(match.group(1)) == LIVE_TOOL_COUNT, (
        f"AGENTS.md says {match.group(1)} MCP tools, but the manifest ships {LIVE_TOOL_COUNT}"
    )


def test_mcp_enums_reference_service_constants():
    # The schema must reuse the service-owned enums, not inline copies.
    from app.services import links as links_svc
    from app.services import requests as req_svc

    by_name = {t["name"]: t for t in TOOLS}
    link_enum = by_name["link_items"]["inputSchema"]["properties"]["type"]["enum"]
    req_enum = by_name["report_graphban_issue"]["inputSchema"]["properties"]["type"]["enum"]
    assert link_enum == links_svc.LINK_TYPES
    assert req_enum == req_svc.REQUEST_TYPES


def test_agents_md_states_the_live_migration_range():
    """AGENTS.md orients every agent, so a stale range there sends someone to edit an
    applied migration — the one thing that section forbids."""
    head = max(p.name.split("_", 1)[0] for p in _MIGRATIONS.glob("[0-9]*.py"))
    match = re.search(r"currently 0001[–-](\d+)", _AGENTS.read_text())
    assert match, "AGENTS.md must state the migration range as `currently 0001-<head>`"
    assert match.group(1) == head, (
        f"AGENTS.md says migrations run to {match.group(1)}, but the chain head is {head}"
    )


# ---- the claim, not the file (GRPH-467) -----------------------------------------------------
#
# The two ratchets above name `docs/mcp.md` and `AGENTS.md`. That is how they were built: only
# mcp.md was checked, AGENTS.md drifted to 36, and the guard was extended to the file that had
# drifted. Meanwhile three OTHER documents were already stating a count and none was checked —
# ARCHITECTURE.md and product-overview.md said 27, docs/README.md said 30, against a live 54.
#
# So this ratchets the CLAIM wherever it appears. A new document that states a tool count is
# covered on arrival rather than when somebody notices it is wrong.

# Documents that record a MOMENT rather than the present. Their counts are dated measurements
# and correct as history — `acceptance-prd20.md` says 53 because 53 is what was shipped that
# day. A sweep that "fixed" them would falsify the record to make a test pass, which is worse
# than the drift this file exists to catch.
_HISTORICAL = ("docs/acceptance-", "docs/prd-", "docs/IMPLEMENTATION_PLAN.md", "docs/spikes/")

_COUNT_CLAIM = re.compile(r"\b(\d{1,3})\s+(?:MCP\s+)?tools\b", re.I)


def _tracked_markdown() -> list[Path]:
    out = subprocess.run(["git", "-C", str(_REPO), "ls-files", "*.md"],
                         capture_output=True, text=True, check=True).stdout.split()
    return [_REPO / f for f in out if not f.startswith(_HISTORICAL)]


def test_every_document_that_states_a_tool_count_states_the_live_one():
    """Asked of every tracked document, because the failure was never about which file — it
    was that only two files were asked."""
    wrong = []
    for path in _tracked_markdown():
        for i, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            for m in _COUNT_CLAIM.finditer(line):
                if int(m.group(1)) != LIVE_TOOL_COUNT:
                    rel = path.relative_to(_REPO)
                    wrong.append(f"{rel}:{i} says {m.group(1)}, manifest ships {LIVE_TOOL_COUNT}")

    assert not wrong, "stale tool counts: " + "; ".join(wrong)


def test_the_sweep_actually_reads_the_documents():
    """The control. Every assertion above is "no matches found", which is also what a broken
    file list or a regex that matches nothing produces."""
    docs = _tracked_markdown()

    assert len(docs) > 15, f"only {len(docs)} documents in scope — the file list is broken"
    hits = [p for p in docs if _COUNT_CLAIM.search(p.read_text(encoding="utf-8"))]
    assert len(hits) >= 3, (
        f"only {len(hits)} documents state a tool count — if the claim has genuinely been "
        "removed everywhere, delete this ratchet rather than leaving it asserting nothing"
    )


def test_the_historical_exemption_is_still_earned():
    """An exemption that stops being necessary is a lie in the other direction. If no excluded
    document states an out-of-date count any more, the exclusion should go."""
    stale_in_history = []
    out = subprocess.run(["git", "-C", str(_REPO), "ls-files", "*.md"],
                         capture_output=True, text=True, check=True).stdout.split()
    for f in out:
        if not f.startswith(_HISTORICAL):
            continue
        for m in _COUNT_CLAIM.finditer((_REPO / f).read_text(encoding="utf-8")):
            if int(m.group(1)) != LIVE_TOOL_COUNT:
                stale_in_history.append(f)
                break

    assert stale_in_history, (
        "no excluded document records a count that differs from today's — the historical "
        "exemption is protecting nothing and should be removed"
    )


# ---- no count that nothing keeps true (GRPH-558) ------------------------------------

#: Cardinal numbers as words, because the census was written that way — "Nineteen PRDs exist;
#: five have a repo document". A digits-only guard would have missed every figure in it.
_NUM = (
    r"\d+|one|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve|thirteen|fourteen|"
    r"fifteen|sixteen|seventeen|eighteen|nineteen|twenty|thirty|forty|fifty"
)

#: A COUNT OF PRDs, not any number. `one` and `two` occur constantly in ordinary prose here —
#: "the one direction a test cannot fix", "Two consequences worth knowing", "eleven days" — and
#: a guard that flagged those would be one someone deletes rather than obeys. Two shapes, both
#: taken from the census this replaces:
#:
#:   "Nineteen PRDs exist" / "five have a repo document"  -> a number, then the thing counted
#:   "Eight of the fourteen" / "5 of 19"                  -> a ratio, which needs no noun
#:
#: `one` and `two` are excluded from THIS pattern (they stay in the ratio one below). They are
#: pronouns here far more often than quantities — "Write one when a spec is worth reviewing in
#: a diff" is the sentence that proved it — and no plausible census of PRDs reads "one PRD
#: exists". Excluding them costs nothing and is what keeps the guard obeyable.
_QUANTITY = _NUM.replace("one|two|", "").replace("|one|", "|")
_COUNTS_PRDS = re.compile(
    rf"\b({_QUANTITY})\b(?:\s+\w+){{0,3}}\s+(PRDs?|documents?|specs?|copies)\b", re.I)
_RATIO = re.compile(rf"\b({_NUM})\s+of\s+(?:the\s+)?({_NUM})\b", re.I)


def _prd_section() -> str:
    text = _AGENTS.read_text(encoding="utf-8")
    start = text.index("## PRDs live in the ledger")
    end = text.index("\n## ", start + 1)
    return text[start:end]


def test_the_prd_section_states_no_census():
    """AGENTS.md must not count PRDs (GRPH-558).

    It used to. Written 2026-08-25 — *"Nineteen PRDs exist; five have a repo document. Eight of
    the fourteen without one are past `draft`"* — and re-measured two days later **every figure
    was wrong**: 21, 5, 16, 10. Nothing was done badly; PRDs are created faster than a
    hand-typed census is revisited.

    **The other option was to generate the figure, and it is not available.** The tests above
    keep the tool count and the migration range honest because the manifest and the Alembic
    chain are readable from the app, offline. A PRD census lives in the LEDGER, and nothing in
    CI can reach it. So a number here could only ever be re-typed, which restores it for about
    two days — and this repository has already carried the MCP tool count as three different
    values in three places simultaneously.

    Scoped to counts OF PRDS rather than to numbers, deliberately. `one` and `two` are ordinary
    words in this section, and a guard that tripped on "the one direction a test cannot fix"
    would be one the next person deletes instead of obeying.
    """
    section = _prd_section()
    scrubbed = re.sub(r"\b(?:PRD|GRPH|AC|PR)-\d+\b", "", section)   # identifiers name, not count

    counted = [m.group(0) for m in _COUNTS_PRDS.finditer(scrubbed)]
    ratios = [m.group(0) for m in _RATIO.finditer(scrubbed)]

    assert not counted, (
        f"AGENTS.md's PRD section counts PRDs: {counted}. A count here has nothing keeping it "
        "true, and the last one was wrong within two days (GRPH-558)")
    assert not ratios, (
        f"AGENTS.md's PRD section states the ratio(s) {ratios} — the same drift one shape over; "
        "`docs/prd-index.json` said '5 of 19' and the denominator had already moved")


def test_the_prd_section_still_states_the_rule():
    """Dropping the numbers must not drop the point. The section's value is the DECISION — the
    ledger is the source of truth, a repo copy is optional — and a guard that only forbids
    counts would be satisfied by deleting the section entirely."""
    section = _prd_section()

    assert "source of truth" in section
    assert "optional" in section
    assert "test_prd_sync.py" in section, (
        "the section no longer says what IS enforced, which is the half a reader acts on")
    assert "GRPH-465" in section, "the record of the dropped both-copies expectation is gone"
