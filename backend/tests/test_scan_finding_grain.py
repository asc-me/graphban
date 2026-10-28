"""GRPH-1127 — scan finding grain (advisory identity, knip rollups)."""
from __future__ import annotations

import pytest

from app.services import scans as scans_svc
from tests.scan_topology_fixtures import seed_core_topology


@pytest.fixture(autouse=True)
def _seed_core_topology(client):
    from app.db import SessionLocal

    s = SessionLocal()
    try:
        seed_core_topology(s)
    finally:
        s.close()


@pytest.fixture()
def db(client):
    from app.db import SessionLocal

    s = SessionLocal()
    try:
        yield s
    finally:
        s.close()


def _security_findings(db):
    return next(
        c for c in scans_svc.component_health(db, "core", head_sha="h1")["components"]
        if c["path"] == "web/package-lock.json"
    )["categories"]["security"]["findings"]


def test_npm_audit_finding_round_trips_grain_fields(db):
    scans_svc.record_scan(
        db,
        project_id="core",
        scanner_id="npm-audit",
        path="web/package-lock.json",
        scanned_sha="h1",
        findings=[{
            "severity": "crit",
            "summary": "next advisory",
            "path": "web/package-lock.json",
            "advisory_id": "GHSA-xxxx",
            "dependency": "direct",
            "fixed_version": "15.1.0",
        }],
    )
    f = _security_findings(db)[0]
    assert f["advisory_id"] == "GHSA-xxxx"
    assert f["dependency"] == "direct"
    assert f["fixed_version"] == "15.1.0"
    assert f["filed_key"].startswith(scans_svc.FILED_TAG_PREFIX)


def test_fixed_version_omitted_when_not_sent(db):
    scans_svc.record_scan(
        db,
        project_id="core",
        scanner_id="npm-audit",
        path="web/package-lock.json",
        scanned_sha="h1",
        findings=[{
            "severity": "high",
            "summary": "no fix yet",
            "advisory_id": "GHSA-yyyy",
            "dependency": "transitive",
        }],
    )
    f = _security_findings(db)[0]
    assert f["advisory_id"] == "GHSA-yyyy"
    assert "fixed_version" not in f


def test_knip_rollup_stored_as_one_row_with_kind_and_count(db):
    scans_svc.record_scan(
        db,
        project_id="core",
        scanner_id="knip",
        path="backend/",
        scanned_sha="h1",
        findings=[{
            "severity": "info",
            "summary": "207 unused exports",
            "kind": "unused_export",
            "count": 207,
        }],
    )
    dead = next(
        c for c in scans_svc.component_health(db, "core", head_sha="h1")["components"]
        if c["id"] == "api"
    )["categories"]["deadcode"]
    assert len(dead["findings"]) == 1
    f = dead["findings"][0]
    assert f["kind"] == "unused_export"
    assert f["count"] == 207
    assert "filed_key" not in f


def test_legacy_producer_with_summary_and_severity_only(db):
    scans_svc.record_scan(
        db,
        project_id="core",
        scanner_id="npm-audit",
        path="web/package-lock.json",
        scanned_sha="h1",
        findings=[{"severity": "low", "summary": "old advisory text"}],
    )
    f = _security_findings(db)[0]
    assert f["summary"] == "old advisory text"
    assert "advisory_id" not in f
    assert "kind" not in f


def test_advisory_id_is_not_dropped_on_ingest(db):
    """Sabotage the CALL: a handler that strips advisory_id must fail this."""
    scans_svc.record_scan(
        db,
        project_id="core",
        scanner_id="npm-audit",
        path="web/package-lock.json",
        scanned_sha="h1",
        findings=[{
            "severity": "crit",
            "summary": "critical pkg",
            "advisory_id": "GHSA-keep-me",
        }],
    )
    st = scans_svc.scan_status(db, "core", path="web/package-lock.json", head_sha="h1")
    assert st["scans"][0]["state"] == "has_findings"
    assert st["scans"][0]["findings"][0]["advisory_id"] == "GHSA-keep-me"


def test_invalid_dependency_is_refused(db):
    with pytest.raises(ValueError, match="dependency"):
        scans_svc.record_scan(
            db,
            project_id="core",
            scanner_id="npm-audit",
            path="web/package-lock.json",
            scanned_sha="h1",
            findings=[{
                "severity": "high",
                "summary": "bad dep field",
                "dependency": "optional",
            }],
        )
