"""GRPH-1035 — code-health scans + Stack overlay honesty."""
from __future__ import annotations

import pytest

from app.mcp_server import LIVE_TOOL_COUNT, TOOLS
from app.services import scans as scans_svc


@pytest.fixture()
def db(client):
    from app.db import SessionLocal

    s = SessionLocal()
    try:
        yield s
    finally:
        s.close()


def test_record_scan_tools_are_in_the_manifest():
    names = {t["name"] for t in TOOLS}
    assert "record_scan" in names
    assert "scan_status" in names
    assert "scans_due" in names
    assert LIVE_TOOL_COUNT == len(TOOLS)


def test_empty_findings_is_clean_at_sha_not_never_scanned(db):
    scans_svc.record_scan(
        db, project_id="core", scanner_id="pip-audit", scanner_version="2.7.3",
        path="backend/", scanned_sha="abc1234", findings=[], result="complete", reported_by="GRPH-A1",
    )
    st = scans_svc.scan_status(db, "core", path="backend/", head_sha="abc1234")
    assert st["scans"], "a recorded clean scan must appear"
    row = st["scans"][0]
    assert row["state"] == "clean_at_sha"
    assert row["findings"] == []
    assert row["scanned_sha"] == "abc1234"


def test_absence_is_never_scanned_not_clean(db):
    health = scans_svc.component_health(db, "core", head_sha="abc1234")
    web = next(c for c in health["components"] if c["id"] == "web")
    # Security has no producer yet → never_scanned. Must not roll up to CLEAN
    # unless every category is clean (coverage/work may be clean_at_sha derived).
    assert web["categories"]["security"]["state"] == "never_scanned"
    assert web["rollup"]["state"] != "clean_at_sha"
    assert web["rollup"]["label"] in ("NOT SCANNED", "STALE", "FINDINGS")


def test_findings_and_stale_are_distinct(db):
    scans_svc.record_scan(
        db, project_id="core", scanner_id="bandit", path="backend/",
        scanned_sha="oldsha01",
        findings=[{"severity": "high", "summary": "assert used", "path": "backend/app/x.py"}],
    )
    # Against a moved head → still has_findings (findings remain visible).
    st = scans_svc.scan_status(db, "core", path="backend/", head_sha="newsha02")
    assert st["scans"][0]["state"] == "has_findings"
    assert len(st["scans"][0]["findings"]) == 1

    # Clean at old sha, head moved → stale.
    scans_svc.record_scan(
        db, project_id="core", scanner_id="bandit", path="backend/",
        scanned_sha="oldsha01", findings=[], result="complete",
    )
    st2 = scans_svc.scan_status(db, "core", path="backend/", head_sha="newsha02")
    assert st2["scans"][0]["state"] == "stale"


def test_record_scan_requires_scanned_sha(db):
    with pytest.raises(ValueError, match="scanned_sha"):
        scans_svc.record_scan(
            db, project_id="core", scanner_id="pip-audit",
            path="backend/", scanned_sha="", findings=[], result="complete",
        )


def test_stack_health_endpoint(client, auth):
    r = client.get("/api/platform/stack/health?project_id=core", headers=auth)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["scored"] is False
    ids = {c["id"] for c in body["components"]}
    assert ids == {"web", "api", "fleet", "cli"}
    for c in body["components"]:
        assert "rollup" in c and "categories" in c
        assert set(c["categories"]) >= {"security", "coverage", "work"}


def _mcp(client, headers, name, arguments, call_id=1):
    return client.post(
        "/api/mcp",
        json={"jsonrpc": "2.0", "id": call_id, "method": "tools/call",
              "params": {"name": name, "arguments": arguments}},
        headers=headers,
    )


def test_mcp_record_scan_round_trip(client, auth):
    key = client.post("/api/api-keys", json={"name": "scan-e2e"}, headers=auth).json()["plaintext"]
    headers = {"X-API-Key": key}
    r = _mcp(client, headers, "record_scan", {
        "scanner_id": "pip-audit",
        "scanner_version": "2.7.3",
        "path": "web/",
        "scanned_sha": "deadbeef",
        "findings": [
            {"severity": "med", "summary": "lodash advisory", "path": "web/package.json"},
        ],
        "project_id": "core",
    })
    assert r.status_code == 200, r.text
    body = r.json()
    assert "error" not in body, body
    sc = body["result"]["structuredContent"]
    assert sc["findings_recorded"] == 1
    assert sc["clean"] is False

    st = _mcp(client, headers, "scan_status", {
        "path": "web/", "head_sha": "deadbeef", "project_id": "core",
    }, call_id=2)
    assert st.status_code == 200, st.text
    path_payload = st.json()["result"]["structuredContent"]
    assert path_payload["project_id"] == "core"
    assert path_payload["scans"] and path_payload["scans"][0]["state"] == "has_findings"

    # Overlay shape (no path) — exercises components/scored/generated_at for schema probe.
    overlay = _mcp(client, headers, "scan_status", {
        "head_sha": "deadbeef", "project_id": "core",
    }, call_id=3)
    assert overlay.status_code == 200, overlay.text
    ov = overlay.json()["result"]["structuredContent"]
    assert ov["scored"] is False
    assert "generated_at" in ov
    assert {c["id"] for c in ov["components"]} == {"web", "api", "fleet", "cli"}

    due = _mcp(client, headers, "scans_due", {
        "head_sha": "deadbeef", "project_id": "core",
    }, call_id=4)
    assert due.status_code == 200, due.text
    due_body = due.json()["result"]["structuredContent"]
    assert "due" in due_body and "due_count" in due_body


def test_sabotage_caller_dropping_scanned_sha_is_refused(db):
    """Sabotage the CALL: omit scanned_sha — suite must fail if we stop requiring it."""
    with pytest.raises(ValueError):
        scans_svc.record_scan(
            db, project_id="core", scanner_id="pip-audit",
            path="backend/", scanned_sha="   ", findings=[], result="complete",
        )


def test_nested_clean_scan_does_not_hide_parent_findings(db):
    """Same scanner, parent HIGH then nested clean → component still has_findings.

    Collapse-to-latest-per-scanner made the nested clean erase the parent finding.
    """
    sha = "abc1234"
    scans_svc.record_scan(
        db, project_id="core", scanner_id="bandit", path="backend/",
        scanned_sha=sha,
        findings=[{"severity": "high", "summary": "assert used", "path": "backend/app/x.py"}],
    )
    scans_svc.record_scan(
        db, project_id="core", scanner_id="bandit",
        path="backend/app/routers/platform.py",
        scanned_sha=sha, findings=[], result="complete",
    )
    health = scans_svc.component_health(db, "core", head_sha=sha)
    api = next(c for c in health["components"] if c["id"] == "api")
    assert api["categories"]["security"]["state"] == "has_findings"
    assert any(
        f.get("severity") == "high" for f in api["categories"]["security"]["findings"]
    )
    assert api["rollup"]["state"] == "has_findings"


def test_mixed_coverage_revisions_are_stale_not_clean(db):
    """Partly-stale graph under a prefix must read stale, not clean_at_sha."""
    from app.services import code_graph as code_svc

    code_svc.describe_code(
        db, project_id="core", revision="oldsha01",
        nodes=[{"path": "backend/app/a.py", "kind": "module", "summary": "a"}],
    )
    code_svc.describe_code(
        db, project_id="core", revision="newsha02",
        nodes=[{"path": "backend/app/b.py", "kind": "module", "summary": "b"}],
    )
    cat = scans_svc._coverage_category(db, "core", "backend/", head_sha="newsha02")
    assert cat["state"] == "stale", cat


def test_norm_path_keeps_dotfile_directory_prefix():
    """lstrip('./') ate the leading '.' of '.github/...'; removeprefix must not."""
    assert scans_svc._norm_path(".github/workflows/ci.yml") == ".github/workflows/ci.yml"
    assert scans_svc._norm_path("./backend/app") == "backend/app"
    assert scans_svc._norm_path("backend/") == "backend/"


def test_nested_only_clean_is_not_component_clean(db):
    """A clean scan on one nested file must not mark the whole component clean_at_sha.

    Bounce: bandit clean on backend/app/routers/platform.py at head made api security
    read clean_at_sha. Scanning one file says nothing about the rest of backend/.
    """
    sha = "headsha1"
    scans_svc.record_scan(
        db, project_id="core", scanner_id="bandit",
        path="backend/app/routers/platform.py",
        scanned_sha=sha, findings=[], result="complete",
    )
    health = scans_svc.component_health(db, "core", head_sha=sha)
    api = next(c for c in health["components"] if c["id"] == "api")
    assert api["categories"]["security"]["state"] != "clean_at_sha", api["categories"]["security"]
    assert api["categories"]["security"]["state"] == "stale"
    assert api["rollup"]["state"] != "clean_at_sha"


def test_root_clean_at_head_is_clean_at_sha(db):
    """A clean scan that covers the component root at head may read clean_at_sha."""
    sha = "headsha2"
    scans_svc.record_scan(
        db, project_id="core", scanner_id="bandit",
        path="backend/", scanned_sha=sha, findings=[], result="complete",
    )
    health = scans_svc.component_health(db, "core", head_sha=sha)
    api = next(c for c in health["components"] if c["id"] == "api")
    assert api["categories"]["security"]["state"] == "clean_at_sha"


def test_summary_less_finding_is_refused_and_writes_nothing(db):
    """A finding without summary must raise — never drop it and count the scan clean.

    Bounce: record_scan silently skipped non-dict / empty-summary entries, then
    stored clean=true. A bandit adapter that keys on issue_text painted api green.
    """
    from app.models import CodeScan

    with pytest.raises(ValueError, match=r"findings\[0\].*summary"):
        scans_svc.record_scan(
            db, project_id="core", scanner_id="bandit", path="backend/",
            scanned_sha="head1234",
            findings=[{"severity": "high", "issue_text": "B602 subprocess shell=True"}],
        )
    assert db.query(CodeScan).filter(CodeScan.project_id == "core").count() == 0

    with pytest.raises(ValueError, match=r"findings\[1\].*object"):
        scans_svc.record_scan(
            db, project_id="core", scanner_id="bandit", path="backend/",
            scanned_sha="head1234",
            findings=[
                {"severity": "high", "summary": "real finding"},
                "B105 hardcoded password",
            ],
        )
    assert db.query(CodeScan).filter(CodeScan.project_id == "core").count() == 0

    # After a refused write the path is still never_scanned — not clean.
    health = scans_svc.component_health(db, "core", head_sha="head1234")
    api = next(c for c in health["components"] if c["id"] == "api")
    assert api["categories"]["security"]["state"] == "never_scanned"
    assert api["rollup"]["state"] != "clean_at_sha"


def test_touches_component_requires_path_boundary():
    """Bare startswith matched 'backendish' to backend/ and 'w' to web/."""
    assert scans_svc._touches_component("backend/app/x.py", "backend/") is True
    assert scans_svc._touches_component("backend", "backend/") is True
    assert scans_svc._touches_component("backendish/x", "backend/") is False
    assert scans_svc._touches_component("w", "web/") is False
    assert scans_svc._touches_component("web/", "web/") is True


# --- GRPH-1044: provenance from the caller, and the overlay's write path -----------------


def test_mcp_record_scan_key_only_call_stamps_the_credential(client, auth):
    """A key-only call must not store reported_by="" — that reads as nobody reported it."""
    key = client.post("/api/api-keys", json={"name": "scan-prov"}, headers=auth).json()["plaintext"]
    r = _mcp(client, {"X-API-Key": key}, "record_scan", {
        "scanner_id": "bandit", "path": "backend/", "scanned_sha": "f00d",
        "findings": [], "result": "complete", "project_id": "core",
    })
    sc = r.json()["result"]["structuredContent"]
    assert sc["reported_by"] == "key:scan-prov"


def test_mcp_record_scan_with_agent_id_stamps_the_agent(client, auth):
    key = client.post("/api/api-keys", json={"name": "scan-prov2"}, headers=auth).json()["plaintext"]
    r = _mcp(client, {"X-API-Key": key}, "record_scan", {
        "scanner_id": "bandit", "path": "backend/", "scanned_sha": "f00d",
        "findings": [], "result": "complete", "project_id": "core", "agent_id": "GRPH-A9",
    })
    assert r.json()["result"]["structuredContent"]["reported_by"] == "GRPH-A9"


def _api(db):
    return next(c for c in scans_svc.component_health(db, "core", head_sha="h1")["components"]
                if c["id"] == "api")


def test_findings_carry_a_filed_key_and_filing_marks_them(db):
    from app.services import items as items_svc

    scans_svc.record_scan(
        db, project_id="core", scanner_id="pip-audit", path="backend/", scanned_sha="h1",
        findings=[{"severity": "high", "summary": "multipart DoS", "path": "backend/pyproject.toml"}],
    )
    f = _api(db)["categories"]["security"]["findings"][0]
    assert f["filed_key"].startswith(scans_svc.FILED_TAG_PREFIX)
    assert f["filed_item"] is None  # not filed is an explicit None, never a missing field

    # Same finding re-reported → same key (a re-scan must not re-offer filed work).
    scans_svc.record_scan(
        db, project_id="core", scanner_id="pip-audit", path="backend/", scanned_sha="h1",
        findings=[{"severity": "high", "summary": "multipart DoS", "path": "backend/pyproject.toml"}],
    )
    assert _api(db)["categories"]["security"]["findings"][0]["filed_key"] == f["filed_key"]

    it = items_svc.create_item(db, project_id="core", title="Defrag", tags=["defrag", f["filed_key"]])
    f2 = _api(db)["categories"]["security"]["findings"][0]
    assert f2["filed_item"] == it.key


def test_open_work_findings_offer_no_defrag(db):
    from app.services import items as items_svc

    items_svc.create_item(db, project_id="core", title="wip", touchpoints=["backend/app/x.py"])
    work = _api(db)["categories"]["work"]
    assert work["findings"], "fixture should produce an in-flight finding"
    assert all("filed_key" not in f for f in work["findings"])
    assert work["cta"] is None


def test_stale_and_never_scanned_cells_offer_a_cta(db):
    scans_svc.record_scan(
        db, project_id="core", scanner_id="radon", path="backend/", scanned_sha="old", findings=[], result="complete",
    )
    cats = _api(db)["categories"]
    assert cats["refactor"]["state"] == "stale"
    assert cats["refactor"]["cta"]["kind"] == "rerun"
    assert cats["stub"]["state"] == "never_scanned"
    assert cats["stub"]["cta"]["kind"] == "add_scanner"
    # A rerun offer is per stale revision: going stale again later is a new offer.
    first = cats["refactor"]["cta"]["filed_key"]
    scans_svc.record_scan(
        db, project_id="core", scanner_id="radon", path="backend/", scanned_sha="older", findings=[], result="complete",
    )
    assert _api(db)["categories"]["refactor"]["cta"]["filed_key"] != first
    # Clean at head offers nothing.
    scans_svc.record_scan(
        db, project_id="core", scanner_id="radon", path="backend/", scanned_sha="h1", findings=[], result="complete",
    )
    assert _api(db)["categories"]["refactor"]["cta"] is None


# ── GRPH-1061 · tests category ──────────────────────────────────────────────

def _tests_scan(db, *, sha="h1", path="backend/", findings=None, counts=None, scanner="pytest",
                result="complete", **kw):
    return scans_svc.record_scan(
        db, project_id="core", scanner_id=scanner, path=path, scanned_sha=sha,
        findings=findings or [], tests=counts, result=result, **kw,
    )


def test_pytest_without_a_category_is_stored_as_tests(db):
    for sid in ("pytest", "vitest", "jest", "junit"):
        out = _tests_scan(db, scanner=sid, counts={"passed": 3, "failed": 0, "skipped": 0})
        assert out["category"] == "tests", sid
    assert "tests" in scans_svc.CATEGORIES
    assert scans_svc.CATEGORY_LABELS["tests"] == "Tests"


def test_a_suite_that_ran_and_passed_at_head_is_clean(db):
    out = _tests_scan(db, counts={"passed": 412, "failed": 0, "skipped": 3})
    assert out["clean"] is True
    assert out["tests"] == {"passed": 412, "failed": 0, "skipped": 3}
    cell = _api(db)["categories"]["tests"]
    assert cell["state"] == "clean_at_sha"
    # Skips are reported, not folded silently into the passes.
    assert "412 passed · 0 failed · 3 skipped" in cell["note"]


def test_a_suite_that_executed_zero_tests_is_not_clean(db):
    out = _tests_scan(db, counts={"passed": 0, "failed": 0, "skipped": 0})
    assert out["clean"] is False
    cell = _api(db)["categories"]["tests"]
    assert cell["state"] == "has_findings"
    assert "0 tests executed" in cell["findings"][0]["summary"]
    assert cell["findings"][0]["severity"] == "high"


def test_an_all_skipped_run_executed_nothing_and_is_not_clean(db):
    _tests_scan(db, counts={"passed": 0, "failed": 0, "skipped": 9})
    cell = _api(db)["categories"]["tests"]
    assert cell["state"] == "has_findings"
    assert "9 skipped" in cell["findings"][0]["summary"]


def test_no_tests_result_at_head_is_never_scanned_or_stale(db):
    assert _api(db)["categories"]["tests"]["state"] == "never_scanned"
    assert _api(db)["rollup"]["state"] != "clean_at_sha"
    _tests_scan(db, sha="old", counts={"passed": 5, "failed": 0, "skipped": 0})
    assert _api(db)["categories"]["tests"]["state"] == "stale"
    due = scans_svc.scans_due(db, "core", head_sha="h1")["due"]
    assert {"component": "api", "category": "tests"} in [
        {"component": d["component"], "category": d["category"]} for d in due
    ]


# ── GRPH-1060 · dead-code category ──────────────────────────────────────────


@pytest.mark.parametrize("scanner", ["vulture", "knip", "ts-prune"])
def test_dead_code_scanners_infer_the_deadcode_category(db, scanner):
    row = scans_svc.record_scan(
        db, project_id="core", scanner_id=scanner, path="backend/", scanned_sha="h1",
        findings=[{"severity": "low", "summary": "unused function poll_once",
                   "path": "backend/app/sync/legacy.py"}],
    )
    assert row["category"] == "deadcode"
    dead = _api(db)["categories"]["deadcode"]
    assert dead["label"] == "Dead code · unused"
    assert dead["state"] == "has_findings"
    # It landed in deadcode only — not the security default.
    assert _api(db)["categories"]["security"]["state"] == "never_scanned"


def test_dead_code_absence_is_never_scanned_and_blocks_a_clean_rollup(db):
    # Every other scanned category clean at head; dead code never run.
    for sid in ("pip-audit", "radon", "stubscan"):
        scans_svc.record_scan(
            db, project_id="core", scanner_id=sid, path="backend/", scanned_sha="h1", findings=[], result="complete",
        )
    api = _api(db)
    assert api["categories"]["deadcode"]["state"] == "never_scanned"
    assert api["categories"]["deadcode"]["cta"]["kind"] == "add_scanner"
    assert api["rollup"]["state"] != "clean_at_sha"


def test_dead_code_clean_at_head_vs_older_sha(db):
    scans_svc.record_scan(
        db, project_id="core", scanner_id="vulture", path="backend/", scanned_sha="h1", findings=[], result="complete",
    )
    assert _api(db)["categories"]["deadcode"]["state"] == "clean_at_sha"
    scans_svc.record_scan(
        db, project_id="core", scanner_id="vulture", path="backend/", scanned_sha="old", findings=[], result="complete",
    )
    assert _api(db)["categories"]["deadcode"]["state"] == "stale"
    due = scans_svc.scans_due(db, "core", head_sha="h1")["due"]
    assert {"component": "api", "category": "deadcode"} in [
        {"component": d["component"], "category": d["category"]} for d in due
    ]


def test_a_tests_scan_without_counts_is_refused_and_writes_nothing(db):
    with pytest.raises(ValueError, match="passed, failed, skipped"):
        _tests_scan(db, counts=None)
    with pytest.raises(ValueError, match="tests.skipped"):
        _tests_scan(db, counts={"passed": 3, "failed": 0})
    with pytest.raises(ValueError, match="tests.passed"):
        _tests_scan(db, counts={"passed": -1, "failed": 0, "skipped": 0})
    assert _api(db)["categories"]["tests"]["state"] == "never_scanned"


def test_fewer_findings_than_failures_is_refused(db):
    with pytest.raises(ValueError, match="every failing test must be a finding"):
        _tests_scan(db, counts={"passed": 10, "failed": 2, "skipped": 0}, findings=[
            {"summary": "tests/test_a.py::test_one", "path": "backend/tests/test_a.py"},
        ])
    assert _api(db)["categories"]["tests"]["state"] == "never_scanned"


def test_counts_on_a_non_tests_scan_are_refused(db):
    with pytest.raises(ValueError, match="only for the tests category"):
        _tests_scan(db, scanner="bandit", counts={"passed": 1, "failed": 0, "skipped": 0})


def test_each_failing_test_is_a_finding_with_its_id_and_file_filed_once(db):
    fails = [
        {"summary": "tests/test_a.py::test_one", "path": "backend/tests/test_a.py"},
        {"summary": "tests/test_b.py::test_two", "path": "backend/tests/test_b.py"},
    ]
    _tests_scan(db, counts={"passed": 10, "failed": 2, "skipped": 0}, findings=fails)
    cell = _api(db)["categories"]["tests"]
    assert cell["state"] == "has_findings"
    got = [(f["summary"], f["path"], f["severity"]) for f in cell["findings"]]
    assert got == [
        ("tests/test_a.py::test_one", "backend/tests/test_a.py", "high"),
        ("tests/test_b.py::test_two", "backend/tests/test_b.py", "high"),
    ]
    keys = [f["filed_key"] for f in cell["findings"]]
    assert len(set(keys)) == 2
    # Re-running the same failing suite offers the same Defrag keys — filed once.
    _tests_scan(db, counts={"passed": 10, "failed": 2, "skipped": 0}, findings=fails)
    assert [f["filed_key"] for f in _api(db)["categories"]["tests"]["findings"]] == keys


def test_mcp_and_rest_carry_tests_counts(client, auth):
    key = client.post("/api/api-keys", json={"name": "tests-cat"}, headers=auth).json()["plaintext"]
    headers = {"X-API-Key": key}
    r = _mcp(client, headers, "record_scan", {
        "scanner_id": "vitest", "path": "web/", "scanned_sha": "abc",
        "tests": {"passed": 0, "failed": 0, "skipped": 0}, "project_id": "core",
    })
    sc = r.json()["result"]["structuredContent"]
    assert sc["category"] == "tests" and sc["clean"] is False
    assert sc["tests"] == {"passed": 0, "failed": 0, "skipped": 0}

    rest = client.post("/api/platform/stack/scans", headers=headers, json={
        "project_id": "core", "scanner_id": "pytest", "path": "backend/", "scanned_sha": "abc",
        "tests": {"passed": 4, "failed": 0, "skipped": 1}, "result": "complete",
    })
    assert rest.status_code == 200, rest.text
    assert rest.json()["tests"] == {"passed": 4, "failed": 0, "skipped": 1}
    bad = client.post("/api/platform/stack/scans", headers=headers, json={
        "project_id": "core", "scanner_id": "pytest", "path": "backend/", "scanned_sha": "abc",
    })
    assert bad.status_code == 422, bad.text


def test_dead_code_nested_clean_does_not_roll_up_clean(db):
    scans_svc.record_scan(
        db, project_id="core", scanner_id="vulture", path="backend/app/sync/",
        scanned_sha="h1", findings=[], result="complete",
    )
    assert _api(db)["categories"]["deadcode"]["state"] != "clean_at_sha"


def test_dead_code_finding_files_once(db):
    from app.services import items as items_svc

    scans_svc.record_scan(
        db, project_id="core", scanner_id="knip", path="backend/", scanned_sha="h1",
        findings=[{"severity": "low", "summary": "unused export foo", "path": "backend/x.py"}],
    )
    f = _api(db)["categories"]["deadcode"]["findings"][0]
    assert f["filed_item"] is None
    it = items_svc.create_item(
        db, project_id="core", title="Defrag backend/x.py: unused export foo",
        tags=["defrag", "tech-debt", f["filed_key"]],
    )
    assert _api(db)["categories"]["deadcode"]["findings"][0]["filed_item"] == it.key


# ── GRPH-1108 · an unknown scanner with no category is refused, never defaulted ──


def _web(db, head_sha="h1"):
    h = scans_svc.component_health(db, "core", head_sha=head_sha)
    return next(c for c in h["components"] if c["id"] == "web")


def test_unknown_scanner_without_category_is_refused_and_stores_nothing(db):
    with pytest.raises(ValueError) as e:
        scans_svc.record_scan(
            db, project_id="core", scanner_id="coderabbit", path="web/",
            scanned_sha="h1", findings=[], result="complete",
        )
    msg = str(e.value)
    # A repair hint: the valid categories and the known security scanners.
    for cat in scans_svc.CATEGORIES:
        assert cat in msg
    for sid in ("pip-audit", "bandit", "semgrep", "npm-audit"):
        assert sid in msg
    # One empty unknown row at head must not paint security clean_at_sha.
    assert _web(db)["categories"]["security"]["state"] == "never_scanned"
    assert scans_svc.scan_status(db, "core", path="web/", head_sha="h1")["scans"] == []


def test_unknown_scanner_with_an_explicit_category_is_stored_as_given(db):
    row = scans_svc.record_scan(
        db, project_id="core", scanner_id="coderabbit", path="web/",
        scanned_sha="h1", findings=[], result="complete", category="refactor",
    )
    assert row["category"] == "refactor"
    assert _web(db)["categories"]["security"]["state"] == "never_scanned"


def test_a_known_security_scanner_is_still_inferred_as_security(db):
    row = scans_svc.record_scan(
        db, project_id="core", scanner_id="pip-audit", path="web/",
        scanned_sha="h1", findings=[], result="complete",
    )
    assert row["category"] == "security"


def test_mcp_record_scan_refuses_an_unknown_scanner_without_category(client, auth):
    key = client.post("/api/api-keys", json={"name": "scan-unknown"}, headers=auth).json()["plaintext"]
    headers = {"X-API-Key": key}
    r = _mcp(client, headers, "record_scan", {
        "scanner_id": "coderabbit", "path": "web/", "scanned_sha": "h1",
        "findings": [], "project_id": "core",
    })
    assert r.status_code == 200, r.text
    result = r.json()["result"]
    assert result["isError"] is True
    err = result["structuredContent"]["error"]
    assert err["code"] == "validation"
    assert "security" in err["message"] and "pip-audit" in err["message"]

    h = client.get("/api/platform/stack/health?project_id=core&head_sha=h1", headers=auth).json()
    web = next(c for c in h["components"] if c["id"] == "web")
    assert web["categories"]["security"]["state"] == "never_scanned"


# ── GRPH-1125 · producer result states ───────────────────────────────────────


def test_empty_findings_without_result_is_refused_and_prior_row_stays(db):
    scans_svc.record_scan(
        db, project_id="core", scanner_id="semgrep", path="web/", scanned_sha="h1",
        findings=[{"severity": "high", "summary": "rule hit", "path": "web/x.ts"}],
    )
    with pytest.raises(ValueError, match="result is required when findings is empty"):
        scans_svc.record_scan(
            db, project_id="core", scanner_id="semgrep", path="web/", scanned_sha="h2",
            findings=[],
        )
    st = scans_svc.scan_status(db, "core", path="web/", scanner_id="semgrep", head_sha="h1")
    assert st["scans"][0]["state"] == "has_findings"


def test_tool_missing_is_not_never_scanned_and_stays_due(db):
    scans_svc.record_scan(
        db, project_id="core", scanner_id="semgrep", path="web/", scanned_sha="h1",
        findings=[], result="tool_missing",
    )
    cell = _web(db, head_sha="h1")["categories"]["security"]
    assert cell["state"] == "tool_missing"
    due = scans_svc.scans_due(db, "core", head_sha="h1")["due"]
    assert {"component": "web", "category": "security"} in [
        {"component": d["component"], "category": d["category"]} for d in due
    ]


def test_not_applicable_stops_scans_due(db):
    scans_svc.record_scan(
        db, project_id="core", scanner_id="pip-audit", path="web/", scanned_sha="h1",
        findings=[], result="not_applicable",
    )
    cell = _web(db, head_sha="h1")["categories"]["security"]
    assert cell["state"] == "not_applicable"
    due = scans_svc.scans_due(db, "core", head_sha="h1")["due"]
    assert not any(d["component"] == "web" and d["category"] == "security" for d in due)


def test_incomplete_with_findings_is_not_clean(db):
    scans_svc.record_scan(
        db, project_id="core", scanner_id="knip", path="web/", scanned_sha="h1",
        findings=[{"severity": "low", "summary": "unused export", "path": "web/x.ts"}],
        result="incomplete",
    )
    cell = _web(db, head_sha="h1")["categories"]["deadcode"]
    assert cell["state"] == "incomplete"
    assert cell["findings"]


def test_tool_missing_at_old_sha_is_stale(db):
    scans_svc.record_scan(
        db, project_id="core", scanner_id="trivy", path="web/", scanned_sha="old",
        findings=[], result="tool_missing",
    )
    cell = _web(db, head_sha="h1")["categories"]["security"]
    assert cell["state"] == "stale"


def test_coverage_without_code_graph_is_not_computable(db):
    from app.db import SessionLocal
    from app.models import Project

    s = SessionLocal()
    try:
        pid = "no-graph-p58"
        if not s.get(Project, pid):
            s.add(Project(id=pid, name="No graph", tag="NGRP"))
            s.commit()
        cov = scans_svc.component_health(s, pid, head_sha="h1")["components"][0]["categories"]["coverage"]
        assert cov["state"] == "not_computable"
    finally:
        s.close()


def test_mcp_empty_findings_without_result_is_validation_error(client, auth):
    key = client.post("/api/api-keys", json={"name": "scan-result"}, headers=auth).json()["plaintext"]
    headers = {"X-API-Key": key}
    r = _mcp(client, headers, "record_scan", {
        "scanner_id": "pip-audit", "path": "web/", "scanned_sha": "h1",
        "findings": [], "project_id": "core",
    })
    err = r.json()["result"]
    assert err["isError"] is True
    assert err["structuredContent"]["error"]["code"] == "validation"
    assert "complete" in err["structuredContent"]["error"]["message"]


# ── GRPH-1126 · first-time due ───────────────────────────────────────────────


def test_never_scanned_stub_and_deadcode_are_due_before_any_report(db):
    due = scans_svc.scans_due(db, "core", head_sha="h1")
    pairs = {(d["component"], d["category"]) for d in due["due"]}
    assert ("api", "stub") in pairs
    assert ("api", "deadcode") in pairs
    assert ("web", "refactor") in pairs
    assert ("web", "tests") in pairs


def test_has_findings_at_head_is_not_due(db):
    scans_svc.record_scan(
        db, project_id="core", scanner_id="pip-audit", path="web/", scanned_sha="h1",
        findings=[{"severity": "high", "summary": "advisory", "path": "web/package.json"}],
        result="complete",
    )
    due = scans_svc.scans_due(db, "core", head_sha="h1")["due"]
    assert not any(d["component"] == "web" and d["category"] == "security" for d in due)


def test_incomplete_stays_due(db):
    scans_svc.record_scan(
        db, project_id="core", scanner_id="knip", path="web/", scanned_sha="h1",
        findings=[{"severity": "low", "summary": "unused export", "path": "web/x.ts"}],
        result="incomplete",
    )
    due = scans_svc.scans_due(db, "core", head_sha="h1")["due"]
    assert any(d["component"] == "web" and d["category"] == "deadcode" for d in due)


def _clean_all_scannable_at_head(db, project_id="core", head_sha="h1"):
    """Every producer category clean or not_applicable on every default component."""
    counts = {"passed": 1, "failed": 0, "skipped": 0}
    for prefix, scanners in (
        ("web/", ("pip-audit", "radon", "stubscan", "knip", "vitest")),
        ("backend/", ("pip-audit", "radon", "stubscan", "vulture", "pytest")),
        ("fleet/", ("pip-audit", "radon", "stubscan", "vulture", "pytest")),
        ("cli/", ("pip-audit", "radon", "stubscan", "vulture", "pytest")),
    ):
        for sid in scanners:
            kw = {"tests": counts} if sid in ("vitest", "pytest") else {}
            scans_svc.record_scan(
                db, project_id=project_id, scanner_id=sid, path=prefix, scanned_sha=head_sha,
                findings=[], result="complete", **kw,
            )


def test_empty_due_names_why_not_all_clean(db):
    _clean_all_scannable_at_head(db)
    out = scans_svc.scans_due(db, "core", head_sha="h1")
    assert out["due_count"] == 0
    assert out["empty_reason"] in ("all_clean_or_not_applicable", "coverage_not_computable")


def test_no_graph_project_empty_due_is_coverage_not_computable(db):
    from app.db import SessionLocal
    from app.models import Project

    s = SessionLocal()
    try:
        pid = "no-graph-due-p58"
        if not s.get(Project, pid):
            s.add(Project(id=pid, name="No graph due", tag="NGDP"))
            s.commit()
        _clean_all_scannable_at_head(s, project_id=pid, head_sha="h1")
        out = scans_svc.scans_due(s, pid, head_sha="h1")
        assert out["due_count"] == 0
        assert out["empty_reason"] == "coverage_not_computable"
    finally:
        s.close()
