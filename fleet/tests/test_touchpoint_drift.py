"""GRPH-785 — the reap-time check: did the partition's input hold?

The partition that keeps two workers off the same file is computed from `touchpoints`. The
supervisor already MEASURES what each child actually changed — one worker, one worktree, one
branch makes the diff boundary exact — and the two were never compared. The server unions
measured paths into the declaration, so the moment they are stored they stop being
distinguishable; the comparison had to happen before that, and nothing did it.
"""
from __future__ import annotations

import io

from gbfleet.cli import report
from gbfleet.supervisor import Wave, _declared_into, _note_touchpoints
from gbfleet.touchpoints import covers, overlaps, undeclared


class _Child:
    def __init__(self, branch, held):
        self.branch, self.held_items = branch, held


# ---- covers: broad on purpose -----------------------------------------------------------------

def test_a_directory_covers_what_is_under_it():
    assert covers("backend/app", "backend/app/services/fleet.py")
    assert covers("backend/app/services/fleet.py", "backend/app/services/fleet.py")
    assert covers("web/src/**/*.tsx", "web/src/features/fleet/FleetView.tsx")


def test_coverage_is_broad_because_a_narrow_test_would_cry_wolf():
    """This decides whether a changed file was already declared. Narrow means covered files
    read as undeclared, and a report that fires on normal work is one nobody reads. The
    server's `areas_collide` takes a union for the mirror-image reason."""
    # A sibling under the same parent — the parent-directory half of the server's rule.
    assert covers("backend/app/services", "backend/app/services/items.py")
    # And genuinely unrelated stays unrelated, or the check reports nothing ever.
    assert not covers("web", "backend/app/services/fleet.py")
    assert not covers("backend/app/services", "backend/app/routers/fleet.py")


# ---- undeclared -------------------------------------------------------------------------------

def test_a_file_no_touchpoint_covers_is_reported():
    """THE CHECK. Sabotage: return [] and a worker editing outside its declared areas — the
    partition's input being wrong — goes on producing no signal at all."""
    assert undeclared(["backend/app/services/fleet.py", "web/src/x.tsx"],
                      ["backend/app/services"]) == ["web/src/x.tsx"]


def test_an_item_that_declared_nothing_is_not_drift():
    """Its areas were PREDICTED server-side — a known-lower-confidence state the board
    already marks. Reporting every file such an item touched would bury the real cases."""
    assert undeclared(["a.py", "b.py"], []) == []


def test_declaring_more_than_you_touched_is_not_drift():
    """Over-declaration costs parallelism, not correctness. Flagging it here would put a
    warning on every cautious author."""
    assert undeclared(["backend/x.py"], ["backend", "web", "docs"]) == []


# ---- overlaps: the check that needs no declaration ---------------------------------------------

def test_the_same_file_on_two_branches_is_reported_with_both():
    """The failure ITSELF, observed rather than predicted: exact paths, plain intersection,
    no coverage rule and no second definition of what collides. If touchpoints were wrong
    this fires; if they were right and the divvy was wrong this fires too.

    Sabotage: compare only against the newest branch and a wave whose first two children
    collided reports nothing."""
    assert overlaps({"gb/w-1": ["a.py", "b.py"],
                     "gb/w-2": ["b.py", "c.py"],
                     "gb/w-3": ["d.py"]}) == {"b.py": ["gb/w-1", "gb/w-2"]}


def test_one_branch_touching_a_file_twice_is_not_a_collision():
    assert overlaps({"gb/w-1": ["a.py", "a.py"]}) == {}


# ---- at reap ----------------------------------------------------------------------------------

class _Ledger:
    """A client whose `get_item_details` answers with the item as it stands NOW."""

    def __init__(self, items, *, fail=None):
        self.allowed = frozenset({"get_item_details"})
        self.items, self.fail, self.calls = items, fail, []

    def call(self, tool, **kw):
        self.calls.append((tool, kw))
        if self.fail is not None:
            raise self.fail
        return {"id": kw["id"], "touchpoints": list(self.items.get(kw["id"]) or [])}


def test_a_touchpoint_the_child_added_mid_build_counts_as_declared():
    """THE p48c-2 SHAPE (GRPH-1025). Delegated with one area; the child then declared its two
    test files with `update_item`, which the server unions into the item. Every changed file
    is on the item at reap — and the snapshot comparison refused the branch on two of them.

    Sabotage: compare against `wave.declared` instead of `_declared_now` and this goes red."""
    wave = Wave()
    _declared_into(wave, {"GRPH-943": {"touchpoints": ["web/src/features/roadmap/RoadmapView.tsx"]}})
    changed = ["web/src/features/roadmap/RoadmapView.tsx",
               "web/src/__tests__/absence-vs-failure.test.tsx",
               "web/src/__tests__/roadmap-error-state.test.tsx"]
    wave.touched["gb/p48c-2"] = list(changed)
    ledger = _Ledger({"GRPH-943": changed})

    _note_touchpoints(wave, _Child("gb/p48c-2", ["GRPH-943"]), ledger)
    assert wave.undeclared == {}
    assert wave.declared_unread == {}, "it was read, so the report must not say it was not"
    assert ledger.calls == [("get_item_details", {"id": "GRPH-943"})]


def test_a_file_the_item_still_does_not_declare_is_still_undeclared():
    """THE CONTROL: the nine-file App.tsx case GRPH-949 was built for. Re-reading must not turn
    into a pass — the item as it stands now does not cover App.tsx, so the branch is held."""
    wave = Wave()
    _declared_into(wave, {"GRPH-1": {"touchpoints": ["backend/app/services"]}})
    wave.touched["gb/w-1"] = ["backend/app/services/fleet.py", "web/src/App.tsx"]
    ledger = _Ledger({"GRPH-1": ["backend/app/services", "backend/tests/test_fleet.py"]})

    _note_touchpoints(wave, _Child("gb/w-1", ["GRPH-1"]), ledger)
    assert wave.undeclared == {"gb/w-1": ["web/src/App.tsx"]}


def test_an_unreadable_item_falls_back_to_the_snapshot_and_says_so():
    """A failed read is neither a pass nor a refusal with no reason: the snapshot is compared,
    and the branch is named in `declared_unread` with why."""
    from gbfleet.client import ServerUnreachable

    wave = Wave()
    _declared_into(wave, {"GRPH-1": {"touchpoints": ["backend/app/services"]}})
    wave.touched["gb/w-1"] = ["backend/app/services/fleet.py", "web/src/x.tsx"]

    _note_touchpoints(wave, _Child("gb/w-1", ["GRPH-1"]),
                      _Ledger({}, fail=ServerUnreachable("connection refused")))
    assert wave.undeclared == {"gb/w-1": ["web/src/x.tsx"]}
    assert "GRPH-1" in wave.declared_unread["gb/w-1"]
    assert "connection refused" in wave.declared_unread["gb/w-1"]


def test_no_client_is_an_unread_declaration_too():
    wave = Wave()
    _declared_into(wave, {"GRPH-1": {"touchpoints": ["backend"]}})
    wave.touched["gb/w-1"] = ["backend/x.py"]
    _note_touchpoints(wave, _Child("gb/w-1", ["GRPH-1"]))
    assert wave.undeclared == {}
    assert "no client" in wave.declared_unread["gb/w-1"]


def test_an_item_read_as_declaring_nothing_is_an_answer_not_a_fallback():
    """`[]` from a successful read is the item's declaration; only a FAILED read falls back."""
    wave = Wave()
    _declared_into(wave, {"GRPH-1": {"touchpoints": ["web"]}})
    wave.touched["gb/w-1"] = ["web/src/x.tsx"]
    _note_touchpoints(wave, _Child("gb/w-1", ["GRPH-1"]), _Ledger({"GRPH-1": []}))
    assert wave.declared_unread == {}
    assert wave.undeclared == {}, "an item that declares nothing is not drift"


def _bound_child_that_handed_off():
    """A real `Child` driven through `_remember_holdings`: bound to GRPH-1025, held it, moved it
    to review (the roster stops reading it), then claimed GRPH-2 and is reaped holding only that.
    """
    from pathlib import Path

    from gbfleet.spawn import Child
    from gbfleet.supervisor import Partition, _remember_holdings

    child = Child(adapter="fake", worktree=Path("/nowhere"), branch="gb/declared-1", base="",
                  seat_path=Path("/nowhere/seat.json"), process=None, started_at=0.0,
                  log_dir=Path("/nowhere/logs"))
    child.agent_id = "GRPH-A1"
    child.assigned = {"item": "GRPH-1025", "state": "claimed"}
    partition = Partition()
    for reading in (["GRPH-1025"], [], ["GRPH-2"]):
        partition.held = {"GRPH-A1": reading}
        _remember_holdings([child], partition)
    assert child.held_items == ["GRPH-2"], "the shape under test: X is gone from the last reading"
    return child


def test_a_bound_child_that_handed_off_is_judged_by_the_item_it_built_too():
    """THE `declared` SHAPE (GRPH-1079). The bound child handed GRPH-1025 to review, claimed
    GRPH-2, and was reaped holding only GRPH-2. The branch carries GRPH-1025's work, so
    GRPH-1025's declaration is part of the operand and its own file is not undeclared.

    Sabotage: iterate `child.held_items` alone in `_note_touchpoints` and this goes red —
    supervisor.py is then compared against GRPH-2's declaration only and the branch is held."""
    wave = Wave()
    wave.touched["gb/declared-1"] = ["fleet/src/gbfleet/supervisor.py", "backend/app/x.py"]
    ledger = _Ledger({"GRPH-1025": ["fleet/src/gbfleet/supervisor.py"],
                      "GRPH-2": ["backend/app/x.py"]})

    _note_touchpoints(wave, _bound_child_that_handed_off(), ledger)
    assert wave.undeclared == {}
    assert [kw["id"] for _, kw in ledger.calls] == ["GRPH-1025", "GRPH-2"], \
        "bound item first, then what it went on to hold"


def test_a_file_none_of_the_branch_items_declare_is_still_refused():
    """THE CONTROL for GRPH-1079: widening the operand to every item on the branch is not a
    pass. A file neither the handed-off item nor the later one declares still holds the PR."""
    wave = Wave()
    wave.touched["gb/declared-1"] = ["fleet/src/gbfleet/supervisor.py", "web/src/App.tsx"]
    ledger = _Ledger({"GRPH-1025": ["fleet/src/gbfleet/supervisor.py"],
                      "GRPH-2": ["backend/app/x.py"]})

    _note_touchpoints(wave, _bound_child_that_handed_off(), ledger)
    assert wave.undeclared == {"gb/declared-1": ["web/src/App.tsx"]}


def test_an_item_held_and_handed_off_without_a_binding_still_counts():
    """Unbound seats too: the history is the roster's, not the seat's. A child that held
    GRPH-1, handed it off and then held GRPH-2 is judged against both."""
    from gbfleet.supervisor import _branch_items

    child = _bound_child_that_handed_off()
    child.assigned = None
    assert _branch_items(child) == ["GRPH-1025", "GRPH-2"]


def test_the_collision_check_covers_the_whole_wave_not_just_the_last_pair():
    """Recomputed over every branch measured so far. A pairwise check at each reap would miss
    the pair reaped either side of it — which is most of them.

    Sabotage: compare the reaped child only against the previous one and this fails."""
    wave = Wave()
    wave.touched.update({"gb/w-1": ["shared.py"], "gb/w-2": ["other.py"],
                         "gb/w-3": ["shared.py"]})
    _note_touchpoints(wave, _Child("gb/w-3", []))
    assert wave.collided == {"shared.py": ["gb/w-1", "gb/w-3"]}


def test_a_child_that_changed_nothing_reports_nothing():
    wave = Wave()
    _declared_into(wave, {"GRPH-1": {"touchpoints": ["backend"]}})
    _note_touchpoints(wave, _Child("gb/w-1", ["GRPH-1"]))
    assert wave.undeclared == {} and wave.collided == {}


def test_the_supervisor_reads_but_writes_nothing_and_decides_nothing():
    """What "collides" means belongs to the server; this records and reports. Since GRPH-1025
    it READS the item's current declaration, and that read is the only call it makes."""
    import ast
    import inspect
    import textwrap

    from gbfleet.supervisor import _declared_now

    def body(fn_obj):
        # The BODY, with the docstring dropped — which these functions' own prose explains
        # at length, so matching on raw source would fail on the explanation of the rule.
        fn = ast.parse(textwrap.dedent(inspect.getsource(fn_obj))).body[0]
        nodes = fn.body[1:] if isinstance(fn.body[0], ast.Expr) and isinstance(
            fn.body[0].value, ast.Constant) else fn.body
        return "\n".join(ast.unparse(node) for node in nodes)

    for code in (body(_note_touchpoints), body(_declared_now)):
        for verb in ("update_item", "release_item", "record."):
            assert verb not in code, f"reaches for {verb}"
    assert "client.call(" not in body(_note_touchpoints)
    calls = [n for n in ast.walk(ast.parse(body(_declared_now)))
             if isinstance(n, ast.Call) and ast.unparse(n.func) == "client.call"]
    assert [ast.literal_eval(c.args[0]) for c in calls] == ["get_item_details"]


# ---- and it is printed, which is where the last one of these died ------------------------------

def test_both_findings_reach_the_report():
    """`quiet` existed, its docstring said it was there so an operator would not have to work
    it out afterwards, and no output surface ever mentioned it (GRPH-579). Sabotage: drop
    either print and this fails."""
    wave = Wave()
    wave.collided = {"shared.py": ["gb/w-1", "gb/w-2"]}
    wave.undeclared = {"gb/w-1": ["web/src/x.tsx"]}

    out = io.StringIO()
    report(wave, out=out)
    text = out.getvalue()
    assert "COLLIDED shared.py: changed on gb/w-1, gb/w-2" in text
    assert "UNDECLARED gb/w-1" in text and "web/src/x.tsx" in text


def test_the_report_says_which_declaration_was_compared():
    """GRPH-1025. Sabotage: drop the DECLARATION print and an UNDECLARED line computed from a
    stale snapshot reads exactly like one computed from the item."""
    wave = Wave()
    wave.declared_unread = {"gb/w-1": "GRPH-1: connection refused"}
    out = io.StringIO()
    report(wave, out=out)
    assert ("DECLARATION unread gb/w-1: compared against delegation-time snapshot "
            "(GRPH-1: connection refused)") in out.getvalue()


def test_a_long_undeclared_list_is_summarised_rather_than_dumped():
    wave = Wave()
    wave.undeclared = {"gb/w-1": [f"f{i}.py" for i in range(9)]}
    out = io.StringIO()
    report(wave, out=out)
    line = [l for l in out.getvalue().splitlines() if l.startswith("UNDECLARED")][0]
    assert "9 file(s)" in line and "(+4 more)" in line


# ---- GRPH-786: how stale the reviewer's diff is -----------------------------------------------

def _a_repo_with_a_remote(tmp_path):
    import subprocess

    origin = tmp_path / "origin.git"
    subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(origin)], check=True)
    repo = tmp_path / "clone"
    subprocess.run(["git", "clone", "-q", str(origin), str(repo)], check=True)
    cfg = ["-c", "user.email=a@b.c", "-c", "user.name=t"]
    (repo / "f.txt").write_text("x\n")
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
    subprocess.run(["git", *cfg, "commit", "-qm", "one"], cwd=repo, check=True)
    subprocess.run(["git", "push", "-q", "origin", "main"], cwd=repo, check=True)
    subprocess.run(["git", "remote", "set-head", "origin", "main"], cwd=repo, check=True)
    return origin, repo, cfg


def test_a_branch_cut_before_the_trunk_moved_is_reported_as_behind(tmp_path):
    """THE CHECK. Two agents can each be green on their own base and conflict on merge —
    measured three times in one afternoon on this repository. The supervisor is the only
    party that can see it: the server has no git, and the reviewer gets a branch with no
    indication of what its diff is against.

    Sabotage: report 0 instead of measuring and a stale branch reads as current."""
    import subprocess

    from gbfleet.supervisor import Wave, _note_staleness
    from gbfleet.worktree import Worktree

    origin, repo, cfg = _a_repo_with_a_remote(tmp_path)
    base = subprocess.run(["git", "rev-parse", "HEAD"], cwd=repo,
                          capture_output=True, text=True).stdout.strip()
    # The trunk moves after the worktree was cut, from somewhere else.
    other = tmp_path / "other"
    subprocess.run(["git", "clone", "-q", str(origin), str(other)], check=True)
    (other / "g.txt").write_text("y\n")
    subprocess.run(["git", "add", "-A"], cwd=other, check=True)
    subprocess.run(["git", *cfg, "commit", "-qm", "two"], cwd=other, check=True)
    subprocess.run(["git", "push", "-q", "origin", "main"], cwd=other, check=True)

    wave = Wave()
    _note_staleness(wave, Worktree(path=repo, branch="gb/w-1", repo=repo, base=base))
    assert wave.stale == {"gb/w-1": (1, "origin/main")}
    assert not wave.stale_unmeasured


def test_it_fetches_first_or_it_would_measure_nothing(tmp_path):
    """Without a fetch a remote-tracking ref is only as fresh as the last one, and this check
    would report every branch as current — the absence-reads-as-clean failure it exists to
    catch, inside itself. The test above only passes BECAUSE of the fetch: nothing in that
    clone had ever seen the second commit.

    Sabotage: drop the refresh and the test above fails."""
    import inspect

    from gbfleet.supervisor import _note_staleness

    assert "refresh_ref" in inspect.getsource(_note_staleness)


def test_a_current_branch_reports_nothing(tmp_path):
    import subprocess

    from gbfleet.supervisor import Wave, _note_staleness
    from gbfleet.worktree import Worktree

    origin, repo, cfg = _a_repo_with_a_remote(tmp_path)
    base = subprocess.run(["git", "rev-parse", "HEAD"], cwd=repo,
                          capture_output=True, text=True).stdout.strip()
    wave = Wave()
    _note_staleness(wave, Worktree(path=repo, branch="gb/w-1", repo=repo, base=base))
    assert wave.stale == {}


def test_a_repository_with_no_remote_says_unmeasured_not_current(tmp_path):
    """"We could not ask" and "nothing moved" are different facts, and reporting the second
    for the first is the whole failure mode."""
    import subprocess

    from gbfleet.supervisor import Wave, _note_staleness
    from gbfleet.worktree import Worktree

    repo = tmp_path / "lonely"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=repo, check=True)
    (repo / "f.txt").write_text("x\n")
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
    subprocess.run(["git", "-c", "user.email=a@b.c", "-c", "user.name=t",
                    "commit", "-qm", "one"], cwd=repo, check=True)
    base = subprocess.run(["git", "rev-parse", "HEAD"], cwd=repo,
                          capture_output=True, text=True).stdout.strip()

    wave = Wave()
    _note_staleness(wave, Worktree(path=repo, branch="gb/w-1", repo=repo, base=base))
    assert wave.stale == {}
    assert "no remote" in wave.stale_unmeasured


def test_staleness_reaches_the_report():
    wave = Wave()
    wave.stale = {"gb/w-1": (4, "origin/main")}
    out = io.StringIO()
    report(wave, out=out)
    assert "BEHIND gb/w-1: cut from a base 4 commit(s) behind origin/main" in out.getvalue()


def test_unmeasured_staleness_reaches_the_report_too():
    wave = Wave()
    wave.stale_unmeasured = "could not fetch origin/main"
    out = io.StringIO()
    report(wave, out=out)
    assert "BEHIND unmeasured: could not fetch origin/main" in out.getvalue()


def test_a_repository_whose_trunk_is_not_main_is_measured_against_its_own_trunk(tmp_path):
    """Read from the remote HEAD symref the clone recorded, never guessed from likely names.
    A repository on `develop` or `master` is not unusual, and a guess that happened to match
    `main` would make this check silently measure nothing everywhere it did not.

    Sabotage: return f"{remote}/main" and this fails while every other test still passes —
    which is exactly how a guess survives a suite written on a repository called main."""
    import subprocess

    from gbfleet.supervisor import Wave, _note_staleness
    from gbfleet.worktree import Worktree, default_ref, remote_for

    origin = tmp_path / "origin.git"
    subprocess.run(["git", "init", "-q", "--bare", "-b", "develop", str(origin)], check=True)
    repo = tmp_path / "clone"
    subprocess.run(["git", "clone", "-q", str(origin), str(repo)], check=True)
    cfg = ["-c", "user.email=a@b.c", "-c", "user.name=t"]
    (repo / "f.txt").write_text("x\n")
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
    subprocess.run(["git", *cfg, "commit", "-qm", "one"], cwd=repo, check=True)
    subprocess.run(["git", "push", "-q", "origin", "HEAD:develop"], cwd=repo, check=True)
    subprocess.run(["git", "remote", "set-head", "origin", "develop"], cwd=repo, check=True)

    assert default_ref(repo, remote_for(repo)) == "origin/develop"

    base = subprocess.run(["git", "rev-parse", "HEAD"], cwd=repo,
                          capture_output=True, text=True).stdout.strip()
    other = tmp_path / "other"
    subprocess.run(["git", "clone", "-q", str(origin), str(other)], check=True)
    (other / "g.txt").write_text("y\n")
    subprocess.run(["git", "add", "-A"], cwd=other, check=True)
    subprocess.run(["git", *cfg, "commit", "-qm", "two"], cwd=other, check=True)
    subprocess.run(["git", "push", "-q", "origin", "HEAD:develop"], cwd=other, check=True)

    wave = Wave()
    _note_staleness(wave, Worktree(path=repo, branch="gb/w-1", repo=repo, base=base))
    assert wave.stale == {"gb/w-1": (1, "origin/develop")}
