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


# ---- the Merger acts on a conflict: the item is bounced, not left watched ----------------------

class _BounceLedger:
    """A fake Graphban `call` for the Merger: one done item, and a record of every bounce."""

    def __init__(self, item_id: str, *, bounce_raises: Exception | None = None):
        from tests.test_merge_after_sign_off import _done_item

        self.item_id = item_id
        self.item = _done_item(item_id)
        # gbfleet opened this item's PR, so the Merger holds its merge (the server only
        # bounces a `done` item that carries this receipt).
        self.item["evidence"].append({"kind": "url", "url": "https://github.com/o/r/pull/9",
                                      "detail": f"draft PR opened by gbfleet for {self.item['branch']}"})
        self.bounce_raises = bounce_raises
        self.bounces: list[dict] = []

    def call(self, tool, **kw):
        if tool == "fleet_status":
            return {"agents": [], "release_ref": {"ref": None, "source": "unmeasured"}}
        if tool == "search_items":
            return {"results": [self.item]}
        if tool == "get_item_details":
            return dict(self.item)
        if tool == "bounce":
            if self.bounce_raises is not None:
                raise self.bounce_raises
            # The server's rule (fleet._require_bounceable): `review`, or `done` with a
            # gbfleet PR receipt and no merge receipt. Anything else is refused, as the real
            # service refuses it.
            details = [str(e.get("detail") or "") for e in self.item.get("evidence") or []]
            merged = any(d.startswith("merged by gbfleet") for d in details)
            proposed = any(d.startswith("draft PR opened by gbfleet") for d in details)
            if not (self.item["status"] == "review"
                    or (self.item["status"] == "done" and proposed and not merged)):
                from gbfleet.client import ToolFailed
                raise ToolFailed("bounce", "conflict",
                                 f"{kw['id']} is {self.item['status']}, not awaiting review")
            self.bounces.append(kw)
            self.item["status"] = "next"
            return {"id": kw["id"], "status": "next"}
        raise AssertionError(f"unexpected call {tool}")


def test_THE_CALL_the_merger_bounces_a_rebase_conflict_naming_the_files(gh, monkeypatch):
    """Sabotage: delete `if got.conflicts: self._bounce(...)` in Merger._attempt -> fails."""
    from gbfleet.supervisor import Merger, Wave

    monkeypatch.setattr(propose_mod, "rebase_conflicts", lambda *a: ["a.txt", "b.txt"])
    gh(_Behind(update_ok=False))
    ledger = _BounceLedger("SA-1389")
    merger = Merger(REPO, ledger, enabled=True)
    wave = Wave()

    assert merger.tick(wave) is False

    assert [b["id"] for b in ledger.bounces] == ["SA-1389"]
    reason = ledger.bounces[0]["reason"]
    assert "a.txt" in reason and "b.txt" in reason
    assert "SA-1389" not in merger.watching
    assert not wave.failures


def test_a_bounce_the_server_refuses_is_reported_and_the_item_stays_watched(gh, monkeypatch):
    from gbfleet.client import ToolFailed
    from gbfleet.supervisor import Merger, Wave

    monkeypatch.setattr(propose_mod, "rebase_conflicts", lambda *a: ["x.py"])
    gh(_Behind(update_ok=False))
    ledger = _BounceLedger("SA-1390", bounce_raises=ToolFailed("bounce", "refused", "nope"))
    merger = Merger(REPO, ledger, enabled=True)
    wave = Wave()

    merger.tick(wave)

    assert not ledger.bounces
    assert "SA-1390" in merger.watching  # asked again on a later tick
    assert wave.failures and "x.py" in wave.failures[0] and "could not bounce" in wave.failures[0]


# ---- a drain spawns alongside a soft lease, and says so -----------------------------------------

def test_a_soft_overlap_is_spawned_into_and_reported_not_waited_on(monkeypatch):
    """The server marks an overlap git can merge as `soft_overlap`, never `held_by`. The
    drain counts it as free work and reports the overlap instead of "Not spawning"."""
    from gbfleet import until

    events = []
    monkeypatch.setattr(until.observe, "emit", lambda ev, **kw: events.append((ev, kw)))
    until._sharing.clear()
    cluster = {"items": ["GRPH-2"], "areas": ["backend/app/x.py"],
               "soft_overlap": [{"area": "backend/app/x.py", "reserved": "backend/app/*",
                                 "by": "GRPH-A9"}]}
    replies = {"collision_clusters": {"clusters": [cluster]}, "fleet_status": {"agents": []}}

    class _Stub:
        def call(self, tool, **_kw):
            return replies[tool]

    assert until._wanted_workers(_Stub(), _Stub(), live_n=0, max_workers=2) == 1
    assert len(events) == 1 and events[0][0] == "contention"
    detail = events[0][1]["detail"]
    assert "GRPH-A9" in detail and "backend/app/* vs backend/app/x.py" in detail
    assert "Spawning anyway" in detail and "Not spawning" not in detail
    # Said once, not every tick.
    until._wanted_workers(_Stub(), _Stub(), live_n=0, max_workers=2)
    assert len(events) == 1
    until._sharing.clear()
