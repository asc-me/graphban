"""GRPH-1837 — path-mode scan_status defaults to the project head; directory security cells."""
from __future__ import annotations

import pytest

from app.services import scans as scans_svc
from app.services import stack as stack_svc
from tests.scan_topology_fixtures import seed_core_topology


@pytest.fixture()
def db(client):
    from app.db import SessionLocal

    s = SessionLocal()
    try:
        seed_core_topology(s)
        yield s
    finally:
        s.close()


def _scan(db, path, sha="headX", findings=None, scanner="pip-audit"):
    scans_svc.record_scan(
        db, project_id="core", scanner_id=scanner, path=path, scanned_sha=sha,
        findings=findings or [], result="complete",
    )


def test_path_mode_defaults_to_project_head(db, monkeypatch):
    monkeypatch.setattr(stack_svc, "project_head_sha", lambda d, p: "headX")
    _scan(db, "backend/")
    st = scans_svc.scan_status(db, "core", path="backend/", scanner_id="pip-audit")
    assert st["head_sha"] == "headX"
    assert st["scans"][0]["state"] != "stale"


def test_explicit_head_overrides_project_head(db, monkeypatch):
    monkeypatch.setattr(stack_svc, "project_head_sha", lambda d, p: "headX")
    _scan(db, "backend/")
    st = scans_svc.scan_status(
        db, "core", path="backend/", scanner_id="pip-audit", head_sha="other")
    assert st["head_sha"] == "other"
    assert st["scans"][0]["state"] == "stale"


def test_path_mode_without_project_head_stays_unknown(db, monkeypatch):
    monkeypatch.setattr(stack_svc, "project_head_sha", lambda d, p: None)
    _scan(db, "backend/")
    st = scans_svc.scan_status(db, "core", path="backend/", scanner_id="pip-audit")
    assert st["head_sha"] is None
    assert st["scans"][0]["state"] == "stale"


def _dir_cells(db, head="headX"):
    comps = scans_svc.component_health(db, "core", head_sha=head)["components"]
    return [c for c in comps if c["path"].endswith("/") and "security" in c["categories"]]


def test_directory_cell_has_security_never_scanned_without_row(db):
    cells = _dir_cells(db)
    assert cells
    assert all(c["categories"]["security"]["state"] == "never_scanned" for c in cells)


def test_directory_cell_reads_security_row_at_its_path(db):
    cells = _dir_cells(db)
    target = cells[0]["path"]
    _scan(db, target, findings=[{"summary": "vuln", "severity": "high"}])
    cell = next(c for c in _dir_cells(db) if c["path"] == target)
    assert cell["categories"]["security"]["state"] == "has_findings"
    other = next((c for c in _dir_cells(db) if c["path"] != target), None)
    if other:
        assert other["categories"]["security"]["state"] == "never_scanned"
