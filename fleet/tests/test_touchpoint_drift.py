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

def test_the_reap_compares_against_what_was_declared_when_work_was_handed_out():
    """Not against whatever the item says now. The question is whether the DIVVY's input was
    right, and that input is the snapshot the partition was computed from — the item has been
    edited by the worker since."""
    wave = Wave()
    _declared_into(wave, {"GRPH-1": {"touchpoints": ["backend/app/services"]}})
    wave.touched["gb/w-1"] = ["backend/app/services/fleet.py", "web/src/x.tsx"]

    _note_touchpoints(wave, _Child("gb/w-1", ["GRPH-1"]))
    assert wave.undeclared == {"gb/w-1": ["web/src/x.tsx"]}


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


def test_the_supervisor_writes_nothing_and_decides_nothing():
    """It holds `fleet_status` and `propose_allocation` and cannot call `update_item`; what
    "collides" means belongs to the server. This records and reports."""
    import ast
    import inspect
    import textwrap

    # The BODY, with the docstring dropped — which this function's own prose explains at
    # length, so matching on raw source would fail on the explanation of the rule.
    fn = ast.parse(textwrap.dedent(inspect.getsource(_note_touchpoints))).body[0]
    body = fn.body[1:] if isinstance(fn.body[0], ast.Expr) and isinstance(
        fn.body[0].value, ast.Constant) else fn.body
    code = "\n".join(ast.unparse(node) for node in body)
    for verb in ("update_item", "client", "record"):
        assert verb not in code, f"_note_touchpoints reaches for {verb}"


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
