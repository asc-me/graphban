"""A dependency whose PR is known unmerged holds its dependent — every one of them (GRPH-950).

Reported from SA-P21: SA-558 was signed off while its PR #397 was open. A child claimed the
dependent SA-556 from origin/main and cherry-picked SA-558's commit in — three copies of one
item, COLLIDED on seven files, a manual `rebase --onto`. It got through two ways:

- **Gap A.** The forge said "not merged", but the attested commit was not in this clone, so
  the dependency read `unknown` and the child spawned.
- **Gap B.** Only the seed was checked. A sibling taken by `claim_cluster` or `claim_next`
  never was — and the server calls a done dependency met, so nothing else stopped it.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

from gbfleet import deps, seat
from gbfleet import propose as propose_mod
from gbfleet import until


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


UNFETCHED = "0" * 40  # a commit this clone has never seen — SA-558's, on the child's machine


def _dep(item_id="SA-558", commit=UNFETCHED, pr="https://github.com/o/r/pull/397"):
    return {"id": item_id, "title": item_id, "status": "done", "link_types": ["dependency"],
            "evidence": [{"kind": "attestation", "commit": commit},
                         {"kind": "url", "url": pr}]}


def _forge(monkeypatch, state):
    monkeypatch.setattr(propose_mod, "view",
                        lambda *a, **kw: ({"state": state, "number": 397}, ""))


class Planner:
    def __init__(self, related):
        self.related = related

    def call(self, tool, **kw):
        assert tool == "related_work"
        return {"results": self.related.get(kw["id"], [])}


# ---- Gap A: the forge's answer is definite ------------------------------------------------------

def test_an_open_pr_with_an_unfetched_commit_is_absent(tmp_path, monkeypatch):
    """THE CASE. Sabotage: read the forge's "not merged" as `unknown` again → this spawns."""
    repo = _repo(tmp_path)
    _forge(monkeypatch, "OPEN")

    absent, unknown = deps.check(Planner({"SA-556": [_dep()]}), "SA-556", repo, "main")

    assert [d["id"] for d in absent] == ["SA-558"]
    assert unknown == []
    assert absent[0]["pr"] == "#397"


def test_a_closed_unmerged_pr_is_absent_too(tmp_path, monkeypatch):
    repo = _repo(tmp_path)
    _forge(monkeypatch, "CLOSED")

    absent, _ = deps.check(Planner({"SA-556": [_dep()]}), "SA-556", repo, "main")

    assert [d["id"] for d in absent] == ["SA-558"]


def test_an_unreachable_forge_with_an_unfetched_commit_stays_unknown(tmp_path, monkeypatch):
    """The one case that may still spawn: nobody could say."""
    repo = _repo(tmp_path)
    monkeypatch.setattr(propose_mod, "view", lambda *a, **kw: (None, "gh could not run"))

    absent, unknown = deps.check(Planner({"SA-556": [_dep()]}), "SA-556", repo, "main")

    assert absent == []
    assert [d["id"] for d in unknown] == ["SA-558"]


def test_the_hold_names_the_pr_it_waits_on():
    said = deps.waiting_on([{"id": "SA-558", "commits": ["abc"], "pr": "#397"}], "origin/main")

    assert said == "waiting on #397 (SA-558) to merge"
    assert "waiting on #397" in deps.explain(
        "SA-556", [{"id": "SA-558", "commits": ["abc123def"], "pr": "#397"}], "origin/main")


def test_without_a_pr_number_the_hold_names_the_item_and_base():
    said = deps.waiting_on([{"id": "SA-558", "commits": ["abc123def456"], "pr": ""}],
                           "origin/main")

    assert "SA-558" in said and "abc123def" in said and "origin/main" in said


# ---- Gap B: every member, not only the seed ---------------------------------------------------------

BRIEF = {"lane": {"value": "backend"}, "tier": {"value": "cheap"}, "blocked_by": []}


class Server:
    """Enough of Graphban.call for `_delegate_next`, `_recheck_holds` and `plan`."""

    def __init__(self, clusters, details, related):
        self.clusters = clusters
        self.details = details
        self.related = related
        self.delegated: list[str] = []
        self.writes: list[dict] = []

    def call(self, tool, **kw):
        if tool == "collision_clusters":
            return {"clusters": self.clusters}
        if tool == "get_item_details":
            return dict(self.details[kw["id"]])
        if tool == "related_work":
            return {"results": self.related.get(kw["id"], [])}
        if tool == "delegate":
            self.delegated.append(kw["id"])
            return {"enrolment_code": "WORKER-X"}
        if tool == "update_item":
            self.writes.append(kw)
            self.details[kw["id"]]["blocker"] = kw["blocker"]
            return {}
        if tool == "search_items":
            # Lean rows (the server default) carry id/title/status only — no `blocker`.
            full = kw.get("fields") == "full"
            return {"results": [{"id": k, **({"blocker": v.get("blocker", "")} if full else {})}
                                for k, v in self.details.items()
                                if kw["query"] in (v.get("blocker") or "")]}
        raise AssertionError(tool)


def _cluster_server():
    return Server(
        [{"items": ["SEED", "SIB"]}],
        {"SEED": {"id": "SEED", "status": "next", "brief": BRIEF, "blocker": ""},
         "SIB": {"id": "SIB", "status": "next", "brief": BRIEF, "blocker": ""}},
        {"SIB": [_dep()]},
    )


def test_a_sibling_with_an_unmerged_dependency_is_held_on_the_server(tmp_path, monkeypatch):
    """THE CALL for Gap B. The seed is clean and delegated; its sibling — which the bound child
    could `claim_cluster` — gets a blocker every claim path refuses.

    Sabotage: check only the seed (return at the first clean candidate before looking at the
    rest) → SIB carries no blocker and a child can take it."""
    repo = _repo(tmp_path)
    _forge(monkeypatch, "OPEN")
    server = _cluster_server()
    held: dict[str, list[str]] = {}
    delegated: set[str] = set()

    seed, _code, _want = until._delegate_next(server, "GRPH-A1", "w", delegated, None,
                                              repo=repo, base="main", held=held)

    assert seed == "SEED"
    assert held == {"SIB": ["SA-558"]}
    assert "SIB" in delegated
    [write] = server.writes
    assert write["id"] == "SIB"
    assert "waiting on #397 (SA-558) to merge" in write["blocker"]
    assert deps.is_hold(write["blocker"])


def test_a_held_seed_is_skipped_and_the_clean_sibling_delegated(tmp_path, monkeypatch):
    repo = _repo(tmp_path)
    _forge(monkeypatch, "OPEN")
    server = _cluster_server()
    server.related = {"SEED": [_dep()]}

    seed, _code, _want = until._delegate_next(server, "GRPH-A1", "w", set(), None,
                                              repo=repo, base="main", held={})

    assert seed == "SIB"
    assert [w["id"] for w in server.writes] == ["SEED"]


def test_a_blocker_somebody_else_wrote_is_not_overwritten(tmp_path, monkeypatch):
    repo = _repo(tmp_path)
    _forge(monkeypatch, "OPEN")
    server = _cluster_server()
    details = {"id": "SIB", "status": "next", "blocker": "needs a decision from Kim"}

    assert until._hold(server, "SIB", details, [_dep() | {"pr": "#397", "commits": ["x"]}],
                       "main") is False
    assert server.writes == []


def test_a_hold_is_lifted_when_the_pr_merges(tmp_path, monkeypatch):
    """Merged by a person, not by `--merge`: the recheck finds it and clears only its own
    blocker. Sabotage: skip the recheck → SIB stays blocked after #397 merged."""
    repo = _repo(tmp_path)
    _forge(monkeypatch, "OPEN")
    server = _cluster_server()
    held: dict[str, list[str]] = {}
    delegated: set[str] = set()
    until._delegate_next(server, "GRPH-A1", "w", delegated, None,
                         repo=repo, base="main", held=held)
    assert deps.is_hold(server.details["SIB"]["blocker"])

    _forge(monkeypatch, "MERGED")
    until._recheck_holds(server, held, delegated, repo, "main")

    assert server.details["SIB"]["blocker"] == ""
    assert held == {} and "SIB" not in delegated


def test_a_still_unmerged_hold_stays(tmp_path, monkeypatch):
    repo = _repo(tmp_path)
    _forge(monkeypatch, "OPEN")
    server = _cluster_server()
    held: dict[str, list[str]] = {}
    delegated: set[str] = set()
    until._delegate_next(server, "GRPH-A1", "w", delegated, None,
                         repo=repo, base="main", held=held)

    until._recheck_holds(server, held, delegated, repo, "main")

    assert "SIB" in held and deps.is_hold(server.details["SIB"]["blocker"])


def test_a_hold_an_earlier_wave_wrote_is_adopted():
    """A blocker nobody re-checks strands its item after the merge it waits on."""
    server = _cluster_server()
    server.details["SIB"]["blocker"] = f"waiting on #397 (SA-558) to merge {deps.HOLD_MARK}"
    server.details["SEED"]["blocker"] = "needs a person"
    held: dict[str, list[str]] = {}
    delegated: set[str] = set()

    until._adopt_holds(server, held, delegated)

    assert list(held) == ["SIB"] and delegated == {"SIB"}


# ---- visible before a wave spends anything ---------------------------------------------------------

def test_the_dry_run_shows_the_dependent_as_held(tmp_path, monkeypatch):
    repo = _repo(tmp_path)
    _forge(monkeypatch, "OPEN")
    server = _cluster_server()

    got = until.plan(server, prd=None, max_workers=3, repo=repo, base="main")

    assert got["dependency_holds"] == [
        {"item": "SIB", "waiting_on": "waiting on #397 (SA-558) to merge", "already_held": False}]
    assert server.writes == [], "a dry run writes nothing"


def test_the_child_is_told_a_missing_dependency_is_a_blocker_not_code_to_copy():
    for tmpl in (seat.INSTRUCTION, seat.BOUND_INSTRUCTION):
        assert seat.DEPENDENCY in tmpl
    assert "never code to copy in" in seat.DEPENDENCY
    assert "cherry-pick" in seat.DEPENDENCY
