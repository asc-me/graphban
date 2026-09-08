"""A child is not sent to build on a base that lacks its dependency (GRPH-798).

Reported from super-arc: SA-417 was signed off with a full attestation whose commit existed
only on `origin/gb/p11-m1-4`. Children branch from the remote default, so a dependent item was
green-lit by the tracker while its dependency was absent from the base it would be built on.

`done` means attested, not merged, and that is the design — an attestation binds to a commit
and nothing claims that commit went anywhere. `blocked_by` cannot help: it lists *unfinished*
dependencies, and this one is finished.

The tests below are mostly about the THREE answers. Reachable, provably-not-reachable, and
"this clone has never seen that commit" are different facts, and every way of collapsing them
into two is a bug in one direction or the other.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from gbfleet import deps
from gbfleet.worktree import reaches


def _repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    run = lambda *a: subprocess.run(["git", *a], cwd=repo, capture_output=True, text=True)
    run("init", "-q", "-b", "main")
    run("config", "user.email", "t@t.t")
    run("config", "user.name", "t")
    (repo / "a.txt").write_text("one\n")
    run("add", "-A")
    run("commit", "-qm", "one")
    return repo


def _sha(repo: Path, ref: str = "HEAD") -> str:
    return subprocess.run(["git", "rev-parse", ref], cwd=repo,
                          capture_output=True, text=True).stdout.strip()


def _branch_commit(repo: Path, name: str) -> str:
    run = lambda *a: subprocess.run(["git", *a], cwd=repo, capture_output=True, text=True)
    run("checkout", "-q", "-b", name)
    (repo / f"{name}.txt").write_text("work\n")
    run("add", "-A")
    run("commit", "-qm", name)
    sha = _sha(repo)
    run("checkout", "-q", "main")
    return sha


class Planner:
    def __init__(self, results):
        self.results = results

    def call(self, tool, **kw):
        assert tool == "related_work"
        return {"results": self.results}


def _dep(item_id, commit, *, status="done", link="dependency"):
    return {"id": item_id, "title": item_id, "status": status, "link_types": [link],
            "evidence": [{"kind": "attestation", "commit": commit}] if commit else []}


# ---- the three answers ------------------------------------------------------------------------

def test_a_commit_in_the_base_is_reachable(tmp_path):
    repo = _repo(tmp_path)
    assert reaches(repo, "main", _sha(repo)) is True


def test_a_commit_on_an_unmerged_branch_is_not(tmp_path):
    repo = _repo(tmp_path)
    assert reaches(repo, "main", _branch_commit(repo, "feature")) is False


def test_a_commit_this_clone_has_never_seen_is_unknown(tmp_path):
    """NOT False. Read as "unmerged" it would refuse every wave on a fresh clone; read as
    "merged" it would wave through the case this exists to catch."""
    repo = _repo(tmp_path)
    assert reaches(repo, "main", "0" * 40) is None


# ---- what the check does with them --------------------------------------------------------------

def test_an_unmerged_dependency_is_reported(tmp_path):
    repo = _repo(tmp_path)
    stranded = _branch_commit(repo, "dep-branch")

    absent, unknown = deps.check(Planner([_dep("SA-417", stranded)]), "SA-420", repo, "main")

    assert [d["id"] for d in absent] == ["SA-417"]
    assert unknown == []


def test_a_merged_dependency_is_not(tmp_path):
    repo = _repo(tmp_path)

    absent, unknown = deps.check(Planner([_dep("SA-417", _sha(repo))]), "SA-420", repo, "main")

    assert (absent, unknown) == ([], [])


def test_an_unresolvable_commit_is_unknown_not_absent(tmp_path):
    repo = _repo(tmp_path)

    absent, unknown = deps.check(Planner([_dep("SA-417", "0" * 40)]), "SA-420", repo, "main")

    assert absent == []
    assert [d["id"] for d in unknown] == ["SA-417"]


def test_one_reachable_commit_settles_it(tmp_path):
    """An item attested more than once — CI at one commit, a reviewer at another. If ANY of
    them is in the base, the work is there."""
    repo = _repo(tmp_path)
    stranded = _branch_commit(repo, "old-attempt")
    row = _dep("SA-417", stranded)
    row["evidence"].append({"kind": "attestation", "commit": _sha(repo)})

    absent, unknown = deps.check(Planner([row]), "SA-420", repo, "main")

    assert (absent, unknown) == ([], [])


# ---- what it deliberately ignores -----------------------------------------------------------------

def test_an_unfinished_dependency_is_left_to_blocked_by(tmp_path):
    """`blocked_by` already refuses those, and duplicating the rule here would put two
    definitions of "not ready" in the product."""
    repo = _repo(tmp_path)
    stranded = _branch_commit(repo, "wip")

    absent, _ = deps.check(Planner([_dep("SA-417", stranded, status="in_progress")]),
                           "SA-420", repo, "main")

    assert absent == []


def test_a_related_item_that_is_not_a_dependency_is_ignored(tmp_path):
    """`related_work` returns touchpoint neighbours too. Sharing a file is not depending on
    one, and holding a wave for it would stop nearly everything."""
    repo = _repo(tmp_path)
    stranded = _branch_commit(repo, "neighbour")

    absent, _ = deps.check(Planner([_dep("SA-417", stranded, link="code")]),
                           "SA-420", repo, "main")

    assert absent == []


def test_a_done_dependency_with_no_attested_commit_is_ignored(tmp_path):
    """Nothing to check against. Inventing a commit here would report a missing dependency
    for an item that may be perfectly merged; the completion gate is what has an opinion
    about a `done` with no attestation."""
    repo = _repo(tmp_path)

    absent, unknown = deps.check(Planner([_dep("SA-417", "")]), "SA-420", repo, "main")

    assert (absent, unknown) == ([], [])


def test_a_failed_lookup_is_not_evidence_of_anything(tmp_path):
    repo = _repo(tmp_path)

    class Broken:
        def call(self, tool, **kw):
            raise RuntimeError("server down")

    assert deps.check(Broken(), "SA-420", repo, "main") == ([], [])


def test_no_base_checks_nothing(tmp_path):
    """A repository with no remote default has nothing to be measured against, and guessing
    `main` would measure the wrong thing on a repository whose trunk is `develop`."""
    repo = _repo(tmp_path)
    stranded = _branch_commit(repo, "x")

    assert deps.check(Planner([_dep("SA-417", stranded)]), "SA-420", repo, "") == ([], [])


def test_the_explanation_names_the_item_the_commit_and_the_remedy(tmp_path):
    said = deps.explain("SA-420", [{"id": "SA-417", "title": "t", "commits": ["abc123def456"]}],
                        "origin/main")

    assert "SA-420" in said and "SA-417" in said
    assert "abc123def" in said
    assert "origin/main" in said
    assert "merge" in said
