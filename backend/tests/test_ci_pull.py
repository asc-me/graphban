"""A box behind NAT pulls `suite_green` from GitHub check-runs (GRPH-1822).

The loop writes the attestation itself. Zero check-runs is not a green suite, a
still-running head writes nothing, and a missing or rejected token is a sentence
on the done refusal and on `/health` — not silence.
"""
from __future__ import annotations

import asyncio
import hashlib
import inspect
import pathlib
from datetime import timedelta

import pytest

from app.config import settings
from app.models import utcnow
from app.services import ci_pull, items as items_svc

PR = "https://github.com/asc-me/graphban/pull/1822"
ROOT = pathlib.Path(__file__).resolve().parents[2]


def _sha(tag: str) -> str:
    return hashlib.sha1(tag.encode()).hexdigest()


@pytest.fixture(autouse=True)
def _quiet_pull(monkeypatch):
    """Observations are process-global. A leaked sentence would change a later
    file's refusal for the same commit, and a real token would call GitHub."""
    ci_pull.reset_state()
    monkeypatch.setattr(settings, "github_token", "test-token", raising=False)
    monkeypatch.setattr(settings, "pr_cooldown_seconds", 60, raising=False)
    yield
    ci_pull.reset_state()


@pytest.fixture()
def db(client):
    from app.db import SessionLocal
    session = SessionLocal()
    try:
        yield session
    finally:
        session.close()


class _Resp:
    def __init__(self, status, body=None, headers=None):
        self.status_code = status
        self._body = {} if body is None else body
        self.headers = headers or {}

    def json(self):
        return self._body


def _github(monkeypatch, responder):
    calls = []

    def get(url, headers=None, timeout=None):
        calls.append({"url": url, "headers": headers or {}, "timeout": timeout})
        return responder(url, headers or {})

    monkeypatch.setattr(ci_pull.httpx, "get", get)
    return calls


def _check(name, status="completed", conclusion="success", url="https://github.com/runs/1"):
    return {"name": name, "status": status, "conclusion": conclusion, "html_url": url}


def _body(*runs, total=None):
    return {"total_count": len(runs) if total is None else total, "check_runs": list(runs)}


def _item(db, *, status="review", head=None, github_url=PR, evidence=None):
    item = items_svc.create_item(db, project_id="core", title="pull me", status=status)
    fields = {"head_commit": head or _sha("default"), "github_url": github_url or ""}
    if evidence:
        fields["evidence"] = evidence
    # Commit the head through the service. Setting it on the object and then calling
    # update_item would commit a freshly loaded row and drop the uncommitted head.
    item = items_svc.update_item(db, item.id, **fields)
    item.pr_linked_at = utcnow() - timedelta(hours=2)
    db.commit()
    db.refresh(item)
    return item


def _suite(item):
    out = []
    for receipt in items_svc.attestation_receipts(item.evidence):
        preds = [p for p in receipt.get("predicates") or []
                 if isinstance(p, dict) and p.get("name") == items_svc.CI_PREDICATE]
        if preds:
            out.append((receipt, preds))
    return out


def _ok(url, headers, runs):
    return _Resp(200, _body(*runs))


# ---- a finished green head -----------------------------------------------------------

def test_all_green_writes_one_pass_and_the_item_can_reach_done(db, monkeypatch):
    head = "AbCdEf12" + "a" * 32
    item = _item(db, head=head)
    calls = _github(monkeypatch, lambda url, headers: _ok(
        url, headers, [_check("test", url="https://github.com/asc-me/graphban/runs/9")]))
    ci_pull.run_once(db, now=0)
    db.refresh(item)
    [(receipt, preds)] = _suite(item)
    assert receipt["adapter"] == "github-checks"
    assert receipt["commit"] == head  # the gate matches the recorded string, not a lowercased sha
    assert receipt["branch"] == ""
    assert preds == [{"name": "suite_green", "passed": True,
                      "detail": "all 1 check-runs completed and none failed"}]
    assert receipt["run_ref"] == "https://github.com/asc-me/graphban/runs/9"
    assert calls[0]["headers"]["Authorization"] == "Bearer test-token"
    assert f"/commits/{head.lower()}/check-runs" in calls[0]["url"]
    assert "scans_clean" not in str(receipt["predicates"])
    done = items_svc.update_item(db, item.id, status="done")
    assert done.status == "done"
    assert items_svc.CI_PREDICATE in items_svc.attested_predicates(done.evidence, commit=head)
    # A pass is the answer. A later wake does not call GitHub again.
    ci_pull.run_once(db, now=10_000)
    assert len(calls) == 1


def test_one_fetch_covers_every_review_item_at_that_head(db, monkeypatch):
    head = _sha("shared")
    first = _item(db, head=head)
    second = _item(db, head=head)
    calls = _github(monkeypatch, lambda url, headers: _ok(url, headers, [_check("test")]))
    ci_pull.run_once(db, now=0)
    assert len(calls) == 1
    for item in (first, second):
        db.refresh(item)
        assert _suite(item)[0][1][0]["passed"] is True


@pytest.mark.parametrize("conclusion", [
    "failure", "cancelled", "timed_out", "action_required", "startup_failure",
])
def test_a_completed_failure_is_suite_green_failed_and_names_the_check(db, monkeypatch, conclusion):
    head = _sha(conclusion)
    item = _item(db, head=head)
    calls = _github(monkeypatch, lambda url, headers: _ok(url, headers, [
        _check("unit", conclusion=conclusion, url=f"https://github.com/runs/{conclusion}"),
        _check("lint", conclusion="success"),
    ]))
    ci_pull.run_once(db, now=0)
    db.refresh(item)
    [(receipt, preds)] = _suite(item)
    assert preds[0]["passed"] is False
    assert preds[0]["name"] == "suite_green"
    assert f"failing check: unit ({conclusion})" in preds[0]["detail"]
    assert receipt["run_ref"] == f"https://github.com/runs/{conclusion}"
    # The bounce a reviewer hits (GRPH-1410): the failed receipt is the answer,
    # and the check's name is the reason. `update_item` stops earlier, on the
    # failing attestation, once any passing receipt is absent.
    with pytest.raises(items_svc.PRCINotGreen) as exc:
        items_svc.refuse_if_pr_cooling_down(db, item, [], commit=head)
    assert f"unit ({conclusion})" in str(exc.value)
    assert "BOUNCE" in str(exc.value)
    ci_pull.run_once(db, now=10_000)
    assert len(calls) == 1


def test_skipped_beside_success_is_still_a_pass(db, monkeypatch):
    item = _item(db, head=_sha("skipped"))
    _github(monkeypatch, lambda url, headers: _ok(url, headers, [
        _check("test"), _check("optional", conclusion="skipped"),
        _check("note", conclusion="neutral"),
    ]))
    ci_pull.run_once(db, now=0)
    db.refresh(item)
    assert _suite(item)[0][1][0]["passed"] is True


# ---- not an answer yet ---------------------------------------------------------------

def test_checks_still_running_write_nothing(db, monkeypatch):
    item = _item(db, head=_sha("running"))
    before = list(item.evidence or [])
    _github(monkeypatch, lambda url, headers: _ok(url, headers, [
        _check("test", status="in_progress", conclusion=""),
    ]))
    ci_pull.run_once(db, now=0)
    db.refresh(item)
    assert list(item.evidence or []) == before
    assert ci_pull.last_error() == ""
    with pytest.raises(items_svc.MissingAttestation) as exc:
        items_svc.update_item(db, item.id, status="done")
    assert "GitHub checks for this head are still running" in str(exc.value)
    assert "in progress" not in str(exc.value)
    with pytest.raises(items_svc.PRCINotGreen) as cooled:
        items_svc.refuse_if_pr_cooling_down(db, item, [], commit=item.head_commit)
    assert "still running" in str(cooled.value)
    assert "in progress" not in str(cooled.value)


def test_a_running_check_blocks_a_verdict_even_when_another_check_failed(db, monkeypatch):
    """The acceptance rule is that a still-running head writes nothing, including when
    a sibling has already failed. The failure is recorded once every check has finished."""
    item = _item(db, head=_sha("mixed"))
    before = list(item.evidence or [])
    _github(monkeypatch, lambda url, headers: _ok(url, headers, [
        _check("unit", conclusion="failure"),
        _check("lint", status="in_progress", conclusion=""),
    ]))
    ci_pull.run_once(db, now=0)
    db.refresh(item)
    assert list(item.evidence or []) == before
    with pytest.raises(items_svc.MissingAttestation) as exc:
        items_svc.update_item(db, item.id, status="done")
    assert "still running" in str(exc.value)
    assert "failing check" not in str(exc.value)


def test_zero_check_runs_is_not_a_green_suite(db, monkeypatch):
    item = _item(db, head=_sha("empty"))
    _github(monkeypatch, lambda url, headers: _Resp(200, {"total_count": 0, "check_runs": []}))
    ci_pull.run_once(db, now=0)
    db.refresh(item)
    assert _suite(item) == []
    assert items_svc.has_valid_attestation(item.evidence, commit=item.head_commit) is False
    assert ci_pull.last_error() == ""
    with pytest.raises(items_svc.MissingAttestation) as exc:
        items_svc.update_item(db, item.id, status="done")
    assert "no check-runs were found" in str(exc.value)
    with pytest.raises(items_svc.PRCINotGreen) as cooled:
        items_svc.refuse_if_pr_cooling_down(db, item, [], commit=item.head_commit)
    assert "no check-runs were found" in str(cooled.value)
    assert "no CI run has reported suite_green" in str(cooled.value)


def test_a_head_is_not_polled_more_than_once_a_minute(db, monkeypatch):
    item = _item(db, head=_sha("floor"))
    calls = _github(monkeypatch, lambda url, headers: _ok(url, headers, [
        _check("test", status="queued", conclusion=""),
    ]))
    ci_pull.run_once(db, now=0)
    ci_pull.run_once(db, now=59)
    assert len(calls) == 1
    ci_pull.run_once(db, now=60)
    assert len(calls) == 2
    db.refresh(item)
    assert _suite(item) == []


def test_a_slower_setting_slows_the_poll(db, monkeypatch):
    monkeypatch.setattr(settings, "ci_pull_seconds", 300, raising=False)
    _item(db, head=_sha("slow"))
    calls = _github(monkeypatch, lambda url, headers: _ok(url, headers, [
        _check("test", status="queued", conclusion=""),
    ]))
    ci_pull.run_once(db, now=0)
    ci_pull.run_once(db, now=60)
    assert len(calls) == 1
    ci_pull.run_once(db, now=300)
    assert len(calls) == 2


# ---- the token, and a list that is not the whole list --------------------------------

def test_github_401_writes_nothing_and_names_the_token(db, client, monkeypatch):
    item = _item(db, head=_sha("denied"))
    _github(monkeypatch, lambda url, headers: _Resp(401, {}))
    ci_pull.run_once(db, now=0)
    db.refresh(item)
    assert _suite(item) == []
    error = client.get("/health").json()["ci_pull"]["last_error"]
    assert error == "the box could not read GitHub checks: GitHub answered 401"
    with pytest.raises(items_svc.MissingAttestation) as exc:
        items_svc.update_item(db, item.id, status="done")
    assert error in str(exc.value)


def test_no_token_makes_no_request_and_says_so(db, client, monkeypatch):
    monkeypatch.setattr(settings, "github_token", "", raising=False)
    item = _item(db, head=_sha("notoken"))
    calls = _github(monkeypatch, lambda url, headers: _Resp(200, _body(_check("test"))))
    ci_pull.run_once(db, now=0)
    assert calls == []
    db.refresh(item)
    assert _suite(item) == []
    error = client.get("/health").json()["ci_pull"]["last_error"]
    assert error == "the box could not read GitHub checks: no token is set"
    with pytest.raises(items_svc.MissingAttestation) as exc:
        items_svc.update_item(db, item.id, status="done")
    assert error in str(exc.value)


def _as_another_worker():
    """A non-leader has none of the leader's locals. Plant that emptiness so a
    read that consults this process, instead of `ci_pull_notes`, fails closed."""
    ci_pull._last_error = None
    ci_pull._observations = {}


@pytest.mark.parametrize("case,sentence,on_health", [
    ("401", "GitHub answered 401", True),
    ("404", "GitHub answered 404", True),
    ("notoken", "no token is set", True),
    ("zero", "no check-runs were found", False),
    ("running", "still running", False),
    ("truncated", "truncated", True),
])
def test_a_worker_that_did_not_run_the_loop_still_names_why(
        db, client, monkeypatch, case, sentence, on_health):
    """Compose runs N workers. The loop is the leader's. `/health` and the done
    refusal land on whichever worker the request hits, and that worker's module
    globals are empty. The sentence has to be the row."""
    if case == "notoken":
        monkeypatch.setattr(settings, "github_token", "", raising=False)
    if case == "truncated":
        monkeypatch.setattr(ci_pull, "MAX_PAGES", 1)
    item = _item(db, head=_sha(f"worker-{case}"))

    def responder(url, headers):
        if case == "401":
            return _Resp(401, {})
        if case == "404":
            return _Resp(404, {})
        if case == "zero":
            return _Resp(200, {"total_count": 0, "check_runs": []})
        if case == "running":
            return _Resp(200, _body(_check("test", status="in_progress", conclusion="")))
        if case == "truncated":
            return _Resp(200, {"total_count": 150, "check_runs": [_check("only")]})
        return _Resp(200, _body(_check("test")))

    _github(monkeypatch, responder)
    ci_pull.run_once(db, now=0)
    db.refresh(item)
    assert _suite(item) == []
    _as_another_worker()
    error = client.get("/health").json()["ci_pull"]["last_error"]
    if on_health:
        assert sentence in (error or "")
    else:
        assert error == ""
    with pytest.raises(items_svc.MissingAttestation) as exc:
        items_svc.update_item(db, item.id, status="done")
    assert sentence in str(exc.value)
    assert "nothing has attested it" in str(exc.value)


def test_github_404_writes_nothing_and_names_the_miss(db, client, monkeypatch):
    item = _item(db, head=_sha("missing"))
    _github(monkeypatch, lambda url, headers: _Resp(404, {}))
    ci_pull.run_once(db, now=0)
    db.refresh(item)
    assert _suite(item) == []
    error = client.get("/health").json()["ci_pull"]["last_error"]
    assert error == "the box could not read GitHub checks: GitHub answered 404"
    with pytest.raises(items_svc.MissingAttestation) as exc:
        items_svc.update_item(db, item.id, status="done")
    assert error in str(exc.value)


def test_a_401_does_not_keep_calling_the_heads_after_it(db, monkeypatch):
    _item(db, head=_sha("first-401"), github_url="https://github.com/one/alpha/pull/1")
    _item(db, head=_sha("second-401"), github_url="https://github.com/two/beta/pull/2")
    calls = _github(monkeypatch, lambda url, headers: _Resp(401, {}))
    ci_pull.run_once(db, now=0)
    assert len(calls) == 1
    assert "401" in (ci_pull.last_error() or "")


def test_a_404_does_not_stop_the_next_repository(db, monkeypatch):
    _item(db, head=_sha("gone"), github_url="https://github.com/one/alpha/pull/1")
    kept = _item(db, head=_sha("kept"), github_url="https://github.com/two/beta/pull/2")

    def responder(url, headers):
        if "/one/alpha/" in url:
            return _Resp(404, {})
        return _Resp(200, _body(_check("test")))

    calls = _github(monkeypatch, responder)
    ci_pull.run_once(db, now=0)
    assert len(calls) == 2
    db.refresh(kept)
    assert _suite(kept)[0][1][0]["passed"] is True


def test_rate_limit_backs_off_and_then_attests(db, monkeypatch):
    item = _item(db, head=_sha("limited"))
    seen = {"n": 0}

    def responder(url, headers):
        seen["n"] += 1
        if seen["n"] == 1:
            return _Resp(429, {}, {"Retry-After": "120"})
        return _Resp(200, _body(_check("test")))

    calls = _github(monkeypatch, responder)
    ci_pull.run_once(db, now=0)
    db.refresh(item)
    assert len(calls) == 1 and _suite(item) == []
    assert "rate limited" in (ci_pull.last_error() or "")
    ci_pull.run_once(db, now=30)
    assert len(calls) == 1
    ci_pull.run_once(db, now=121)
    db.refresh(item)
    assert len(calls) == 2
    assert _suite(item)[0][1][0]["passed"] is True


def test_a_failure_on_page_two_is_not_a_pass(db, monkeypatch):
    item = _item(db, head=_sha("paged"))

    def responder(url, headers):
        if url.endswith("page=1"):
            return _Resp(200, {"total_count": 2, "check_runs": [_check("unit")]})
        return _Resp(200, {"total_count": 2, "check_runs": [
            _check("lint", conclusion="failure", url="https://github.com/runs/lint")]})

    _github(monkeypatch, responder)
    ci_pull.run_once(db, now=0)
    db.refresh(item)
    pred = _suite(item)[0][1][0]
    assert pred["passed"] is False
    assert "lint (failure)" in pred["detail"]


def test_a_truncated_list_is_not_a_green_suite(db, monkeypatch):
    monkeypatch.setattr(ci_pull, "MAX_PAGES", 1)
    item = _item(db, head=_sha("cut"))
    _github(monkeypatch, lambda url, headers: _Resp(
        200, {"total_count": 150, "check_runs": [_check("only")]}))
    ci_pull.run_once(db, now=0)
    db.refresh(item)
    assert _suite(item) == []
    assert items_svc.has_valid_attestation(item.evidence, commit=item.head_commit) is False
    error = ci_pull.last_error()
    assert error == "the box could not read GitHub checks: the check-run list was truncated"
    with pytest.raises(items_svc.MissingAttestation) as exc:
        items_svc.update_item(db, item.id, status="done")
    assert error in str(exc.value)


# ---- who gets polled -----------------------------------------------------------------

def test_backlog_already_answered_and_gitlab_are_not_fetched(db, monkeypatch):
    _item(db, status="backlog", head=_sha("backlog"))
    answered = _sha("answered")
    _item(db, head=answered, evidence=[{
        "kind": "attestation", "adapter": "github-actions", "commit": answered,
        "predicates": [{"name": "suite_green", "passed": True, "detail": "ok"}],
    }])
    _item(db, head=_sha("gitlab"),
          github_url="https://gitlab.com/acme/widget/-/merge_requests/4")
    wanted = _item(db, head=_sha("wanted"))
    calls = _github(monkeypatch, lambda url, headers: _ok(url, headers, [_check("test")]))
    ci_pull.run_once(db, now=0)
    assert len(calls) == 1
    assert wanted.head_commit in calls[0]["url"]


def test_a_pull_request_url_in_evidence_is_enough(db, monkeypatch):
    head = _sha("evidence-url")
    item = _item(db, head=head, github_url="", evidence=[{
        "kind": "url", "url": PR, "detail": "PR",
    }])
    calls = _github(monkeypatch, lambda url, headers: _ok(url, headers, [_check("test")]))
    ci_pull.run_once(db, now=0)
    assert len(calls) == 1 and "asc-me/graphban" in calls[0]["url"]
    db.refresh(item)
    assert _suite(item)[0][1][0]["passed"] is True


def test_a_recorded_github_url_with_no_evidence_is_enough(db, monkeypatch):
    item = _item(db, head=_sha("recorded"), github_url=PR, evidence=None)
    calls = _github(monkeypatch, lambda url, headers: _ok(url, headers, [_check("test")]))
    ci_pull.run_once(db, now=0)
    assert len(calls) == 1
    db.refresh(item)
    assert _suite(item)[0][1][0]["passed"] is True


def test_health_says_the_loop_has_not_looked_until_a_pass_finishes(client):
    assert client.get("/health").json()["ci_pull"]["last_error"] is None


def test_a_pass_with_nothing_to_pull_clears_the_error(db, client, monkeypatch):
    ci_pull.run_once(db, now=0)
    assert client.get("/health").json()["ci_pull"]["last_error"] == ""


# ---- a token that cannot read check-runs (GRPH-1921) ---------------------------------
#
# A fine-grained token has no Checks permission GitHub will grant it, so
# `commits/{sha}/check-runs` answers 403 whatever else it can read. The head is
# then read from Actions instead: `actions/runs?head_sha=` plus each run's
# `/jobs`. Same judge, same attestation, same adapter.

RUN_URL = "https://github.com/asc-me/graphban/actions/runs/77"


def _run(run_id=77, name="ci", status="completed", conclusion="success", url=RUN_URL):
    return {"id": run_id, "name": name, "status": status, "conclusion": conclusion,
            "html_url": url}


def _runs_body(*runs, total=None):
    return {"total_count": len(runs) if total is None else total,
            "workflow_runs": list(runs)}


def _jobs_body(*jobs, total=None):
    return {"total_count": len(jobs) if total is None else total, "jobs": list(jobs)}


def _actions(monkeypatch, jobs_by_run, *, runs=None, checks_status=403):
    """Route check-runs to `checks_status` and serve Actions from `jobs_by_run`.

    `jobs_by_run` maps a run id to that run's jobs. `runs` defaults to one
    completed run per id, so a test states only the jobs it cares about.
    """
    ids = list(jobs_by_run)
    if runs is None:
        runs = [_run(rid) for rid in ids]

    def responder(url, headers):
        if "/check-runs" in url:
            return _Resp(checks_status, {})
        if "/actions/runs?" in url:
            return _Resp(200, _runs_body(*runs))
        for rid in ids:
            if f"/actions/runs/{rid}/jobs" in url:
                return _Resp(200, _jobs_body(*jobs_by_run[rid]))
        return _Resp(404, {})

    return _github(monkeypatch, responder)


def test_a_403_on_check_runs_falls_through_to_actions_and_attests_green(db, monkeypatch):
    head = "AbCdEf34" + "b" * 32
    item = _item(db, head=head)
    calls = _actions(monkeypatch, {
        77: [_check("test", url=f"{RUN_URL}/job/1")],
    })
    ci_pull.run_once(db, now=0)
    db.refresh(item)
    [(receipt, preds)] = _suite(item)
    assert receipt["adapter"] == "github-checks"
    assert receipt["commit"] == head
    assert preds == [{"name": "suite_green", "passed": True,
                      "detail": "all 1 jobs completed and none failed"}]
    # The run URL points at the Actions run, not at the job inside it.
    assert receipt["run_ref"] == RUN_URL
    # Check-runs is still tried first; Actions is the fallback, not the route.
    assert "/check-runs" in calls[0]["url"]
    assert f"/actions/runs?head_sha={head.lower()}" in calls[1]["url"]
    assert f"/actions/runs/77/jobs" in calls[2]["url"]
    assert calls[2]["headers"]["Authorization"] == "Bearer test-token"
    # The 403 that fell through is not a problem to report: Actions answered.
    assert ci_pull.last_error() == ""
    done = items_svc.update_item(db, item.id, status="done")
    assert done.status == "done"
    assert items_svc.CI_PREDICATE in items_svc.attested_predicates(done.evidence, commit=head)


def test_a_token_that_reads_check_runs_never_calls_actions(db, monkeypatch):
    item = _item(db, head=_sha("classic-token"))
    calls = _github(monkeypatch, lambda url, headers: _ok(url, headers, [_check("test")]))
    ci_pull.run_once(db, now=0)
    db.refresh(item)
    [(_, preds)] = _suite(item)
    assert preds == [{"name": "suite_green", "passed": True,
                      "detail": "all 1 check-runs completed and none failed"}]
    assert len(calls) == 1
    assert "actions" not in calls[0]["url"]


@pytest.mark.parametrize("conclusion", sorted(ci_pull.FAIL_CONCLUSIONS))
def test_a_completed_actions_job_that_failed_names_that_job(db, monkeypatch, conclusion):
    item = _item(db, head=_sha(f"actions-{conclusion}"))
    _actions(monkeypatch, {77: [
        _check("postgres"),
        _check("test", conclusion=conclusion, url=f"{RUN_URL}/job/2"),
    ]})
    ci_pull.run_once(db, now=0)
    db.refresh(item)
    [(receipt, preds)] = _suite(item)
    assert preds == [{"name": "suite_green", "passed": False,
                      "detail": f"failing job: test ({conclusion})"}]
    assert receipt["run_ref"] == RUN_URL
    assert ci_pull.last_error() == ""


def test_an_actions_job_still_running_writes_nothing(db, monkeypatch):
    item = _item(db, head=_sha("actions-running"))
    _actions(monkeypatch, {77: [
        _check("test"),
        _check("lint", status="in_progress", conclusion=""),
    ]})
    ci_pull.run_once(db, now=0)
    db.refresh(item)
    assert _suite(item) == []
    assert items_svc.has_valid_attestation(item.evidence, commit=item.head_commit) is False
    assert ci_pull.last_error() == ""
    with pytest.raises(items_svc.MissingAttestation) as exc:
        items_svc.update_item(db, item.id, status="done")
    assert "still running" in str(exc.value)


def test_a_queued_run_with_no_jobs_yet_writes_nothing(db, monkeypatch):
    """A run that has not produced a job is not an empty, therefore green, list."""
    item = _item(db, head=_sha("actions-queued"))
    _actions(monkeypatch, {77: []}, runs=[_run(77, status="queued", conclusion="")])
    ci_pull.run_once(db, now=0)
    db.refresh(item)
    assert _suite(item) == []
    with pytest.raises(items_svc.MissingAttestation) as exc:
        items_svc.update_item(db, item.id, status="done")
    assert "still running" in str(exc.value)


def test_zero_actions_runs_is_not_a_green_suite(db, monkeypatch):
    item = _item(db, head=_sha("actions-noruns"))
    calls = _actions(monkeypatch, {}, runs=[])
    ci_pull.run_once(db, now=0)
    db.refresh(item)
    assert _suite(item) == []
    assert items_svc.has_valid_attestation(item.evidence, commit=item.head_commit) is False
    # An absent head is not a fault in the token, so it is not on `/health`.
    assert ci_pull.last_error() == ""
    with pytest.raises(items_svc.MissingAttestation) as exc:
        items_svc.update_item(db, item.id, status="done")
    assert "no workflow runs were found" in str(exc.value)
    # The runs list was read and answered empty; the jobs call needs a run id.
    assert any("/actions/runs?" in call["url"] for call in calls)
    assert not any("/jobs" in call["url"] for call in calls)


def test_a_failure_in_a_later_run_names_that_run(db, monkeypatch):
    """Two workflows at one head: the green one must not hide the red one."""
    item = _item(db, head=_sha("actions-two-runs"))
    _actions(monkeypatch, {
        77: [_check("test")],
        88: [_check("lint", conclusion="failure", url="https://github.com/r/88/job/1")],
    }, runs=[
        _run(77, url="https://github.com/asc-me/graphban/actions/runs/77"),
        _run(88, name="lint", url="https://github.com/asc-me/graphban/actions/runs/88"),
    ])
    ci_pull.run_once(db, now=0)
    db.refresh(item)
    [(receipt, preds)] = _suite(item)
    assert preds == [{"name": "suite_green", "passed": False,
                      "detail": "failing job: lint (failure)"}]
    assert receipt["run_ref"] == "https://github.com/asc-me/graphban/actions/runs/88"


def test_a_rate_limited_403_does_not_fall_through_to_actions(db, monkeypatch):
    """A spent budget is not a missing permission: retrying Actions would spend
    the same budget twice and report a green head that was never read."""
    item = _item(db, head=_sha("actions-ratelimit"))
    calls = _github(monkeypatch, lambda url, headers: _Resp(
        403, {}, headers={"retry-after": "30"}))
    ci_pull.run_once(db, now=0)
    db.refresh(item)
    assert _suite(item) == []
    assert all("/actions/" not in call["url"] for call in calls)
    assert "rate limited" in (ci_pull.last_error() or "")


def test_a_truncated_workflow_run_list_is_not_a_green_suite(db, monkeypatch):
    monkeypatch.setattr(ci_pull, "MAX_PAGES", 1)
    item = _item(db, head=_sha("actions-truncated"))

    def responder(url, headers):
        if "/check-runs" in url:
            return _Resp(403, {})
        return _Resp(200, _runs_body(_run(1), total=150))

    _github(monkeypatch, responder)
    ci_pull.run_once(db, now=0)
    db.refresh(item)
    assert _suite(item) == []
    assert "truncated" in (ci_pull.last_error() or "")


def test_a_token_that_can_read_neither_route_names_the_permission(db, client, monkeypatch):
    item = _item(db, head=_sha("actions-both-403"))
    calls = _github(monkeypatch, lambda url, headers: _Resp(403, {}))
    ci_pull.run_once(db, now=0)
    db.refresh(item)
    assert _suite(item) == []
    error = client.get("/health").json()["ci_pull"]["last_error"]
    assert "neither check-runs nor Actions runs" in error
    assert "Actions: Read" in error
    with pytest.raises(items_svc.MissingAttestation) as exc:
        items_svc.update_item(db, item.id, status="done")
    assert "Actions: Read" in str(exc.value)
    # Both routes were tried, so the sentence is about the token and not a guess.
    assert any("/check-runs" in call["url"] for call in calls)
    assert any("/actions/runs?" in call["url"] for call in calls)


# ---- the loop, and the docs ----------------------------------------------------------

def test_lifespan_starts_and_stops_the_pull_loop():
    from app import main
    src = inspect.getsource(main.lifespan)
    assert "_ci_pull_loop" in src
    assert "ci_pull_seconds" in src
    assert "ci_pull_task" in src


def test_a_raising_pass_does_not_end_the_loop(monkeypatch):
    import app.main as main

    calls = {"n": 0}

    def boom():
        calls["n"] += 1
        raise RuntimeError("boom")

    monkeypatch.setattr(main, "_ci_pull_pass", boom)
    monkeypatch.setattr(main.settings, "ci_pull_seconds", 0.01, raising=False)

    async def drive():
        task = asyncio.create_task(main._ci_pull_loop())
        deadline = asyncio.get_event_loop().time() + 2
        while calls["n"] < 2 and asyncio.get_event_loop().time() < deadline:
            await asyncio.sleep(0.02)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(drive())
    assert calls["n"] >= 2


def test_the_pull_route_and_its_token_scope_are_documented():
    runners = (ROOT / "docs" / "ci-runners.md").read_text()
    exposure = (ROOT / "docs" / "self-host-exposure.md").read_text()
    config = (ROOT / "docs" / "configuration.md").read_text()
    flat = " ".join(runners.split())
    assert "behind NAT" in runners
    assert "api.github.com" in runners
    assert "github-checks" in runners
    assert "Actions: Read" in runners
    assert "no Checks permission" in runners
    assert "classic personal access token with the `repo` scope" in runners
    assert "Zero check-runs is not a green suite" in flat
    assert "CI_PULL_SECONDS" in runners
    assert "outbound HTTPS to" in exposure and "api.github.com" in exposure
    assert "Actions: Read" in exposure
    assert "Do not open a tunnel" in exposure
    assert "CI_PULL_SECONDS" in config
    assert "does not fall through" in config
