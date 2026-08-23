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
