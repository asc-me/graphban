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
        return {"id": "bnr_1", "project_id": "scratch", "api_key": "scratch-key",
                "seats": [{"task_id": t, "enrolment_code": f"C-{t}"} for t in self.tasks]}

    def post_bench_result(self, body):
        self.posted = body
        return {"id": "bnb_1", **body}


def drive(repo, tmp_path, tasks, server, behaviour, cut_at="base", **kw):
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
        return SimpleNamespace(running=False)

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


def row(repo):
    return {"id": "t1", "kind": "build", "base_sha": base_of(repo), "title": "t", "description": "d",
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
    source = KeyedServer(["t1"], key="scratch-key")
    source.get_suite = lambda project_id: {"version": 1, "tasks": [row(repo)]}

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
                   "--workspace", str(tmp_path / "ws")) == 0
    [(seat, roster)] = seats
    assert seat.api_key == "scratch-key" and seat.code == "C-t1"
    assert roster.api_key == "scratch-key" and roster.project_id == "scratch"
    assert source.posted["outcomes"][0]["outcome"] == "never_registered"


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
    assert cuts == {"build-slugify": fx, "t1": repo}
    assert checks == {"bench-build-slugify": fx, "bench-t1": repo}
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
                   "--workspace", str(tmp_path / "ws")) == 0
    assert cuts == [R.template_fixture(tmp_path / "ws")]
