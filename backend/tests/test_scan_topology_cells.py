"""GRPH-1124 — scan board cells from posted app topology."""
from __future__ import annotations

import pytest

from app.models import AppTopology
from app.services import scans as scans_svc
from app.services import scan_topology as topo_svc


@pytest.fixture()
def db(client):
    from app.db import SessionLocal

    s = SessionLocal()
    try:
        yield s
    finally:
        s.close()


def test_without_topology_health_is_unmapped_with_no_phantom_boxes(db):
    db.query(AppTopology).filter(AppTopology.project_id == "core").delete()
    db.commit()
    health = scans_svc.component_health(db, "core", head_sha="h1")
    assert health["mapping"]["state"] == "unmapped"
    assert health["components"] == []
    assert health["scored"] is False
    due = scans_svc.scans_due(db, "core", head_sha="h1")
    assert due["due"] == []
    assert due["due_count"] == 0


def test_topology_lockfile_scan_satisfies_only_that_security_cell(db):
    from datetime import datetime, timezone

    db.query(AppTopology).filter(AppTopology.project_id == "core").delete()
    db.add(AppTopology(
        project_id="core",
        mapped_sha="sha1",
        mapped_at=datetime.now(timezone.utc),
        components=[
            {"id": "root", "dir": ".", "label": "root",
             "provenance": {"file": "package-lock.json", "rule": "lock", "line": 1}},
            {"id": "desktop", "dir": "apps/scribe-desktop", "label": "desktop",
             "provenance": {"file": "apps/scribe-desktop/package.json", "rule": "name", "line": 1}},
            {
                "id": "desktop-lock",
                "dir": "apps/scribe-desktop/package-lock.json",
                "label": "lock",
                "provenance": {
                    "file": "apps/scribe-desktop/package-lock.json",
                    "rule": "lock",
                    "line": 1,
                },
            },
            {
                "id": "root-lock",
                "dir": "package-lock.json",
                "label": "root lock",
                "provenance": {"file": "package-lock.json", "rule": "lock", "line": 1},
            },
        ],
    ))
    db.commit()
    scans_svc.record_scan(
        db, project_id="core", scanner_id="npm-audit",
        path="package-lock.json", scanned_sha="sha1", findings=[], result="complete",
    )
    health = scans_svc.component_health(db, "core", head_sha="sha1")
    root = next(c for c in health["components"] if c["path"] == "package-lock.json")
    desktop = next(c for c in health["components"] if c["path"] == "apps/scribe-desktop/package-lock.json")
    assert root["categories"]["security"]["state"] == "clean_at_sha"
    assert desktop["categories"]["security"]["state"] == "never_scanned"


def test_no_code_graph_yields_one_not_computable_coverage_cell(db):
    from datetime import datetime, timezone

    from app.models import CodeNode

    db.query(CodeNode).filter(CodeNode.project_id == "core").delete()
    db.query(AppTopology).filter(AppTopology.project_id == "core").delete()
    db.add(AppTopology(
        project_id="core",
        mapped_sha="s1",
        mapped_at=datetime.now(timezone.utc),
        components=[{
            "id": "src", "dir": "src", "label": "src",
            "provenance": {"file": "src/index.ts", "rule": "main", "line": 1},
        }],
    ))
    db.commit()
    cells = topo_svc.scan_cells(db, "core")[1]
    cov = [c for c in cells if "coverage" in c.categories]
    assert len(cov) == 1
    assert cov[0].id == "_coverage:not_computable"
