"""GRPH-1393: the touchpoint PR gate can be run by the child, while it can still declare."""
from __future__ import annotations

import subprocess
from pathlib import Path

from gbfleet import touchpoints as tp
from gbfleet.cli import gate_report, main


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=str(cwd), capture_output=True, text=True,
                          check=True).stdout


def test_gate_report_names_files_and_the_declaring_call():
    out = gate_report(tp.gate_missing(["a/x.py", "b/y.py"], ["a/x.py"]), item_id="GRPH-1")
    assert "b/y.py" in out and "a/x.py" not in out
    assert 'update_item(id="GRPH-1"' in out and "touchpoints=['b/y.py']" in out
    assert '"kind": "note"' in out


def test_gate_report_is_empty_when_covered_or_undeclared_or_generated():
    assert tp.gate_missing(["a/x.py"], ["a"]) == []
    assert tp.gate_missing(["a/x.py"], []) == []
    assert tp.gate_missing(["AGENTS.md"], ["a"], ignore=lambda f: f == "AGENTS.md") == []


def test_cli_check_touchpoints_exit_codes(git_repo: Path, capsys):
    (git_repo / "new.py").write_text("x = 1\n", encoding="utf-8")
    assert main(["check-touchpoints", "--path", str(git_repo), "--item", "GRPH-9",
                 "--declared", "other.py"]) == 1
    assert "new.py" in capsys.readouterr().out
    assert main(["check-touchpoints", "--path", str(git_repo), "--declared", "new.py"]) == 0


# ---- GRPH-1591: a refused gate holds the item out of review ------------------------------------

import time  # noqa: E402

import pytest  # noqa: E402

from gbfleet import propose as propose_mod  # noqa: E402
from gbfleet import supervisor as sup  # noqa: E402
from gbfleet import worktree as wt  # noqa: E402
from gbfleet.spawn import Child  # noqa: E402

STRAY = "feature.py-E"


class _Live:
    pid = 4343
    returncode = None

    def poll(self):
        return None


class _Ledger:
    """A ledger that answers the three reads `_publish_in_review` makes and records posts."""

    allowed = frozenset({"search_items", "get_item_details", "get_rule_toggles"})

    def __init__(self, declared: list[str], review: list[str], mode: str | None = None):
        self.declared = declared
        self.review = review
        self.mode = mode
        self.observations: list[dict] = []
        self.attempts: list[dict] = []

    def call(self, tool, /, **kw):
        if tool == "search_items":
            return {"results": [{"id": i, "status": "review"} for i in self.review]}
        if tool == "get_item_details":
            return {"id": kw["id"], "touchpoints": list(self.declared)}
        if tool == "get_rule_toggles":
            toggles = [] if self.mode is None else [
                {"rule_id": "build.undeclared_files", "params": {"value": {"mode": self.mode}}}]
            return {"toggles": toggles}
        return {}

    def post_observation(self, **payload):
        self.observations.append(payload)
        return {"recorded": True}

    def post_attempt(self, **payload):
        self.attempts.append(payload)
        return {"id": "at_1"}


@pytest.fixture
def stray_builder(git_repo: Path, tmp_path: Path):
    """A running bound child whose branch carries the work AND a BSD `sed -i -E` backup."""
    bare = tmp_path / "origin.git"
    subprocess.run(["git", "init", "-q", "--bare", str(bare)], check=True)
    _git(git_repo, "remote", "add", "origin", str(bare))
    tree = wt.create(git_repo, tmp_path / "kid", wave="w", agent_id="A1")
    (tree.path / "feature.py").write_text("x = 1\n", encoding="utf-8")
    (tree.path / STRAY).write_text("x = 0\n", encoding="utf-8")
    _git(tree.path, "add", "-A")
    _git(tree.path, "commit", "-qm", "GRPH-1591: the work, and a stray backup")
    child = Child(adapter="fake", worktree=tree.path, branch=tree.branch, base=tree.base,
                  seat_path=tmp_path / "seat.json", process=_Live(),
                  started_at=time.monotonic(), log_dir=tmp_path / "logs",
                  seat_id="seat-1", held_items=["GRPH-1"])
    child.log_dir.mkdir(parents=True, exist_ok=True)
    return child, bare


def _on_remote(bare: Path, branch: str) -> bool:
    return branch in subprocess.run(["git", "-C", str(bare), "branch", "--list"],
                                    capture_output=True, text=True).stdout


def test_an_item_in_review_with_a_file_outside_touchpoints_is_held_not_published(
        stray_builder):
    """GRPH-1587's path: the item reached review while its child kept running.

    No push, no `branch_published` release to `claim_review`, and one `undeclared_held`
    observation naming the stray file — which the server turns into `in_progress` plus a
    directive. Sabotage: make `_held_for_undeclared` return False and the branch is pushed,
    released, and nothing names the file.
    """
    child, bare = stray_builder
    ledger = _Ledger(["feature.py"], ["GRPH-1"])
    wave = sup.Wave()

    sup._publish_in_review(wave, [child], ledger)

    assert not _on_remote(bare, child.branch), "a gated branch was published for review"
    assert not [a for a in ledger.attempts if a.get("branch_published")]
    held = [o for o in ledger.observations if o.get("kind") == "undeclared_held"]
    assert len(held) == 1 and held[0]["item_id"] == "GRPH-1"
    assert STRAY in held[0]["reason"] and "feature.py," not in held[0]["reason"]
    assert held[0]["branch"] == child.branch
    assert child.branch not in wave.review_publish_tried



def _holds(ledger) -> list[dict]:
    return [o for o in ledger.observations if o.get("kind") == "undeclared_held"]


def test_a_second_return_to_review_without_a_fix_is_held_again(stray_builder):
    """The bounce on 96a78f84: the hold deduped per file list for the life of the wave, so a
    builder that moved the item back to review without touching the branch left it in review
    with no PR and nothing posted. Sabotage: restore the per-list dedupe and this gets 1."""
    child, bare = stray_builder
    ledger = _Ledger(["feature.py"], ["GRPH-1"])
    wave = sup.Wave()
    sup._publish_in_review(wave, [child], ledger)
    ledger.review = []  # the server recorded the hold and took the item out of review
    sup._publish_in_review(wave, [child], ledger)
    assert len(_holds(ledger)) == 1
    ledger.review = ["GRPH-1"]  # the builder moved it back, the stray file still committed

    sup._publish_in_review(wave, [child], ledger)

    assert len(_holds(ledger)) == 2
    assert not _on_remote(bare, child.branch)


def test_a_resubmission_between_ticks_is_held_again(stray_builder):
    """The builder may move the item back before any tick saw it out of review. Seeing it in
    review after a recorded hold is a new episode either way."""
    child, _ = stray_builder
    ledger = _Ledger(["feature.py"], ["GRPH-1"])
    wave = sup.Wave()
    sup._publish_in_review(wave, [child], ledger)
    sup._publish_in_review(wave, [child], ledger)
    assert len(_holds(ledger)) == 2


class _OldLedger(_Ledger):
    """A server that predates `undeclared_held`: it refuses the kind and moves nothing."""

    def post_observation(self, **payload):
        raise RuntimeError("422 unknown observation kind 'undeclared_held'")


def test_a_refused_hold_is_not_reposted_every_tick(stray_builder):
    child, bare = stray_builder
    ledger = _OldLedger(["feature.py"], ["GRPH-1"])
    wave = sup.Wave()
    for _ in range(3):
        sup._publish_in_review(wave, [child], ledger)
    assert wave.observations_refused == 1
    assert not _on_remote(bare, child.branch)


def test_the_reap_gate_holds_again_only_an_item_back_in_review(stray_builder):
    """The early check held the branch; the reap re-measures it. An item still out of review
    is not held twice, and one its builder moved back before exiting is."""
    child, _ = stray_builder
    ledger = _Ledger(["feature.py"], ["GRPH-1"])
    wave = sup.Wave()
    sup._publish_in_review(wave, [child], ledger)
    ledger.review = []
    sup._hold_undeclared(wave, ledger, ["GRPH-1"], child.branch, [STRAY])
    assert len(_holds(ledger)) == 1
    ledger.review = ["GRPH-1"]
    sup._hold_undeclared(wave, ledger, ["GRPH-1"], child.branch, [STRAY])
    assert len(_holds(ledger)) == 2


def test_once_declared_the_same_head_is_published(stray_builder):
    """The builder widened its touchpoints instead of removing the file: no new commit, and
    the next review is re-judged rather than skipped as already tried."""
    child, bare = stray_builder
    ledger = _Ledger(["feature.py"], ["GRPH-1"])
    wave = sup.Wave()
    sup._publish_in_review(wave, [child], ledger)
    ledger.declared = ["feature.py", STRAY]

    sup._publish_in_review(wave, [child], ledger)

    assert _on_remote(bare, child.branch)
    assert [a for a in ledger.attempts if a.get("branch_published")]


def test_warn_mode_publishes_and_holds_nothing(stray_builder):
    child, bare = stray_builder
    ledger = _Ledger(["feature.py"], ["GRPH-1"], mode="warn")
    sup._publish_in_review(sup.Wave(), [child], ledger)
    assert _on_remote(bare, child.branch)
    assert not [o for o in ledger.observations if o.get("kind") == "undeclared_held"]


def test_the_reap_gate_also_sends_the_item_back(monkeypatch):
    """The item reached review after its child exited: the reap's refused PR posts the hold
    too. Sabotage: drop the `_hold_undeclared` call in `propose_branch` and this fails."""
    monkeypatch.setattr(wt, "remote_for", lambda repo: "origin")
    monkeypatch.setattr(wt, "default_ref", lambda repo, remote: "origin/main")
    monkeypatch.setattr(propose_mod, "subject", lambda repo, branch, base: "GRPH-1: work")
    opened = []
    monkeypatch.setattr(propose_mod, "propose", lambda *a, **k: opened.append(a))
    wave = sup.Wave()
    wave.undeclared["gb/w-1"] = [STRAY]
    ledger = _Ledger(["feature.py"], [])

    sup.propose_branch(wave, Path("."), "gb/w-1", ["GRPH-1"], client=ledger)

    assert opened == []
    kinds = [o["kind"] for o in ledger.observations]
    assert "withheld" in kinds and "undeclared_held" in kinds
    held = next(o for o in ledger.observations if o["kind"] == "undeclared_held")
    assert STRAY in held["reason"]
