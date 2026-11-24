from __future__ import annotations

import shlex
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from gbfleet import cli
from gbfleet.bench import run as R
from gbfleet.bench.suite import Check, Task
from gbfleet.client import ObservationRefused
from gbfleet.spawn import LaunchFailed

PYTEST = f"{shlex.quote(sys.executable)} -m pytest -q -p no:cacheprovider"
OLD_TEST = "def test_old():\n    assert True\n"
NEW_OLD = "import mod\n\n\ndef test_old():\n    assert mod.ok()\n"
NEW_TEST = "import mod\n\n\ndef test_new():\n    assert mod.ok()\n"


def git(repo: Path, *args: str) -> str:
    env = {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t",
           "GIT_COMMITTER_EMAIL": "t@t", "PATH": __import__("os").environ["PATH"], "HOME": str(repo)}
    return subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True,
                          text=True, env=env).stdout.strip()


def base_of(repo):
    return git(repo, "rev-parse", "main")


def ref_of(repo):
    return git(repo, "rev-parse", "ref")


@pytest.fixture
def repo(tmp_path):
    r = tmp_path / "repo"
    r.mkdir()
    git(r, "init", "-q", "-b", "main")
    (r / "tests").mkdir()
    (r / "tests/test_old.py").write_text(OLD_TEST)
    git(r, "add", "-A")
    git(r, "commit", "-q", "-m", "base")
    base = git(r, "rev-parse", "HEAD")
    git(r, "checkout", "-q", "-b", "ref")
    (r / "mod.py").write_text("def ok():\n    return True\n")
    (r / "tests/test_old.py").write_text(NEW_OLD)
    (r / "tests/test_new.py").write_text(NEW_TEST)
    git(r, "add", "-A")
    git(r, "commit", "-q", "-m", "reference")
    ref = git(r, "rev-parse", "HEAD")
    git(r, "checkout", "-q", "main")
    return r


def task(repo, tid="t1", band="S"):
    return Task(id=tid, kind="build", base_sha=base_of(repo), title=tid, description="d", acceptance=(),
                touchpoints=("mod.py",), capabilities=("B7",), size_band=band,
                check=Check(PYTEST, ("tests/test_old.py", "tests/test_new.py")), reference=ref_of(repo))


class FakeServer:
    def __init__(self, tasks, fail_open=False):
        self.tasks, self.fail_open, self.posted, self.opened = tasks, fail_open, None, None

    def open_bench_run(self, body):
        self.opened = body
        if self.fail_open:
            raise ObservationRefused("/api/bench/runs: HTTP 500")
        # The server's seat rows (GRPH-1878): a review task comes back on a review seat.
        kinds = {t["id"]: t.get("kind", "build") for t in body.get("tasks", [])}
        return {"id": "bnr_1", "project_id": "scratch", "api_key": "scratch-key",
                "seats": [{"task_id": t, "enrolment_code": f"C-{t}", "item_id": f"I-{t}",
                           "kind": kinds.get(t, "build")} for t in self.tasks]}

    def post_bench_result(self, body):
        self.posted = body
        return {"id": "bnb_1", **body}


def drive(repo, tmp_path, tasks, server, behaviour, cut_at="base", running=False, **kw):
    plan = R.build_plan(tasks, adapter="fake:m", model="m", suite_id="s", suite_version="1")
    seen = {}

    def cut(t):
        wt = tmp_path / f"wt-{t.id}"
        git(repo, "worktree", "add", "-q", "-b", f"b-{t.id}", str(wt), t.reference if cut_at == "ref" else t.base_sha)
        return SimpleNamespace(path=wt, branch=f"b-{t.id}", repo=repo, base=t.base_sha)

    def start(tree, code, t, scratch):
        seen[t.id] = {"code": code, "tests": sorted(
            p.name for p in (tree.path / "tests").glob("*.py")),
            "old": (tree.path / "tests/test_old.py").read_text()}
        behaviour(tree, t)
        return SimpleNamespace(running=running)

    def reap(tree):
        subprocess.run(["git", "-C", str(tree.path), "add", "-A"], check=True)
        subprocess.run(["git", "-C", str(tree.path), "-c", "user.name=t", "-c", "user.email=t@t",
                        "commit", "-q", "--allow-empty-message", "-m", "salvage"], check=False)
        git(repo, "worktree", "remove", "--force", str(tree.path))

    posted = R.run_plan(plan, repo=repo, client=server, open_scratch=lambda run: None, start=start,
                        cut=cut, limits=SimpleNamespace(child_wall_clock=60.0), reap=reap, **kw)
    return posted, seen


def solve(tree, t):
    (tree.path / "mod.py").write_text("def ok():\n    return True\n")


def nothing(tree, t):
    pass


def test_child_tree_has_no_held_out_test_and_modified_test_is_at_base(repo, tmp_path):
    server = FakeServer(["t1"])
    # Cut from the reference, so the held-out files are IN the tree until hold_out acts.
    _, seen = drive(repo, tmp_path, [task(repo)], server, solve, cut_at="ref")
    assert seen["t1"]["tests"] == ["test_old.py"]       # test_new.py is absent
    assert seen["t1"]["old"] == OLD_TEST                # modified test at its base version


def test_sabotage_leaving_held_out_files_in_is_caught(repo, tmp_path, monkeypatch):
    monkeypatch.setattr(R, "hold_out", lambda tree, t: None)
    _, seen = drive(repo, tmp_path, [task(repo)], FakeServer(["t1"]), solve, cut_at="ref")
    assert seen["t1"]["tests"] != ["test_old.py"] and seen["t1"]["old"] != OLD_TEST


def test_e2e_two_tasks_outcomes_match_the_fake_adapter(repo, tmp_path):
    server = FakeServer(["good", "idle"])
    tasks = [task(repo, "good"), task(repo, "idle")]
    posted, seen = drive(repo, tmp_path, tasks, server,
                         lambda tree, t: solve(tree, t) if t.id == "good" else nothing(tree, t))
    print(posted["results"])
    got = {o["task_id"]: o["outcome"] for o in server.posted["outcomes"]}
    assert got == {"good": "check_green", "idle": "no_branch"}
    assert server.posted["run_id"] == "bnr_1"
    assert seen["good"]["code"] == "C-good"


def test_a_wrong_solution_is_check_red(repo, tmp_path):
    server = FakeServer(["t1"])
    drive(repo, tmp_path, [task(repo)], server,
          lambda tree, t: (tree.path / "mod.py").write_text("def ok():\n    return False\n"))
    assert server.posted["outcomes"][0]["outcome"] == "check_red"


def test_seat_acquisition_failure_records_harness_error(repo, tmp_path):
    server = FakeServer(["t1"], fail_open=True)
    drive(repo, tmp_path, [task(repo)], server, solve)
    assert [o["outcome"] for o in server.posted["outcomes"]] == ["harness_error"]


def test_launch_failure_is_never_registered(repo, tmp_path):
    server = FakeServer(["t1"])
    plan = R.build_plan([task(repo)], adapter="f", model="", suite_id="s", suite_version="1")

    def cut(t):
        wt = tmp_path / "w"
        git(repo, "worktree", "add", "-q", "-b", "bx", str(wt), t.base_sha)
        return SimpleNamespace(path=wt, branch="bx")

    def start(*a):
        raise LaunchFailed("never registered")

    R.run_plan(plan, repo=repo, client=server, open_scratch=lambda r: None, start=start, cut=cut,
               limits=SimpleNamespace(child_wall_clock=1.0))
    assert server.posted["outcomes"][0]["outcome"] == "never_registered"


class SeatServer(FakeServer):
    def __init__(self, tasks):
        super().__init__(tasks)
        self.minted = []

    def bench_task_seat(self, run_id, task_id):
        self.minted.append((run_id, task_id))
        return {"enrolment_code": f"FRESH{len(self.minted)}-{task_id}"}


def _seat_plan(repo, tmp_path):
    def cut(t):
        wt = tmp_path / "w"
        git(repo, "worktree", "add", "-q", "-b", "bx", str(wt), t.base_sha)
        return SimpleNamespace(path=wt, branch="bx")
    return R.build_plan([task(repo)], adapter="f", model="", suite_id="s", suite_version="1"), cut


def test_each_task_starts_on_a_seat_minted_as_it_starts(repo, tmp_path):
    server, codes = SeatServer(["t1"]), []
    plan, cut = _seat_plan(repo, tmp_path)

    def start(tree, code, *a):
        codes.append(code)
        raise LaunchFailed("stub")

    R.run_plan(plan, repo=repo, client=server, open_scratch=lambda r: None, start=start, cut=cut,
               limits=SimpleNamespace(child_wall_clock=1.0))
    assert codes == ["FRESH1-t1"] and server.minted[0] == ("bnr_1", "t1")


def test_an_expired_seat_is_re_requested_once_then_is_harness_error(repo, tmp_path):
    server, codes = SeatServer(["t1"]), []
    plan, cut = _seat_plan(repo, tmp_path)

    def start(tree, code, *a):
        codes.append(code)
        raise LaunchFailed("unauthorized: that seat is already expired")

    R.run_plan(plan, repo=repo, client=server, open_scratch=lambda r: None, start=start, cut=cut,
               limits=SimpleNamespace(child_wall_clock=1.0))
    assert len(codes) == 2 and codes[0] != codes[1]
    assert server.posted["outcomes"][0]["outcome"] == "harness_error"


def test_a_task_over_a_cap_is_skipped_naming_the_cap(repo):
    plan = R.build_plan([task(repo, "big", "L"), task(repo, "small", "S")], adapter="f", model="m",
                        suite_id="s", suite_version="1", caps={"per_attempt_tokens": 100_000})
    assert [p.task.id for p in plan.tasks] == ["small"]
    assert "per_attempt_tokens" in plan.skipped[0].reason and plan.skipped[0].task_id == "big"


# --- CLI ---------------------------------------------------------------------------------

class CliServer:
    def __init__(self, rows):
        self.rows = rows

    def get_suite(self, project_id):
        return {"version": 3, "tasks": self.rows}


def row(repo, tid="t1"):
    return {"id": tid, "kind": "build", "base_sha": base_of(repo), "title": "t", "description": "d",
            "capabilities": ["B7"], "size_band": "S", "reference": ref_of(repo),
            "check": {"command": "pytest", "tests": ["tests/test_new.py"]}}


def patch_cli(monkeypatch, rows, spawned):
    server = CliServer(rows)
    monkeypatch.setenv(cli.API_KEY_ENV, "k")
    monkeypatch.setattr(cli, "Graphban", lambda *a, **k: server)
    monkeypatch.setattr(cli, "read_preferences",
                        lambda c: (None, SimpleNamespace(caps={}), "", {}, {}, None))
    monkeypatch.setattr(cli, "make_adapter_factory", lambda *a, **k: spawned.append(a))


def run_cli(*extra):
    return cli.main(["bench", "run", "--server", "http://x", "--project", "p", *extra])


def test_dry_run_lists_everything_and_spawns_nothing(repo, monkeypatch, capsys):
    spawned: list = []
    patch_cli(monkeypatch, [row(repo)], spawned)
    assert run_cli("--suite", "repo", "--adapter", "claude:haiku", "--dry-run") == 0
    out = capsys.readouterr().out
    assert "t1" in out and "claude:haiku" in out and "model haiku" in out
    assert "estimated spend ~60000 tokens" in out
    assert spawned == []


def test_repo_suite_with_no_tasks_exits_2(monkeypatch, capsys):
    patch_cli(monkeypatch, [], [])
    assert run_cli("--suite", "repo", "--adapter", "claude") == 2
    assert ("repo suite has 0 tasks; run `gbfleet bench harvest` or use `--suite template`"
            in capsys.readouterr().err)


def test_all_suite_reports_an_empty_repo_suite_as_zero_and_does_not_skip_it(monkeypatch, capsys):
    patch_cli(monkeypatch, [], [])
    # The shipped template now carries review tasks, which a plan lists as skipped; this test
    # is about the empty repo suite, so take the template out of it.
    monkeypatch.setattr(R, "template_suite", lambda: None)
    assert run_cli("--suite", "all", "--adapter", "claude", "--dry-run") == 0
    out = capsys.readouterr().out
    assert "repo suite: 0 tasks" in out and "skipped" not in out



# --- the scratch credential (bounce of attempt 1) ------------------------------------------

class KeyedServer(FakeServer):
    """An open-run reply that carries the scratch project's key, as the server's must."""

    def __init__(self, tasks, key="scratch-key"):
        super().__init__(tasks)
        self.key = key

    def open_bench_run(self, body):
        got = super().open_bench_run(body)
        got.pop("api_key")
        if self.key is not None:
            got["api_key"] = self.key
        return got


def test_a_run_reply_without_a_scratch_key_is_harness_error_and_spawns_nothing(repo, tmp_path):
    server = KeyedServer(["t1"], key=None)
    posted, seen = drive(repo, tmp_path, [task(repo)], server, solve)
    assert seen == {}
    assert [o["outcome"] for o in server.posted["outcomes"]] == ["harness_error"]
    assert "scratch" in posted["results"][0].detail


def test_cli_children_and_roster_use_the_scratch_key_not_the_source_key(repo, tmp_path, monkeypatch):
    from gbfleet import supervisor as sup
    from gbfleet import worktree as wt_mod

    clients: list = []
    seats: list = []
    slots: list = []
    source = KeyedServer(["avg-clean", "take-clean"], key="scratch-key")
    # Dashed ids sharing a last segment: keyed on that segment, both would log to `-clean`.
    source.get_suite = lambda project_id: {"version": 1, "tasks": [row(repo, "avg-clean"),
                                                                    row(repo, "take-clean")]}

    def graphban(server, key, project_id=None, **kw):
        c = SimpleNamespace(api_key=key, project_id=project_id)
        clients.append(c)
        return source if project_id == "p" else c

    def tree_for(repo_, workspace, wave, name, base=None):
        wt = tmp_path / f"wt-{name}"
        git(repo, "worktree", "add", "-q", "-b", f"bench-{name}", str(wt), base)
        return SimpleNamespace(path=wt, branch=f"bench-{name}", repo=repo, base=base)

    def start_one(tree, seat, factory, client, *a, **k):
        seats.append((seat, client))
        slots.append(k.get("slot"))
        raise LaunchFailed("stub")

    monkeypatch.setenv(cli.API_KEY_ENV, "source-key")
    monkeypatch.setattr(cli, "Graphban", graphban)
    monkeypatch.setattr(cli, "read_preferences",
                        lambda c: (None, SimpleNamespace(caps={}), "", {}, {}, None))
    monkeypatch.setattr(cli, "make_adapter_factory", lambda *a, **k: object())
    monkeypatch.setattr(sup, "_tree_for", tree_for)
    monkeypatch.setattr(sup, "start_one", start_one)
    monkeypatch.setattr(wt_mod, "reap_held", lambda tree, vendor: None)
    assert run_cli("--suite", "repo", "--adapter", "claude:haiku", "--repo", str(repo),
                   "--workspace", str(tmp_path / "ws")) == 1
    assert [s.code for s, _ in seats] == ["C-avg-clean", "C-take-clean"]
    for seat, roster in seats:
        assert seat.api_key == "scratch-key"
        assert roster.api_key == "scratch-key" and roster.project_id == "scratch"
    assert [o["outcome"] for o in source.posted["outcomes"]] == ["never_registered"] * 2
    # GRPH-1883: the log slot is the whole task id, never its last dash segment, so two
    # tasks ending in the same segment log to distinct directories.
    assert slots == ["avg-clean", "take-clean"]


# --- the template fixture (bounce of attempt 2) --------------------------------------------

def test_template_tasks_cut_and_check_in_the_fixture_repo_not_the_users(repo, tmp_path, monkeypatch):
    """`--suite all`: a template task runs against the built fixture, a repo task against --repo."""
    from gbfleet import supervisor as sup
    from gbfleet import worktree as wt_mod
    from gbfleet.bench import suite as suite_mod
    from gbfleet.bench import validate as validate_mod
    from gbfleet.bench.template import fixture

    full = R.template_suite()
    slug = next(t for t in full.tasks if t.id == "build-slugify")
    monkeypatch.setattr(R, "template_suite", lambda: suite_mod.Suite(full.id, full.version, (slug,)))
    source = KeyedServer(["build-slugify", "t1"])
    source.get_suite = lambda project_id: {"version": 1, "tasks": [row(repo)]}
    cuts: dict = {}
    checks: dict = {}

    def tree_for(repo_, workspace, wave, name, base=None):
        cuts[name] = Path(repo_)
        wt = tmp_path / f"wt-{name}"
        git(repo_, "worktree", "add", "-q", "-b", f"bench-{name}", str(wt), base)
        return SimpleNamespace(path=wt, branch=f"bench-{name}", repo=repo_, base=base)

    def start_one(tree, seat, factory, client, *a, **k):
        if (tree.path / "pkg/m1.py").exists():
            _n, _s, _d, impl, test = fixture.BUILD[1]
            (tree.path / "pkg/m1.py").write_text(impl)
            (tree.path / "tests/test_m1.py").write_text(fixture._test_file("m1", "slugify", test))
        else:
            solve(tree, None)
        git(tree.path, "add", "-A")
        git(tree.path, "commit", "-q", "-m", "child")
        return SimpleNamespace(running=False)

    real_check = validate_mod.run_check

    def run_check(repo_, branch, check, overlay=""):
        checks[branch] = Path(repo_)
        return real_check(repo_, branch, check, overlay=overlay)

    monkeypatch.setenv(cli.API_KEY_ENV, "source-key")
    monkeypatch.setattr(cli, "Graphban", lambda server, key, project_id=None, **kw:
                        source if project_id == "p" else SimpleNamespace(api_key=key))
    monkeypatch.setattr(cli, "read_preferences",
                        lambda c: (None, SimpleNamespace(caps={}), "", {}, {}, None))
    monkeypatch.setattr(cli, "make_adapter_factory", lambda *a, **k: object())
    monkeypatch.setattr(sup, "_tree_for", tree_for)
    monkeypatch.setattr(sup, "start_one", start_one)
    monkeypatch.setattr(wt_mod, "reap_held", lambda tree, vendor: None)
    monkeypatch.setattr(validate_mod, "run_check", run_check)
    ws = tmp_path / "ws"
    assert run_cli("--suite", "all", "--adapter", "claude:haiku", "--repo", str(repo),
                   "--workspace", str(ws)) == 0
    fx = R.template_fixture(ws)
    assert fx.parent == ws and fx != repo
    assert {k.split("-", 1)[1]: v for k, v in cuts.items()} == {"build-slugify": fx, "t1": repo}
    assert {k.replace(k.split("-")[1] + "-", "", 1): v for k, v in checks.items()} == {"bench-build-slugify": fx, "bench-t1": repo}
    outcomes = {o["task_id"]: o["outcome"] for o in source.posted["outcomes"]}
    assert outcomes["build-slugify"] == "check_green"


def test_template_only_run_needs_no_user_repo(tmp_path, monkeypatch):
    """`--suite template` from a directory that is not a git repo still cuts in the fixture."""
    from gbfleet import supervisor as sup

    full = R.template_suite()
    slug = next(t for t in full.tasks if t.id == "build-slugify")
    from gbfleet.bench import suite as suite_mod
    monkeypatch.setattr(R, "template_suite", lambda: suite_mod.Suite(full.id, full.version, (slug,)))
    source = KeyedServer(["build-slugify"])
    cuts: list = []

    def tree_for(repo_, *a, **k):
        cuts.append(Path(repo_))
        raise RuntimeError("stop here")

    monkeypatch.setenv(cli.API_KEY_ENV, "k")
    monkeypatch.setattr(cli, "Graphban", lambda server, key, project_id=None, **kw:
                        source if project_id == "p" else SimpleNamespace(api_key=key))
    monkeypatch.setattr(cli, "read_preferences",
                        lambda c: (None, SimpleNamespace(caps={}), "", {}, {}, None))
    monkeypatch.setattr(cli, "make_adapter_factory", lambda *a, **k: object())
    monkeypatch.setattr(sup, "_tree_for", tree_for)
    plain = tmp_path / "not-a-repo"
    plain.mkdir()
    assert run_cli("--suite", "template", "--adapter", "claude", "--repo", str(plain),
                   "--workspace", str(tmp_path / "ws")) == 1
    assert cuts == [R.template_fixture(tmp_path / "ws")]


def test_bench_run_posts_the_binary_version_the_adapter_resolved(tmp_path, monkeypatch):
    """GRPH-1876: the baseline carries the version the supervisor stamps on natural attempts,
    so it can key onto the natural cell. Posted as "" it never matched one."""
    from gbfleet import supervisor as sup
    from gbfleet.bench import suite as suite_mod

    full = R.template_suite()
    slug = next(t for t in full.tasks if t.id == "build-slugify")
    monkeypatch.setattr(R, "template_suite", lambda: suite_mod.Suite(full.id, full.version, (slug,)))
    source = KeyedServer(["build-slugify"])

    def tree_for(*a, **k):
        raise RuntimeError("stop here")

    monkeypatch.setenv(cli.API_KEY_ENV, "k")
    monkeypatch.setattr(cli, "Graphban", lambda server, key, project_id=None, **kw:
                        source if project_id == "p" else SimpleNamespace(api_key=key))
    monkeypatch.setattr(cli, "read_preferences",
                        lambda c: (None, SimpleNamespace(caps={}), "", {}, {}, None))
    # The real factory, over a resolved adapter: the version must come from `resolve`.
    monkeypatch.setattr(cli, "resolve", lambda name, **k: SimpleNamespace(
        adapter=object(), binary="/bin/claude", version="2.1.37"))
    monkeypatch.setattr(cli, "checked_tuning", lambda adapter, tuning: None)
    monkeypatch.setattr(sup, "_tree_for", tree_for)
    plain = tmp_path / "not-a-repo"
    plain.mkdir()
    assert run_cli("--suite", "template", "--adapter", "claude:sonnet", "--repo", str(plain),
                   "--workspace", str(tmp_path / "ws")) == 1
    assert source.opened["binary_version"] == "2.1.37"
    assert source.posted["binary_version"] == "2.1.37"
    assert source.posted["vendor"] == "claude" and source.posted["model"] == "sonnet"


def leafy(repo, caps=("A1", "E1", "E2", "H5")):
    t = task(repo)
    return Task(**{**t.__dict__, "capabilities": caps})


def leaves_of(server):
    return {k: v["verdict"] for k, v in server.posted["outcomes"][0]["leaves"].items()}


def test_red_check_inside_touchpoints_fails_a1_and_passes_e1_e2_h5(repo, tmp_path):
    server = FakeServer(["t1"])
    drive(repo, tmp_path, [leafy(repo)], server,
          lambda tree, t: (tree.path / "mod.py").write_text("def ok():\n    return False\n"))
    assert server.posted["outcomes"][0]["outcome"] == "check_red"
    assert leaves_of(server) == {"A1": "fail", "E1": "pass", "E2": "pass", "H5": "pass"}


def test_edit_outside_touchpoints_fails_h5_with_a_green_check(repo, tmp_path):
    def behave(tree, t):
        solve(tree, t)
        (tree.path / "other.py").write_text("x = 1\n")

    server = FakeServer(["t1"])
    drive(repo, tmp_path, [leafy(repo)], server, behave)
    assert server.posted["outcomes"][0]["outcome"] == "check_green"
    assert leaves_of(server) == {"A1": "pass", "E1": "pass", "E2": "pass", "H5": "fail"}


def test_instruction_file_fails_e2(repo, tmp_path):
    def behave(tree, t):
        solve(tree, t)
        (tree.path / "CLAUDE.md").write_text("hi\n")

    server = FakeServer(["t1"])
    drive(repo, tmp_path, [leafy(repo)], server, behave)
    assert leaves_of(server)["E2"] == "fail"


def test_no_branch_fails_e1_and_excludes_diff_leaves(repo, tmp_path):
    server = FakeServer(["t1"])
    drive(repo, tmp_path, [leafy(repo)], server, nothing)
    assert leaves_of(server)["E1"] == "fail"
    assert leaves_of(server)["A1"] == "fail"
    assert leaves_of(server)["E2"] == "excluded" and leaves_of(server)["H5"] == "excluded"



def timing_out(repo, tmp_path, server, behaviour, caps=("A1", "E1", "E2", "H5")):
    ticks = iter(range(0, 10_000, 100))
    drive(repo, tmp_path, [leafy(repo, caps)], server, behaviour, running=True,
          clock=lambda: next(ticks), sleep=lambda s: None)
    assert server.posted["outcomes"][0]["outcome"] == "timed_out"
    return server.posted["outcomes"][0]["leaves"]


def test_timed_out_child_with_commits_is_measured_on_its_diff(repo, tmp_path):
    leaves = timing_out(repo, tmp_path, FakeServer(["t1"]), solve)
    assert {k: v["verdict"] for k, v in leaves.items()} == {
        "A1": "excluded", "E1": "pass", "E2": "pass", "H5": "pass"}
    assert leaves["A1"]["reason"]


def test_timed_out_child_outside_touchpoints_fails_h5(repo, tmp_path):
    def behave(tree, t):
        (tree.path / "other.py").write_text("x = 1\n")

    leaves = timing_out(repo, tmp_path, FakeServer(["t1"]), behave)
    assert leaves["H5"]["verdict"] == "fail" and leaves["E1"]["verdict"] == "pass"


def test_timed_out_child_without_commits_fails_e1_and_excludes_diff_leaves(repo, tmp_path):
    leaves = timing_out(repo, tmp_path, FakeServer(["t1"]), nothing)
    assert {k: v["verdict"] for k, v in leaves.items()} == {
        "A1": "excluded", "E1": "fail", "E2": "excluded", "H5": "excluded"}


MINE = "import mod\n\n\ndef test_mine():\n    assert mod.ok()\n"


def c1_of(repo, tmp_path, test_body):
    def behave(tree, t):
        solve(tree, t)
        (tree.path / "tests/test_mine.py").write_text(test_body)

    server = FakeServer(["t1"])
    drive(repo, tmp_path, [leafy(repo, ("A1", "C1"))], server, behave)
    assert server.posted["outcomes"][0]["outcome"] == "check_green"
    return server.posted["outcomes"][0]["leaves"]["C1"]


def test_c1_passes_a_test_green_with_the_change_and_red_without(repo, tmp_path):
    assert c1_of(repo, tmp_path, MINE)["verdict"] == "pass"


def test_c1_fails_a_test_that_is_red_with_the_change_in_place(repo, tmp_path):
    c1 = c1_of(repo, tmp_path, "import nosuchmod\n\n\ndef test_mine():\n    assert True\n")
    assert c1["verdict"] == "fail" and "in place" in c1["reason"]


def test_c1_fails_a_test_that_still_passes_with_the_change_reverted(repo, tmp_path):
    c1 = c1_of(repo, tmp_path, "def test_mine():\n    assert True\n")
    assert c1["verdict"] == "fail" and "reverted" in c1["reason"]


def _runner_says(monkeypatch, returncode, out, exc=None):
    """Make the project's runner answer `out` (or raise `exc`) for the child's test files."""
    from gbfleet.bench import run as run_mod

    real = subprocess.run

    def fake(argv, **kw):
        if "tests/test_mine.py" not in " ".join(map(str, argv)):
            return real(argv, **kw)
        if exc:
            raise exc
        return subprocess.CompletedProcess(argv, returncode, stdout="", stderr=out)

    monkeypatch.setattr(run_mod.subprocess, "run", fake)


def test_c1_is_red_when_the_runner_collects_nothing_from_the_childs_test(repo, tmp_path, monkeypatch):
    _runner_says(monkeypatch, 5, "Ran 0 tests in 0.000s\n\nNO TESTS RAN")
    c1 = c1_of(repo, tmp_path, MINE)
    assert c1["verdict"] == "fail"
    assert c1["reason"] == "child's tests are not collected by the project's runner"


def test_c1_stays_excluded_when_the_runner_cannot_start(repo, tmp_path, monkeypatch):
    _runner_says(monkeypatch, 0, "", exc=FileNotFoundError("python3"))
    c1 = c1_of(repo, tmp_path, MINE)
    assert c1["verdict"] == "excluded" and "FileNotFoundError" in c1["reason"]


def test_c1_excluded_when_the_child_wrote_no_test(repo, tmp_path):
    server = FakeServer(["t1"])
    drive(repo, tmp_path, [leafy(repo, ("C1",))], server, solve)
    assert server.posted["outcomes"][0]["leaves"]["C1"]["verdict"] == "excluded"


# --- review tasks (GRPH-1874) ---------------------------------------------------------------

from gbfleet.bench import review as review_mod  # noqa: E402
from gbfleet.bench.score import score_run  # noqa: E402

REVIEW_PYTEST = f"{shlex.quote(sys.executable)} -m pytest -q -p no:cacheprovider"


@pytest.fixture
def rrepo(tmp_path):
    r = tmp_path / "rrepo"
    r.mkdir()
    git(r, "init", "-q", "-b", "main")
    (r / "mod.py").write_text("def add(a, b):\n    return a + b\n")
    (r / "test_mod.py").write_text("from mod import add\n\n\ndef test_add():\n    assert add(1, 2) == 3\n")
    git(r, "add", "-A")
    git(r, "commit", "-q", "-m", "clean")
    return r


def review_tasks(repo):
    sha = review_mod.apply_mutation(repo, "HEAD", "d1", {"path": "mod.py", "old": "a + b", "new": "a - b"})
    base = git(repo, "rev-parse", "HEAD")

    def mk(tid, expect, ref):
        return Task(id=tid, kind="review", base_sha=base, title=tid, description="d", acceptance=(),
                    touchpoints=("mod.py",), capabilities=("F2",), size_band="S",
                    check=Check(REVIEW_PYTEST, ("test_mod.py",)), expect=expect, ref=ref)
    return [mk("defect", "bounce", sha), mk("clean", "sign_off", base)]


def drive_review(repo, tmp_path, tasks, server, verdict, start=None, **kw):
    plan = R.build_plan(tasks, adapter="fake:m", model="m", suite_id="s", suite_version="1")
    started = []

    def cut(t, base=None):
        wt = tmp_path / f"rwt-{t.id}"
        git(repo, "worktree", "add", "-q", "--detach", str(wt), base or t.base_sha)
        return SimpleNamespace(path=wt, branch="x", repo=repo, base=base or t.base_sha)

    def default_start(tree, code, t, scratch, review_skip=()):
        started.append((t.id, code))
        return SimpleNamespace(running=False)

    def read_verdict(t, scratch, item_id):
        assert item_id == f"I-{t.id}"  # the task's own item, not a title search
        v = verdict(t)
        return v if isinstance(v, tuple) else (v or "no_verdict", "")

    posted = R.run_plan(plan, repo=repo, client=server, open_scratch=lambda run: None,
                        start=start or default_start, cut=cut,
                        limits=SimpleNamespace(child_wall_clock=60.0),
                        reap=lambda tree: git(repo, "worktree", "remove", "--force", str(tree.path)),
                        read_verdict=read_verdict, **kw)
    return plan, posted, started


def test_review_tasks_are_planned_not_skipped(rrepo):
    plan = R.build_plan(review_tasks(rrepo), adapter="f", model="", suite_id="s", suite_version="1")
    assert [p.task.id for p in plan.tasks] == ["defect", "clean"] and plan.skipped == []
    assert "defect [S] ~60000 tokens" in "\n".join(plan.describe())


def test_a_reviewer_that_bounces_everything_passes_defects_and_fails_clean(rrepo, tmp_path):
    tasks = review_tasks(rrepo)
    server = FakeServer(["defect", "clean"])
    plan, posted, started = drive_review(rrepo, tmp_path, tasks, server, lambda t: "bounce")
    assert sorted(started) == [("clean", "C-clean"), ("defect", "C-defect")]
    got = {o["task_id"]: (o["outcome"], o["expect"]) for o in server.posted["outcomes"]}
    assert got == {"defect": ("bounce", "bounce"), "clean": ("bounce", "sign_off")}
    s = score_run(R.to_outcomes(plan, posted["results"]))
    assert (s.f2_n, s.f2) == (1, 1.0) and s.f1 < 1.0


def test_a_reviewer_that_never_registers_is_excluded_from_n(rrepo, tmp_path):
    def start(*a, **k):
        raise LaunchFailed("never registered")
    tasks = review_tasks(rrepo)
    plan, posted, _ = drive_review(rrepo, tmp_path, tasks, FakeServer(["defect", "clean"]),
                                   lambda t: "bounce", start=start)
    assert {r.outcome for r in posted["results"]} == {"never_registered"}
    s = score_run(R.to_outcomes(plan, posted["results"]))
    assert s.f1_n == 0 and s.f2_n == 0


def test_no_verdict_and_failed_preflight(rrepo, tmp_path):
    tasks = review_tasks(rrepo)
    bad = Task(**{**tasks[0].__dict__, "id": "bad", "ref": tasks[1].ref})  # "defect" that stays green
    _, posted, started = drive_review(rrepo, tmp_path, [tasks[0], bad], FakeServer(["defect", "bad"]),
                                      lambda t: None)
    got = {r.task_id: r.outcome for r in posted["results"]}
    assert got == {"defect": "no_verdict", "bad": "harness_error"}
    assert [s[0] for s in started] == ["defect"]


def test_open_run_sends_review_tasks_as_review_work(rrepo, tmp_path):
    """open_run refuses a review task without ref and a PR url (GRPH-1878); it gets both."""
    tasks = review_tasks(rrepo)
    server = FakeServer(["defect", "clean"])
    drive_review(rrepo, tmp_path, tasks, server, lambda t: "bounce")
    sent = {t["id"]: t for t in server.opened["tasks"]}
    for t in tasks:
        row = sent[t.id]
        assert row["kind"] == "review" and row["ref"] == t.ref and row["expect"] == t.expect
        assert "/pull/" in row["pr"] and ".invalid/" in row["pr"]
        assert f"git diff {t.base_sha}..{t.ref}" in row["description"]


def test_each_reviewer_is_told_to_skip_the_other_review_items(rrepo, tmp_path):
    skips = {}

    def start(tree, code, t, scratch, review_skip=()):
        skips[t.id] = review_skip
        return SimpleNamespace(running=False)
    drive_review(rrepo, tmp_path, review_tasks(rrepo), FakeServer(["defect", "clean"]),
                 lambda t: "bounce", start=start)
    assert skips == {"defect": ("I-clean",), "clean": ("I-defect",)}


def test_a_review_task_seated_as_build_work_is_harness_error_and_spawns_nothing(rrepo, tmp_path):
    """The bounce on attempt 1: a build seat makes the 'reviewer' its builder. Never spawn it."""
    class BuildSeats(FakeServer):
        def open_bench_run(self, body):
            got = super().open_bench_run(body)
            for s in got["seats"]:
                s["kind"] = "build"
            return got
    _, posted, started = drive_review(rrepo, tmp_path, review_tasks(rrepo),
                                      BuildSeats(["defect", "clean"]), lambda t: "bounce")
    assert started == []
    assert {r.outcome for r in posted["results"]} == {"harness_error"}
    assert "build work" in posted["results"][0].detail


def test_a_verdict_recorded_before_the_wall_clock_counts(rrepo, tmp_path):
    ticks = iter(range(0, 10_000, 100))

    def start(tree, code, t, scratch, review_skip=()):
        return SimpleNamespace(running=True)
    tasks = review_tasks(rrepo)[:1]
    _, posted, _ = drive_review(rrepo, tmp_path, tasks, FakeServer(["defect"]),
                                lambda t: "bounce", start=start,
                                clock=lambda: next(ticks), sleep=lambda s: None)
    assert [r.outcome for r in posted["results"]] == ["bounce"]
    _, posted, _ = drive_review(rrepo, tmp_path / "again", tasks, FakeServer(["defect"]),
                                lambda t: None, start=start,
                                clock=lambda: next(ticks), sleep=lambda s: None)
    assert [r.outcome for r in posted["results"]] == ["timed_out"]


def test_cli_spawns_a_review_only_reviewer_and_reads_its_item(tmp_path, monkeypatch):
    """The real cli `start` and `read_verdict` against a stub of open_run (bounce item 3).

    The server half is stubbed the way GRPH-1878 shipped it: review seats with an item id,
    and a bounce that sends the item to `next` with `reviewed_by` cleared."""
    from gbfleet import supervisor as sup
    from gbfleet import worktree as wt_mod
    from gbfleet.bench import suite as suite_mod
    from gbfleet.seat import instruction_for

    full = R.template_suite()
    pair = tuple(t for t in full.tasks if t.id in ("review-take_last-defect", "review-take_last-clean"))
    assert len(pair) == 2
    monkeypatch.setattr(R, "template_suite", lambda: suite_mod.Suite(full.id, full.version, pair))
    source = KeyedServer([t.id for t in pair])
    items = {
        "I-review-take_last-defect": {"status": "next", "reviewed_by": None,
                                      "bounce_reason": "take_last returns the first element"},
        "I-review-take_last-clean": {"status": "review", "evidence": [
            {"kind": "note", "detail": "refused at `done` (pr_ci_not_green): no suite_green"}]},
    }
    readers: list = []
    seats: list = []

    def graphban(server, key, project_id=None, **kw):
        if project_id == "p":
            return source
        c = SimpleNamespace(api_key=key, project_id=project_id, allowed=kw.get("allowed"),
                            close=lambda: None,
                            call=lambda tool, **a: (readers.append((tool, c.allowed)), items[a["id"]])[1])
        return c

    def tree_for(repo_, workspace, wave, name, base=None):
        wt = tmp_path / f"wt-{name}"
        git(repo_, "worktree", "add", "-q", "--detach", str(wt), base)
        return SimpleNamespace(path=wt, branch=f"bench-{name}", repo=repo_, base=base)

    def start_one(tree, seat, factory, client, *a, **k):
        seats.append((seat, instruction_for(seat, tree.path, tree.branch)))
        return SimpleNamespace(running=False)

    monkeypatch.setenv(cli.API_KEY_ENV, "source-key")
    monkeypatch.setattr(cli, "Graphban", graphban)
    monkeypatch.setattr(cli, "read_preferences",
                        lambda c: (None, SimpleNamespace(caps={}), "", {}, {}, None))
    monkeypatch.setattr(cli, "make_adapter_factory", lambda *a, **k: object())
    monkeypatch.setattr(sup, "_tree_for", tree_for)
    monkeypatch.setattr(sup, "start_one", start_one)
    monkeypatch.setattr(wt_mod, "reap_held", lambda tree, vendor: None)
    assert run_cli("--suite", "template", "--adapter", "claude:haiku",
                   "--workspace", str(tmp_path / "ws")) == 0
    assert len(seats) == 2
    for seat, text in seats:
        assert seat.review_only and seat.role == "reviewer" and not seat.bound_only
        assert seat.api_key == "scratch-key"
        assert "claim_review" in text and "REVIEW-ONLY" in text
    assert seats[0][0].review_skip == ("I-review-take_last-clean",)
    assert readers and all(tool == "get_item_details" and allowed == cli.BENCH_VERDICT_READS
                           for tool, allowed in readers)
    got = {o["task_id"]: (o["outcome"], o.get("kind"), o.get("expect"))
           for o in source.posted["outcomes"]}
    assert got == {"review-take_last-defect": ("bounce", "review", "bounce"),
                   "review-take_last-clean": ("sign_off", "review", "sign_off")}


def test_relative_workspace_is_resolved_against_the_callers_cwd(tmp_path, monkeypatch):
    """A relative --workspace lands under <cwd>/<relative>, not under the fixture repo.

    The real `_tree_for` cuts the worktree (git runs with the fixture repo as its cwd, which
    is where an unresolved path went wrong) and the real `start_one` writes the instruction
    file; the adapter's launch factory is the first stub, and it records both paths.
    """
    from gbfleet.bench import suite as suite_mod

    full = R.template_suite()
    slug = next(t for t in full.tasks if t.id == "build-slugify")
    monkeypatch.setattr(R, "template_suite", lambda: suite_mod.Suite(full.id, full.version, (slug,)))
    source = KeyedServer(["build-slugify"])
    seen: dict = {}

    def factory(seat, tree, instruction, debug_file):
        seen.update(tree=Path(tree.path), instruction=Path(instruction),
                    tree_exists=Path(tree.path).is_dir(), instruction_exists=Path(instruction).is_file())
        raise RuntimeError("stop here")

    monkeypatch.setenv(cli.API_KEY_ENV, "k")
    monkeypatch.setattr(cli, "Graphban", lambda server, key, project_id=None, **kw:
                        source if project_id == "p" else SimpleNamespace(api_key=key))
    monkeypatch.setattr(cli, "read_preferences",
                        lambda c: (None, SimpleNamespace(caps={}), "", {}, {}, None))
    monkeypatch.setattr(cli, "make_adapter_factory", lambda *a, **k: factory)
    plain = tmp_path / "not-a-repo"
    plain.mkdir()
    cwd = tmp_path / "cwd"
    cwd.mkdir()
    monkeypatch.chdir(cwd)
    assert run_cli("--suite", "template", "--adapter", "claude", "--repo", str(plain),
                   "--workspace", "ws-rel") == 1
    absolute = (cwd / "ws-rel").resolve()
    assert seen, "the launch factory was never reached"
    assert seen["tree"].is_absolute() and seen["tree_exists"]
    assert seen["tree"].parent == absolute
    assert seen["tree"].name.startswith("bench-") and seen["tree"].name.endswith("-build-slugify")
    assert seen["instruction"] == seen["tree"] / ".gbfleet-instruction"
    assert seen["instruction_exists"]
    # Nothing was cut inside the fixture repo.
    assert not (R.template_fixture(absolute) / "ws-rel").exists()


def _branches(repo):
    return subprocess.run(["git", "-C", str(repo), "branch", "--list", "gb/bench-*"],
                          capture_output=True, text=True, check=True).stdout.split()


def _real_cut_run(repo, tmp_path, tag):
    from gbfleet import supervisor as sup

    server = FakeServer(["t1"])
    plan = R.build_plan([task(repo)], adapter="f", model="", suite_id="s", suite_version="1")
    cuts = []

    def cut(t, base=None):
        tree = sup._tree_for(repo, tmp_path / "ws", "bench", f"{tag}-{t.id}", base=t.base_sha)
        cuts.append(tree.branch)
        return tree

    def start(tree, code, t, scratch):
        solve(tree, t)
        return SimpleNamespace(running=False)

    def reap(tree):
        subprocess.run(["git", "-C", str(tree.path), "add", "-A"], check=True)
        subprocess.run(["git", "-C", str(tree.path), "-c", "user.name=t", "-c", "user.email=t@t",
                        "commit", "-q", "-m", "salvage"], check=False)
        git(repo, "worktree", "remove", "--force", str(tree.path))

    posted = R.run_plan(plan, repo=repo, client=server, open_scratch=lambda r: None, start=start,
                        cut=cut, limits=SimpleNamespace(child_wall_clock=60.0), reap=reap)
    return posted, cuts


def test_two_runs_on_one_repo_both_cut_and_leave_no_bench_branch(repo, tmp_path):
    p1, c1 = _real_cut_run(repo, tmp_path, "aaaaaa")
    p2, c2 = _real_cut_run(repo, tmp_path, "bbbbbb")
    assert c1 != c2
    assert [r.outcome for r in p1["results"]] == ["check_green"]
    assert [r.outcome for r in p2["results"]] == ["check_green"]
    assert _branches(repo) == []


def test_sabotage_a_branch_left_behind_refuses_the_same_name(repo, tmp_path, monkeypatch):
    """Without cleanup (or without a run token) the second cut meets the first's branch."""
    monkeypatch.setattr(R, "discard_tree", lambda repo_, tree: None)
    _real_cut_run(repo, tmp_path, "same")
    p2, _ = _real_cut_run(repo, tmp_path, "same")
    assert [r.outcome for r in p2["results"]] == ["harness_error"]
