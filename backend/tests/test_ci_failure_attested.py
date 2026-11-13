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
    assert "steps.gate_result.outcome == 'failure'" in step.get("if", ""), \
        "the failed-run step must key on the result check, not on any step failing"
    assert any(s.get("id") == "gate_result" for s in steps)


def _timeout(*a, **kw):
    raise TimeoutError("timed out")


def test_a_ledger_timeout_in_post_head_exits_zero_and_attests_no_failure(monkeypatch, capsys):
    sent = []
    monkeypatch.setattr(attest_ci, "LEDGER_BACKOFF", 0)
    monkeypatch.setattr(attest_ci, "call_tool", lambda *a, **kw: (sent.append(a), _timeout())[1])
    monkeypatch.setenv("GRAPHBAN_URL", "http://gb.invalid")
    monkeypatch.setenv("GRAPHBAN_GATE_KEY", "k")
    monkeypatch.setattr(attest_ci.time, "sleep", lambda s: None)
    rc = attest_ci.main(["--commit", HEAD, "--branch", "gb/x", "--mode", "head",
                         "--text", "GRPH-1 fix"])
    out = capsys.readouterr().out
    assert rc == 0
    assert len(sent) == attest_ci.LEDGER_ATTEMPTS  # retried
    assert "::warning" in out and "::error" not in out
    assert all(a[3].get("evidence") is None for a in sent)  # no failure receipt written


def test_a_client_error_is_not_transient_and_still_fails(monkeypatch):
    import urllib.error
    err = urllib.error.HTTPError("u", 401, "no", {}, None)
    assert not attest_ci.is_transient(err)
    assert attest_ci.is_transient(urllib.error.HTTPError("u", 503, "x", {}, None))


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


# ---- GRPH-1409: an item sharing the PR's branch is attested too -----------------------

def test_items_recorded_on_the_head_branch_are_found_even_when_the_pr_names_another():
    rows = {"review": [{"id": "GRPH-1359", "branch": "gb/p59h-1"},
                       {"id": "GRPH-5", "branch": "gb/other"}],
            "done": [{"id": "GRPH-1399", "branch": "gb/p59h-1"}]}

    def search(url, key, status, **_):
        return rows.get(status, [])

    assert attest_ci.items_on_branch("u", "k", "gb/p59h-1", search=search) == [
        "GRPH-1359", "GRPH-1399"]
    assert attest_ci.items_on_branch("u", "k", "", search=search) == []


# ---- GRPH-1416: a run still in progress is a wait, not a dead end ---------------------

def _started(commit=HEAD, ref="https://ci/runs/42/attempts/1"):
    return {"kind": "attestation", "adapter": "github-actions", "commit": commit,
            "run_ref": ref, "predicates": [{"name": "ci_in_progress", "passed": True,
                                            "detail": "CI run started"}]}


def _refusal_exc(db, evidence):
    item = items_svc.create_item(db, project_id="core", title="Built it")
    items_svc.update_item(db, item.id, evidence=[PR, *evidence])
    item.pr_linked_at = utcnow() - timedelta(seconds=3600)
    db.commit()
    with pytest.raises(items_svc.PRCooldown) as exc:
        items_svc.refuse_if_pr_cooling_down(db, item, [], commit=HEAD)
    return exc.value


def test_past_cooldown_with_a_run_in_progress_names_the_run_and_is_not_a_dead_end(db, cooldown):
    exc = _refusal_exc(db, [_started()])
    msg = str(exc)
    assert not isinstance(exc, items_svc.PRCINotGreen)
    assert "waiting on CI run https://ci/runs/42/attempts/1, in progress" in msg
    assert "cooldown over" not in msg and "Waiting will not change" not in msg


def test_a_finished_run_is_not_reported_as_in_progress(db, cooldown):
    red = {"kind": "attestation", "adapter": "github-actions", "commit": HEAD,
           "predicates": [{"name": "suite_green", "passed": False, "detail": "CI failed"}]}
    green_other = {"kind": "attestation", "adapter": "github-actions", "commit": HEAD,
                   "predicates": [{"name": "suite_green", "passed": False, "detail": "x"}]}
    assert items_svc.ci_run_in_progress([_started(), red], [HEAD]) is None
    assert items_svc.ci_run_in_progress([_started(), green_other, _started(ref="r2")],
                                        [HEAD]) is not None
    assert items_svc.ci_run_in_progress([_started(commit="f" * 40)], [HEAD]) is None


def test_no_started_marker_keeps_the_silent_ci_wording(db, cooldown):
    msg = _refusal(db, [])
    assert "cooldown is over" in msg and "in progress" not in msg


def test_a_started_marker_is_not_a_verdict():
    """Bounce on GRPH-1416: the marker's predicate is `passed: True`, so it read as a valid
    attestation — a running (or since red) CI run opened the completion gate."""
    red = {"kind": "attestation", "adapter": "github-actions", "commit": HEAD,
           "predicates": [{"name": "suite_green", "passed": False, "detail": "CI failed"}]}
    assert not items_svc.has_valid_attestation([_started()], commit=HEAD)
    assert not items_svc.has_valid_attestation([_started(), red], commit=HEAD)
    assert items_svc.attested_predicates([_started()], commit=HEAD) == set()
    assert items_svc.attestation_receipts([_started(), red]) == [red]


def test_started_then_red_at_head_is_refused_as_failing(db, monkeypatch):
    from app.config import settings
    monkeypatch.setattr(settings, "pr_cooldown_seconds", 0, raising=False)
    red = {"kind": "attestation", "adapter": "github-actions", "commit": HEAD,
           "predicates": [{"name": "suite_green", "passed": False, "detail": "CI failed"}]}
    item = items_svc.create_item(db, project_id="core", title="Built it")
    items_svc.update_item(db, item.id, evidence=[_started(), red])
    with pytest.raises(items_svc.MissingAttestation) as exc:
        items_svc.update_item(db, item.id, status="done", head_commit=HEAD)
    assert "suite_green as failing" in str(exc.value)
    with pytest.raises(items_svc.MissingAttestation) as exc:
        items_svc.update_item(db, item.id, status="done")
    assert "suite_green as failing" in str(exc.value)


def test_a_started_marker_alone_does_not_open_the_gate(db, monkeypatch):
    from app.config import settings
    monkeypatch.setattr(settings, "pr_cooldown_seconds", 0, raising=False)
    item = items_svc.create_item(db, project_id="core", title="Built it")
    items_svc.update_item(db, item.id, evidence=[_started()])
    with pytest.raises(items_svc.MissingAttestation) as exc:
        items_svc.update_item(db, item.id, status="done", head_commit=HEAD)
    assert "nothing has attested it" in str(exc.value)
