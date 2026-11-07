"""Rebase before merge, and the two-heads backstop (GRPH-1389).

A PR behind its base is rebased and left for CI at the new head; a rebase conflict bounces
the item naming the files; a merge that would leave two alembic heads is refused.

Sabotage: make `heads_of` return `[]` -> the two-heads tests fail.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

from gbfleet import propose as propose_mod

from tests.test_propose import REVIEWED, REPO, _Forge, gh  # noqa: F401

_CFG = ["-c", "user.email=a@b.c", "-c", "user.name=t"]


class _Behind(_Forge):
    """A forge whose PR is BEHIND until `pr update-branch` runs."""

    def __init__(self, *, update_ok=True, **kw):
        super().__init__(draft=False, status="BEHIND", **kw)
        self.update_ok = update_ok

    def __call__(self, argv, **kw):
        if " ".join(argv[1:3]) == "pr update-branch":
            self.calls.append(argv)
            if not self.update_ok:
                return subprocess.CompletedProcess(argv, 1, "", "merge conflict between base and head")
            self.pr["mergeStateStatus"] = "CLEAN"
            return subprocess.CompletedProcess(argv, 0, "", "")
        return super().__call__(argv, **kw)


def _merge(forge, gh_wire):
    gh_wire(forge)
    return propose_mod.merge(REPO, "SA-1", "gb/w-1", reviewed=REVIEWED, green={REVIEWED})


def test_a_pr_behind_its_base_is_rebased_not_merged(gh):
    forge = _Behind()

    got = _merge(forge, gh)

    assert got.pending and not got.ok and "rebased" in got.checked
    assert "pr update-branch" in forge.verbs() and "pr merge" not in forge.verbs()
    assert "--rebase" in forge.calls[-1]


def test_a_rebase_conflict_bounces_naming_the_files(gh, monkeypatch):
    monkeypatch.setattr(propose_mod, "rebase_conflicts", lambda *a: ["a.txt", "b.txt"])
    forge = _Behind(update_ok=False)

    got = _merge(forge, gh)

    assert got.conflicts == ("a.txt", "b.txt")
    assert not got.ok and not got.pending and "a.txt, b.txt" in got.reason
    assert "pr merge" not in forge.verbs()


def test_a_dirty_pr_bounces_without_trying_to_rebase(gh, monkeypatch):
    monkeypatch.setattr(propose_mod, "rebase_conflicts", lambda *a: ["x.py"])
    forge = _Forge(draft=False, mergeable="CONFLICTING", status="DIRTY")

    got = _merge(forge, gh)

    assert got.conflicts == ("x.py",)
    assert "pr update-branch" not in forge.verbs()


def _migration(rev, down):
    return f'revision = "{rev}"\ndown_revision = {down!r}\n'.replace("'", '"')


def test_heads_of_counts_unparented_revisions():
    one = {"0001.py": _migration("0001", None), "0002.py": _migration("0002", "0001")}
    two = {**one, "0003.py": _migration("0003", "0001")}
    assert propose_mod.heads_of(one) == ["0002"]
    assert propose_mod.heads_of(two) == ["0002", "0003"]


def _git(repo: Path, *args: str) -> str:
    done = subprocess.run(["git", *_CFG, *args], cwd=repo, capture_output=True, text=True)
    assert done.returncode == 0, done.stderr
    return done.stdout.strip()


def _two_migration_repo(tmp_path: Path) -> tuple[Path, str]:
    """main gains 0002; a branch cut earlier gains its own 0003 off 0001. Each merges
    cleanly; together they are two heads."""
    repo = tmp_path / "r"
    vdir = repo / "backend" / "alembic" / "versions"
    vdir.mkdir(parents=True)
    subprocess.run(["git", "init", "-q", "-b", "main", str(repo)], check=True)
    (vdir / "0001.py").write_text(_migration("0001", None))
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "base")
    _git(repo, "checkout", "-q", "-b", "feat")
    (vdir / "0003.py").write_text(_migration("0003", "0001"))
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "feat")
    head = _git(repo, "rev-parse", "HEAD")
    _git(repo, "checkout", "-q", "main")
    (vdir / "0002.py").write_text(_migration("0002", "0001"))
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "main moves")
    return repo, head


def test_two_migrations_that_merge_cleanly_leave_two_heads(tmp_path):
    repo, head = _two_migration_repo(tmp_path)

    assert propose_mod.merged_alembic_heads(repo, "main", head) == ["0002", "0003"]


def test_a_merge_that_would_leave_two_heads_is_refused(gh, monkeypatch):
    monkeypatch.setattr(propose_mod, "merged_alembic_heads", lambda *a: ["0002", "0003"])
    forge = _Forge(draft=False)

    got = _merge(forge, gh)

    assert not got.ok and not got.pending and "2 alembic heads" in got.reason
    assert "pr merge" not in forge.verbs()


def test_one_head_merges(gh, monkeypatch):
    monkeypatch.setattr(propose_mod, "merged_alembic_heads", lambda *a: ["0002"])

    got = _merge(_Forge(draft=False), gh)

    assert got.ok and "alembic-heads=1" in got.checked


# ---- the Merger acts on the conflict: the item is bounced, naming the files -----------------

from gbfleet.client import ToolFailed  # noqa: E402
from gbfleet.supervisor import Merger, Wave  # noqa: E402

from tests.test_merge_after_sign_off import _done_item, _Ledger  # noqa: E402


class _BounceLedger(_Ledger):
    """The merge fakes' ledger, plus `bounce` — recorded, or refused when `refuse` is set."""

    def __init__(self, *a, refuse: Exception | None = None, **kw):
        super().__init__(*a, **kw)
        self.refuse = refuse

    def call(self, tool, **kw):
        if tool == "bounce":
            self.calls.append((tool, kw))
            if self.refuse is not None:
                raise self.refuse
            self.items[kw["id"]]["status"] = "next"
            return {"id": kw["id"], "status": "next"}
        return super().call(tool, **kw)


def test_the_merger_bounces_a_rebase_conflict_naming_the_files(gh, monkeypatch):
    """Sabotage: delete `if got.conflicts: self._bounce(...)` in `Merger._attempt` and this
    fails — the reviewer's mutation at de98d061 passed every test before it existed."""
    monkeypatch.setattr(propose_mod, "rebase_conflicts", lambda *a: ["a.txt", "b.txt"])
    gh(_Behind(update_ok=False))
    ledger = _BounceLedger({"SA-1": _done_item("SA-1")})
    merger = Merger(REPO, ledger, enabled=True)
    wave = Wave()

    merger.tick(wave)

    bounced = ledger.wrote("bounce")
    assert len(bounced) == 1 and bounced[0]["id"] == "SA-1"
    assert "a.txt" in bounced[0]["reason"] and "b.txt" in bounced[0]["reason"]
    assert "SA-1" not in merger.watching, "a bounced item is still watched for a merge"
    assert ledger.wrote("update_item") == [] and wave.failures == []


def test_a_bounce_the_ledger_refuses_is_a_wave_failure_naming_the_files(gh, monkeypatch):
    monkeypatch.setattr(propose_mod, "rebase_conflicts", lambda *a: ["x.py"])
    gh(_Behind(update_ok=False))
    ledger = _BounceLedger({"SA-1": _done_item("SA-1")}, refuse=ToolFailed("bounce", "conflict", "nope"))
    wave = Wave()

    Merger(REPO, ledger, enabled=True).tick(wave)

    assert len(wave.failures) == 1
    assert "SA-1" in wave.failures[0] and "x.py" in wave.failures[0]
    assert "could not bounce" in wave.failures[0]


def test_no_conflict_means_no_bounce(gh, monkeypatch):
    gh(_Behind())
    ledger = _BounceLedger({"SA-1": _done_item("SA-1")})

    Merger(REPO, ledger, enabled=True).tick(Wave())

    assert ledger.wrote("bounce") == []


# ---- the contention event names a soft overlap instead of "Not spawning" ----------------------

def test_a_soft_overlap_is_reported_as_spawning_not_waiting():
    from gbfleet import until

    free = [{"items": ["SA-1"], "soft_with": ["SA-A2"]}, {"items": ["SA-3"]}]
    line = until._overlapping(free)

    assert "1 cluster(s) overlap soft leases held by SA-A2" in line
    assert "Spawning anyway" in line and "Not spawning" not in line
    assert until._overlapping([{"items": ["SA-3"]}]) == ""
