"""G7: the living graph adds no table and no write path (PRD-20).

PRD-20 section 4 states its data model is **None** — every signal the graph renders already
exists, and `presence` is a join rather than a schema. G7 says so as a goal. This says so as
something the suite checks, in the same spirit as `test_wire_name_compat.py` and
`test_infra_identity.py`: an assertion nobody re-derives is one that quietly stops being true.

The whole presence design rests on `AreaReservation` having exactly ONE writer. Section 1.3
argues the glow expires by construction because the lease clock governs it; a second writer
appearing anywhere would invalidate that without any visible failure, because the graph would
still render — it would just be rendering something no lease governs.
"""
import ast
import re
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1]
APP = BACKEND / "app"
FLEET = APP / "services" / "fleet.py"
CODE_GRAPH = APP / "services" / "code_graph.py"

# Everything PRD-20 added on the server. All reads.
PRESENCE_READS = ["held_areas", "area_matches", "hubs", "components", "path", "analysis"]


def _py_files():
    return [p for p in APP.rglob("*.py") if "__pycache__" not in p.parts]


def test_area_reservation_has_exactly_one_construction_site():
    """Assert on write SITES, not on call counts.

    GRPH-380 deliberately changed which CALLERS reach this writer — the all-in-one posture now
    claims through the divvy, so `claim_cluster` runs on installs where it never used to. That
    is the change PRD-20 depends on, and a guard counting calls would have fired on the very
    thing it is meant to permit.
    """
    sites = []
    for path in _py_files():
        for i, line in enumerate(path.read_text().splitlines(), 1):
            if re.search(r"\bAreaReservation\s*\(", line) and "class AreaReservation" not in line:
                sites.append(f"{path.relative_to(BACKEND)}:{i}")
    assert sites == [f"app/services/fleet.py:{_fleet_write_line()}"], (
        f"AreaReservation is constructed in {len(sites)} place(s): {sites}. "
        "PRD-20 section 1.3 rests on there being exactly one writer, so that the lease clock "
        "governs every glow. A second writer makes the graph render presence no lease expires."
    )


def _fleet_write_line() -> int:
    for i, line in enumerate(FLEET.read_text().splitlines(), 1):
        if re.search(r"\bAreaReservation\s*\(", line) and "class" not in line:
            return i
    raise AssertionError("the one AreaReservation writer has disappeared from fleet.py")


def test_the_one_writer_lives_inside_claim_cluster():
    """Not just in the right FILE — in the right function.

    `claim_cluster` writes reservations in the same transaction as the claims that justify
    them. A writer that drifted into a helper could reserve areas nobody claimed, which reads
    identically on the graph and is a lie about the fleet.
    """
    tree = ast.parse(FLEET.read_text())
    holders = [
        node.name
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef)
        and any(
            isinstance(sub, ast.Call)
            and isinstance(sub.func, ast.Name)
            and sub.func.id == "AreaReservation"
            for sub in ast.walk(node)
        )
    ]
    assert holders == ["claim_cluster"], f"AreaReservation is written by {holders}"


def test_presence_and_graph_queries_contain_no_write():
    """Every function PRD-20 added on the server is a read.

    Checked structurally rather than by eye: no `db.add`, `db.delete`, `db.commit`, `db.merge`
    or `db.execute(update/insert/delete)` anywhere inside them. This is the assertion that G7
    ("no new write paths") actually turns on.
    """
    offenders = []
    for src_path in (FLEET, CODE_GRAPH):
        tree = ast.parse(src_path.read_text())
        for node in ast.walk(tree):
            if not isinstance(node, ast.FunctionDef) or node.name not in PRESENCE_READS:
                continue
            for sub in ast.walk(node):
                if not isinstance(sub, ast.Call) or not isinstance(sub.func, ast.Attribute):
                    continue
                target = sub.func
                if not (isinstance(target.value, ast.Name) and target.value.id == "db"):
                    continue
                if target.attr in {"add", "add_all", "delete", "commit", "merge", "flush"}:
                    offenders.append(f"{src_path.name}:{node.name} -> db.{target.attr}")
    assert offenders == [], f"PRD-20 read paths that write: {offenders}"


def test_no_table_was_added_for_presence_or_graph_analysis():
    """The signals PRD-20 renders are joins over rows that already existed."""
    from app.models import Base

    tables = set(Base.metadata.tables)
    for invented in ("presence", "held_areas", "graph_analysis", "code_components",
                     "code_hubs", "node_presence"):
        assert invented not in tables, (
            f"`{invented}` exists — PRD-20 section 4 says its data model is None, and every "
            "signal it renders is a join over rows that already existed."
        )


def test_the_presence_endpoint_is_not_reachable_over_mcp():
    """Privacy, restated as a guard.

    The payload names which HUMAN is editing which file. An agent has `graph_query` for the
    question it actually has. Shipping presence on the MCP surface would put a live map of
    everyone's activity behind every credential in the fleet, and it would be a one-line
    regression to do so.
    """
    from app.mcp_server import TOOLS

    names = {t["name"] for t in TOOLS}
    for leaked in ("fleet_presence", "held_areas", "presence"):
        assert leaked not in names, f"`{leaked}` is on the MCP surface; presence is JWT-only"
