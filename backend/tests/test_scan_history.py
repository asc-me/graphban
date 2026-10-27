"""GRPH-1128 — a re-scan keeps the earlier row and names what cleared."""
from __future__ import annotations

import pytest

from app.services import scans as scans_svc


@pytest.fixture()
def db(client):
    from app.db import SessionLocal

    s = SessionLocal()
    try:
        yield s
    finally:
        s.close()


def _audit(db, sha, findings, path="package-lock.json"):
    return scans_svc.record_scan(
        db, project_id="core", scanner_id="npm-audit", scanner_version="10.9.8",
        path=path, scanned_sha=sha, findings=findings,
    )


def _f(summary, sev="high", path="package-lock.json", **extra):
    return {"severity": sev, "summary": summary, "path": path, **extra}


def test_a_second_audit_of_the_same_lockfile_does_not_wipe_the_first(db):
    first = _audit(db, "sha1", [_f("next: SSRF"), _f("next-auth: open redirect")])
    second = _audit(db, "sha2", [_f("next-auth: open redirect")])
    st = scans_svc.scan_status(db, "core", path="package-lock.json", head_sha="sha2")
    rows = st["scans"]
    assert [r["scan_id"] for r in rows] == [second["scan_id"], first["scan_id"]]
    assert [r["scanned_sha"] for r in rows] == ["sha2", "sha1"]
    assert [r["current"] for r in rows] == [True, False]
    # The earlier row is still readable with its findings.
    assert [f["summary"] for f in rows[1]["findings"]] == [
        "next: SSRF", "next-auth: open redirect",
    ]


def test_the_later_row_names_what_cleared_and_what_is_new(db):
    _audit(db, "sha1", [_f("next: SSRF"), _f("next-auth: open redirect")])
    _audit(db, "sha2", [_f("next-auth: open redirect"), _f("proxy-addr: ReDoS", "crit")])
    newest, older = scans_svc.scan_status(db, "core", path="package-lock.json")["scans"]
    h = newest["history"]
    assert h["compared_sha"] == "sha1"
    assert [f["summary"] for f in h["cleared"]] == ["next: SSRF"]
    assert [f["summary"] for f in h["new"]] == ["proxy-addr: ReDoS"]
    assert older["history"] is None


def test_a_changed_summary_is_a_different_finding(db):
    _audit(db, "sha1", [_f("next: SSRF in image optimizer")])
    _audit(db, "sha2", [_f("next: SSRF in the image optimizer")])
    h = scans_svc.scan_status(db, "core", path="package-lock.json")["scans"][0]["history"]
    assert [f["summary"] for f in h["cleared"]] == ["next: SSRF in image optimizer"]
    assert [f["summary"] for f in h["new"]] == ["next: SSRF in the image optimizer"]


def test_the_same_summary_on_another_path_is_a_different_finding(db):
    _audit(db, "sha1", [_f("unused export", path="src/a.ts")])
    _audit(db, "sha2", [_f("unused export", path="src/b.ts")])
    h = scans_svc.scan_status(db, "core", path="package-lock.json")["scans"][0]["history"]
    assert [f["path"] for f in h["cleared"]] == ["src/a.ts"]
    assert [f["path"] for f in h["new"]] == ["src/b.ts"]


def test_advisory_ids_match_when_both_findings_carry_one():
    earlier = [_f("next: SSRF", advisory_id="GHSA-1")]
    later = [_f("next: SSRF (reworded)", advisory_id="GHSA-1")]
    assert scans_svc.finding_diff(earlier, later) == {"cleared": [], "new": []}
    other = [_f("next: SSRF", advisory_id="GHSA-2")]
    d = scans_svc.finding_diff(earlier, other)
    assert len(d["cleared"]) == 1 and len(d["new"]) == 1


def test_an_advisory_id_on_one_side_only_is_not_an_advisory_match():
    # The missing id is not a wildcard: the reworded summary cannot ride the one-sided id.
    earlier = [_f("next: SSRF", advisory_id="GHSA-1")]
    later = [_f("next: SSRF (reworded)")]
    d = scans_svc.finding_diff(earlier, later)
    assert len(d["cleared"]) == 1 and len(d["new"]) == 1
    # Without an advisory match it falls back to the exact summary and path.
    same = [_f("next: SSRF")]
    assert scans_svc.finding_diff(earlier, same) == {"cleared": [], "new": []}


def test_matching_is_one_to_one():
    d = scans_svc.finding_diff([_f("dup"), _f("dup")], [_f("dup")])
    assert len(d["cleared"]) == 1 and d["new"] == []


def test_the_cell_reads_the_newest_row_not_the_union(db):
    # Findings at an old sha, then a clean re-scan at head: the cell is clean, and the
    # cleared finding is not resurrected into it from the history.
    scans_svc.record_scan(
        db, project_id="core", scanner_id="bandit", path="backend/", scanned_sha="old",
        findings=[_f("assert used", path="backend/app/x.py")],
    )
    scans_svc.record_scan(
        db, project_id="core", scanner_id="bandit", path="backend/", scanned_sha="head",
        findings=[],
    )
    health = scans_svc.component_health(db, "core", head_sha="head")
    api = next(c for c in health["components"] if c["id"] == "api")
    assert api["categories"]["security"]["state"] == "clean_at_sha"
    assert api["categories"]["security"]["findings"] == []
    rows = scans_svc.scan_status(db, "core", path="backend/", head_sha="head")["scans"]
    assert len(rows) == 2
    assert [f["summary"] for f in rows[0]["history"]["cleared"]] == ["assert used"]


def test_history_is_per_scanner(db):
    # bandit and pip-audit on one path: each row compares against its own scanner's earlier row.
    scans_svc.record_scan(
        db, project_id="core", scanner_id="bandit", path="backend/", scanned_sha="s1",
        findings=[_f("assert used", path="backend/x.py")],
    )
    scans_svc.record_scan(
        db, project_id="core", scanner_id="pip-audit", path="backend/", scanned_sha="s1",
        findings=[_f("jinja2 CVE", path="backend/requirements.txt")],
    )
    rows = scans_svc.scan_status(db, "core", path="backend/")["scans"]
    assert [r["current"] for r in rows] == [True, True]
    assert all(r["history"] is None for r in rows)
    rows = scans_svc.scan_status(db, "core", path="backend/", scanner_id="bandit")["scans"]
    assert len(rows) == 1
