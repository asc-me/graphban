"""A filename glob collides by the names it matches, not by its directory (GRPH-1848).

`backend/tests/test_prd*.py` was read as `backend/tests/`, so one touchpoint chained every
item that names a backend test into a single cluster. A directory touchpoint
(`backend/tests/`, `backend/tests/*`) still covers what is under it.

Sabotage: restore the directory fallback for a filename glob — delete the
`_filename_pattern` branch in `clustering.why_match` so two paths that share a parent
return `directory` again. The first test fails.
"""
from __future__ import annotations

import importlib
import sys
from pathlib import Path

import pytest

from app.db import SessionLocal
from app.services import clustering, code_graph, collision
from app.services import fleet as fleet_svc
from app.services import items as items_svc

# (left, right, they collide). One table, both rules — the server's `areas_collide` and
# the fleet's `touchpoints.collide`.
PAIRS = [
    ("backend/tests/test_prd*.py", "backend/tests/project_sync_fake.py", False),
    ("backend/tests/test_prd*.py", "backend/tests/test_rule_adoption.py", False),
    ("backend/tests/test_prd*.py", "backend/tests/test_prd43.py", True),
    ("backend/tests/test_prd*.py", "backend/tests/*", True),
    ("backend/tests/test_prd*.py", "backend/tests/", True),
    ("backend/tests/test_prd*.py", "backend/tests", True),
    ("backend/tests/test_*.py", "backend/tests/*_adoption.py", True),
    ("backend/tests/test_prd*.py", "backend/tests/test_rule*.py", False),
    ("backend/tests/test_prd?.py", "backend/tests/test_prd1.py", True),
    ("backend/tests/test_prd?.py", "backend/tests/test_prd12.py", False),
    ("backend/tests/test_prd?.py", "backend/tests/other.py", False),
]

# The touchpoints that were open when the drain left GRPH-1842 behind 30 other items.
GRPH_1014 = ["backend/app/services/prds.py", "backend/tests/test_prd*.py"]
GRPH_1842 = [
    "backend/app/services/project_sync_client.py",
    "backend/app/services/project_sync_proposals.py",
    "backend/tests/project_sync_fake.py",
    "backend/tests/test_project_sync_proposals.py",
    "backend/tests/test_project_sync_client.py",
]


def _fleet_collide():
    """The worktree's fleet rule, not whatever copy happens to be installed."""
    root = Path(__file__).resolve().parents[2] / "fleet" / "src"
    sys.path.insert(0, str(root))
    for name in ("gbfleet.touchpoints", "gbfleet.worktree", "gbfleet"):
        sys.modules.pop(name, None)
    return importlib.import_module("gbfleet.touchpoints").collide


def _server(left: str, right: str) -> bool:
    return bool(fleet_svc.areas_collide([left], [right]))


@pytest.fixture()
def db(client):
    s = SessionLocal()
    try:
        yield s
    finally:
        s.close()


def test_a_filename_glob_does_not_collide_with_unrelated_files_in_its_directory():
    """The server rule. Same parent directory is not a match when the glob names files."""
    for other in (
        "backend/tests/project_sync_fake.py",
        "backend/tests/test_rule_adoption.py",
    ):
        assert clustering.why_match("backend/tests/test_prd*.py", other) == "", other
        assert clustering.why_match(other, "backend/tests/test_prd*.py") == "", other
        assert fleet_svc.areas_collide(["backend/tests/test_prd*.py"], [other]) == []
        assert fleet_svc.areas_collide([other], ["backend/tests/test_prd*.py"]) == []


def test_a_filename_glob_still_collides_with_a_match_and_with_its_directory():
    pattern = "backend/tests/test_prd*.py"
    assert clustering.why_match(pattern, "backend/tests/test_prd43.py") == "glob"
    assert clustering.why_match(pattern, "backend/tests/*") == "glob"
    assert clustering.why_match(pattern, "backend/tests/") == "directory"
    assert clustering.why_match(pattern, "backend/tests") == "directory"
    for other in ("backend/tests/test_prd43.py", "backend/tests/*", "backend/tests/", "backend/tests"):
        assert fleet_svc.areas_collide([pattern], [other]) == [pattern], other
        assert fleet_svc.areas_collide([other], [pattern]) == [other], other
    # A folder wildcard over a named test file stays the GRPH-1399 exemption.
    assert clustering.why_match("backend/tests/*", "backend/tests/test_prd43.py") == "test-glob"
    assert fleet_svc.areas_collide(["backend/tests/*"], ["backend/tests/test_prd43.py"]) == []


def test_the_fleet_rule_agrees_on_the_same_pairs():
    """Both rules, one table. A drift in either copy fails here."""
    fleet = _fleet_collide()
    for left, right, expect in PAIRS:
        assert _server(left, right) is expect, (left, right, "server")
        assert _server(right, left) is expect, (right, left, "server")
        assert bool(fleet([left], [right])) is expect, (left, right, "fleet")
        assert bool(fleet([right], [left])) is expect, (right, left, "fleet")


def test_grph_1014_and_grph_1842_land_in_different_clusters(db, monkeypatch):
    """The pair the drain actually joined. Transitivity is the cluster, so the test is
    `collision_clusters`, not another call of the matcher."""
    monkeypatch.setattr(code_graph, "search_code", lambda *a, **k: [])
    a = items_svc.create_item(db, title="PRD intent section", project_id="core",
                              touchpoints=GRPH_1014)
    b = items_svc.create_item(db, title="proposal wire", project_id="core",
                              touchpoints=GRPH_1842)
    clusters = collision.collision_clusters(db, [a, b], "core")
    assert sorted(len(c["items"]) for c in clusters) == [1, 1]
    assert {frozenset(c["items"]) for c in clusters} == {frozenset([a.id]), frozenset([b.id])}
