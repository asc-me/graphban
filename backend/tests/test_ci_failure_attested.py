"""A FAILED CI run is recorded as a failure, not left looking like silence (GRPH-1410).

PRs #1389, #1399 and #1404 had CI red on their heads, but CI only attested on a pass. Every
reviewer's sign_off was refused with "no CI run has reported suite_green for it yet", read it
as "CI pending", and waited. A missing pass and a recorded failure looked the same.
"""
from __future__ import annotations

import json
import pathlib
import sys
from datetime import timedelta

import pytest
import yaml

from app.models import utcnow
from app.services import items as items_svc

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2] / "scripts"))
import attest_ci  # noqa: E402

WORKFLOW = pathlib.Path(__file__).resolve().parents[2] / ".github/workflows/ci.yml"
HEAD = "a" * 40
PR = {"kind": "url", "url": "https://github.com/asc-me/graphban/pull/999", "detail": "PR #999"}


@pytest.fixture()
def db(client):
    from app.db import SessionLocal
    s = SessionLocal()
    try:
        yield s
    finally:
        s.close()


@pytest.fixture()
def cooldown(monkeypatch):
    from app.config import settings
    monkeypatch.setattr(settings, "pr_cooldown_seconds", 60, raising=False)


def _refusal(db, evidence) -> str:
    item = items_svc.create_item(db, project_id="core", title="Built it")
    items_svc.update_item(db, item.id, evidence=[PR, *evidence])
    item.pr_linked_at = utcnow() - timedelta(seconds=3600)
    db.commit()
    with pytest.raises(items_svc.PRCINotGreen) as exc:
        items_svc.refuse_if_pr_cooling_down(db, item, [], commit=HEAD)
    return str(exc.value)


# ---- the attester -------------------------------------------------------------------

def test_a_failed_run_builds_a_failed_suite_green_receipt_naming_jobs_and_tests():
    log = ("FAILED backend/tests/test_sharding.py::test_every_test_file_has_a_weight - boom\n"
           "FAILED backend/tests/test_sharding.py::test_every_test_file_has_a_weight - boom\n"
           "ERROR backend/tests/test_x.py::test_y\n")
    needs = json.dumps({"backend": {"result": "failure"}, "web": {"result": "success"},
                        "docs": {"result": "skipped"}})
    r = attest_ci.failed_attestation(
        commit=HEAD, branch="gb/x", jobs=attest_ci.failing_jobs(needs),
        tests=attest_ci.failing_tests(log))
    [pred] = r["predicates"]
    assert pred["name"] == "suite_green" and pred["passed"] is False
    assert "backend" in pred["detail"] and "web" not in pred["detail"]
    assert pred["detail"].count("test_every_test_file_has_a_weight") == 1
    assert "test_x.py::test_y" in pred["detail"]


def test_malformed_needs_json_still_records_a_failure():
    assert attest_ci.failing_jobs("not json") == []
    r = attest_ci.failed_attestation(commit=HEAD, branch="b", jobs=[], tests=[])
    assert r["predicates"][0]["passed"] is False


def test_mode_failed_posts_the_failed_receipt(monkeypatch):
    sent = []
    monkeypatch.setattr(attest_ci, "_call", lambda url, key, args, **kw: sent.append(args))
    monkeypatch.setenv("GRAPHBAN_URL", "http://gb.invalid")
    monkeypatch.setenv("GRAPHBAN_GATE_KEY", "k")
    rc = attest_ci.main(["--commit", HEAD, "--branch", "gb/x", "--mode", "failed",
                         "--needs-json", json.dumps({"backend": {"result": "failure"}}),
                         "--text", "GRPH-1 fix"])
    assert rc == 0
    [call] = sent
    assert call["id"] == "GRPH-1"
    assert call["evidence"][0]["predicates"][0]["passed"] is False


def test_the_failure_step_runs_only_when_ci_failed():
    steps = yaml.safe_load(WORKFLOW.read_text())["jobs"]["ci"]["steps"]
    step = next(s for s in steps if "--mode failed" in (s.get("run") or ""))
    assert step.get("if") == "failure()", \
        "the failed-run step must run on failure only, or it would contradict a green run"


# ---- the server's refusal -----------------------------------------------------------

def test_a_failed_head_refusal_says_ci_failed_names_it_and_says_bounce(db, cooldown):
    red = {"kind": "attestation", "adapter": "github-actions", "commit": HEAD,
           "predicates": [{"name": "suite_green", "passed": False,
                           "detail": "CI failed — failing jobs: backend; first failing "
                                     "tests: tests/t.py::test_a"}]}
    msg = _refusal(db, [red])
    assert "CI failed at " + HEAD[:12] in msg
    assert "tests/t.py::test_a" in msg
    assert "bounce" in msg.lower()
    assert "has not reported" not in msg


def test_no_attestation_at_all_still_reads_as_not_reported(db, cooldown):
    msg = _refusal(db, [])
    assert "no CI run has reported suite_green" in msg
    assert "CI failed at" not in msg
    assert "BOUNCE" not in msg
