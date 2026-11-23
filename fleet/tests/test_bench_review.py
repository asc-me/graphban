from __future__ import annotations

import os
import shlex
import subprocess
import sys
from pathlib import Path

import pytest

from gbfleet.bench import review as R
from gbfleet.bench.harvest import Candidate
from gbfleet.bench.score import score_run
from gbfleet.bench.suite import Check, Task

PYTEST = f"{shlex.quote(sys.executable)} -m pytest -q -p no:cacheprovider"


def git(repo: Path, *args: str) -> str:
    env = {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t",
           "GIT_COMMITTER_EMAIL": "t@t", "PATH": os.environ["PATH"], "HOME": str(repo)}
    return subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True,
                          text=True, env=env).stdout.strip()


@pytest.fixture
def repo(tmp_path):
    r = tmp_path / "repo"
    r.mkdir()
    git(r, "init", "-q", "-b", "main")
    (r / "mod.py").write_text("def add(a, b):\n    return a + b\n")
    (r / "test_mod.py").write_text("from mod import add\n\n\ndef test_add():\n    assert add(1, 2) == 3\n")
    git(r, "add", "-A")
    git(r, "commit", "-q", "-m", "clean")
    return r


MUT = {"path": "mod.py", "old": "a + b", "new": "a - b"}


def task(repo: Path, expect: str, ref: str) -> Task:
    return Task(id=f"r-{expect}", kind="review", base_sha=git(repo, "rev-parse", "HEAD"), title="t",
                description="d", acceptance=(), touchpoints=("mod.py",), capabilities=("F2",),
                size_band="S", check=Check(PYTEST, ("test_mod.py",)), expect=expect, ref=ref)


def defect(repo: Path) -> str:
    sha = R.apply_mutation(repo, "HEAD", "d1", MUT)
    assert sha
    return sha


def test_signoff_on_planted_ref_is_f2_miss(repo):
    t = task(repo, "bounce", defect(repo))
    res = R.run_review_task(t, repo=repo, reviewer=lambda _t: "sign_off")
    assert res.outcome == "sign_off"
    s = score_run(R.to_outcomes([t], [res]))
    assert (s.f2_n, s.f2) == (1, 0.0)


def test_bounce_on_clean_ref_counts_against_f1(repo):
    t = task(repo, "sign_off", "HEAD")
    res = R.run_review_task(t, repo=repo, reviewer=lambda _t: "bounce")
    s = score_run(R.to_outcomes([t], [res]))
    assert (s.f1_n, s.f1) == (1, 0.0)


def test_planted_ref_that_stays_green_is_harness_error(repo):
    t = task(repo, "bounce", "HEAD")  # clean ref labelled as the defect
    called = []
    res = R.run_review_task(t, repo=repo, reviewer=lambda _t: called.append(1) or "bounce")
    assert res.outcome == "harness_error" and "green_at_defect" in res.detail and not called


def test_clean_ref_that_is_red_is_harness_error(repo):
    t = task(repo, "sign_off", defect(repo))
    res = R.run_review_task(t, repo=repo, reviewer=lambda _t: "sign_off")
    assert res.outcome == "harness_error" and "red_at_clean" in res.detail


def test_reviewer_crash_is_harness_error(repo):
    def boom(_t):
        raise RuntimeError("nope")
    res = R.run_review_task(task(repo, "sign_off", "HEAD"), repo=repo, reviewer=boom)
    assert res.outcome == "harness_error"


def cand(repo: Path, item: str) -> Candidate:
    sha = git(repo, "rev-parse", "HEAD")
    return Candidate(id=f"h-{item}", merge_sha=sha, base_sha=sha, title=f"merge {item}", command=PYTEST,
                     tests=["test_mod.py"], touchpoints=["mod.py"], item=item, sabotage=True)


def test_harvest_proposes_review_pair_from_applicable_sabotage(repo):
    tasks, dropped = R.propose_review_tasks(repo, [cand(repo, "GRPH-1")], {"GRPH-1": MUT})
    assert not dropped
    by = {t["expect"]: t for t in tasks}
    assert set(by) == {"bounce", "sign_off"} and all(t["kind"] == "review" for t in tasks)
    assert by["sign_off"]["ref"] == git(repo, "rev-parse", "HEAD")
    assert by["bounce"]["ref"] != by["sign_off"]["ref"]
    assert git(repo, "show", f"{by['bounce']['ref']}:mod.py").count("a - b") == 1


def test_harvest_drops_inapplicable_mutation(repo):
    bad = {"path": "mod.py", "old": "no such text", "new": "x"}
    tasks, dropped = R.propose_review_tasks(repo, [cand(repo, "GRPH-2")], {"GRPH-2": bad})
    assert tasks == [] and [d.reason for d in dropped] == ["mutation_not_applicable"]
    missing = {"path": "gone.py", "old": "a", "new": "b"}
    _, dropped = R.propose_review_tasks(repo, [cand(repo, "GRPH-3")], {"GRPH-3": missing})
    assert [d.reason for d in dropped] == ["mutation_not_applicable"]


# --- verdict_of: reading a live reviewer's verdict (GRPH-1874) -------------------------------

def test_verdict_of_reads_done_as_sign_off():
    assert R.verdict_of({"status": "done"}) == ("sign_off", "")


def test_verdict_of_reads_a_bounce_off_bounce_reason_not_reviewed_by():
    """`fleet.bounce` clears `reviewed_by`; a reader keyed on it never sees a bounce."""
    item = {"status": "next", "reviewed_by": None, "bounce_reason": "returns the first element"}
    assert R.verdict_of(item) == ("bounce", "")


def test_verdict_of_reads_a_gate_refused_sign_off_as_sign_off():
    item = {"status": "review", "evidence": [
        {"kind": "note", "detail": "refused at `done` (pr_ci_not_green): no suite_green"}]}
    outcome, detail = R.verdict_of(item)
    assert outcome == "sign_off" and "pr_ci_not_green" in detail


def test_verdict_of_untouched_item_is_no_verdict():
    assert R.verdict_of({"status": "review", "evidence": []})[0] == "no_verdict"
    assert R.verdict_of({"status": "next"})[0] == "no_verdict"


def test_bench_pr_is_a_pr_url_that_cannot_resolve():
    url = R.bench_pr("template", "review-x defect")
    assert "/pull/" in url and ".invalid/" in url and " " not in url
