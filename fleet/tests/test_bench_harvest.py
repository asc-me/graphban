from __future__ import annotations

import json
import re
import shlex
import subprocess
import sys
from pathlib import Path

import httpx
import pytest

from gbfleet import cli
from gbfleet.bench import harvest as H
from gbfleet.client import Graphban, ObservationRefused

PYTEST = f"{shlex.quote(sys.executable)} -m pytest -q -p no:cacheprovider"
CMDS = {"py": PYTEST}
FLAKY = (
    "import os\n\n\ndef test_flaky():\n"
    "    p = os.environ['GB_COUNTER']\n"
    "    n = (int(open(p).read()) if os.path.exists(p) else 0) + 1\n"
    "    open(p, 'w').write(str(n))\n"
    "    import flakymod\n    assert flakymod.ok()\n    assert n != 5\n"
)


def git(repo: Path, *args: str) -> str:
    env = {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t",
           "GIT_COMMITTER_EMAIL": "t@t", "PATH": __import__("os").environ["PATH"], "HOME": str(repo)}
    done = subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True,
                          text=True, env=env)
    return done.stdout.strip()


def merge(repo: Path, name: str, files: dict[str, str]) -> None:
    git(repo, "checkout", "-q", "-b", name, "main")
    for path, text in files.items():
        (repo / path).parent.mkdir(parents=True, exist_ok=True)
        (repo / path).write_text(text)
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", f"work {name}")
    git(repo, "checkout", "-q", "main")
    git(repo, "merge", "-q", "--no-ff", "-m", f"Merge {name} GRPH-{len(name)}", name)


@pytest.fixture
def repo(tmp_path, monkeypatch):
    monkeypatch.setenv("GB_COUNTER", str(tmp_path / "counter"))
    r = tmp_path / "repo"
    r.mkdir()
    git(r, "init", "-q", "-b", "main")
    (r / "README").write_text("x")
    (r / "passing.py").write_text("")
    git(r, "add", "-A")
    git(r, "commit", "-q", "-m", "base")
    merge(r, "good", {"goodmod.py": "def ok():\n    return True\n",
                      "tests/test_good.py": "import goodmod\n\n\ndef test_it():\n    assert goodmod.ok()\n"})
    merge(r, "greenparent", {"tests/test_green.py": "def test_it():\n    assert True\n"})
    merge(r, "notests", {"src_only.py": "x = 1\n"})
    merge(r, "flaky", {"flakymod.py": "def ok():\n    return True\n", "tests/test_flaky.py": FLAKY})
    return r


def run(repo, **kw):
    return H.harvest(repo, since="", commands=CMDS, **kw)


def by_reason(found):
    return {d.title.split()[1]: d.reason for d in found.dropped}


def test_red_to_green_merge_is_proposed_and_the_rest_dropped(repo):
    found = run(repo)
    assert [c.title.split()[1] for c in found.candidates] == ["good"]
    c = found.candidates[0]
    merge_sha = git(repo, "log", "--merges", "--first-parent", "--grep=Merge good", "--format=%H")
    assert c.merge_sha == merge_sha
    assert c.base_sha == git(repo, "rev-parse", f"{merge_sha}^1")
    assert c.tests == ["tests/test_good.py"] and c.touchpoints == ["goodmod.py"]
    assert by_reason(found) == {"greenparent": "green_at_parent", "notests": "no_tests",
                                "flaky": "flaky"}


def test_a_check_run_once_lets_the_flaky_merge_through(repo):
    """Sabotage: running the check once instead of 3 times proposes the flaky merge."""
    found = run(repo, runs=1)
    assert "flaky" in [c.title.split()[1] for c in found.candidates]


def test_harvest_writes_nothing_to_the_server(repo, monkeypatch):
    calls = []

    class Fake:
        def __init__(self, *a, **k):
            calls.append("init")

        def __getattr__(self, name):
            calls.append(name)
            raise AssertionError(name)

    monkeypatch.setattr(cli, "Graphban", Fake)
    monkeypatch.setattr(H, "PY_COMMAND", PYTEST)
    assert cli.main(["bench", "harvest", "--repo", str(repo), "--since", ""]) == 0
    assert calls == []
    assert "good" in H.load_candidates(repo)["h-" + git(
        repo, "log", "--merges", "--first-parent", "--grep=Merge good", "--format=%H")[:10]].title


def test_test_pattern_mirrors_the_backend():
    text = (Path(__file__).resolve().parents[2] / "backend/app/services/harness.py").read_text()
    m = re.search(r'_TEST_RE = re\.compile\(\s*((?:r?"[^"]*"\s*)+),', text)
    body = "".join(re.findall(r'r?"([^"]*)"', m.group(1)))
    assert body == H.TEST_RE_SOURCE


class AdoptFake:
    def __init__(self, *a, **k):
        self.sent = None

    def get_suite(self, project_id):
        return {"version": 4, "tasks": []}

    def adopt_suite(self, *, expected_version, tasks):
        self.sent = (expected_version, tasks)
        raise ObservationRefused("HTTP 409", status=409,
                                 body=json.dumps({"current_version": 7}))


def test_adopt_sends_expected_version_and_a_409_prints_the_current_one(repo, monkeypatch, capsys):
    monkeypatch.setattr(H, "PY_COMMAND", PYTEST)
    cli.main(["bench", "harvest", "--repo", str(repo), "--since", ""])
    cid = next(iter(H.load_candidates(repo)))
    fake = AdoptFake()
    monkeypatch.setattr(cli, "Graphban", lambda *a, **k: fake)
    monkeypatch.setenv("GBFLEET_API_KEY", "k")
    code = cli.main(["bench", "adopt", cid, "--repo", str(repo), "--server", "http://x",
                     "--project", "p"])
    assert code == 1
    assert fake.sent[0] == 4 and fake.sent[1][0]["id"] == cid
    assert "current version is 7" in capsys.readouterr().err


def test_client_adopt_posts_expected_version_and_surfaces_409_body():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["path"] = request.url.path
        seen["body"] = json.loads(request.content)
        return httpx.Response(409, json={"current_version": 9})

    g = Graphban("http://x", "k", project_id="p")
    g._http = httpx.Client(transport=httpx.MockTransport(handler))
    with pytest.raises(ObservationRefused) as e:
        g.adopt_suite(expected_version=3, tasks=[{"id": "a"}])
    assert seen["path"] == "/api/bench/suite/adopt" and seen["body"]["expected_version"] == 3
    assert e.value.status == 409 and json.loads(e.value.body)["current_version"] == 9


GOOD_TEST = "import goodmod\n\n\ndef test_it():\n    assert goodmod.ok()\n"


def _commit_on_main(repo: Path, files: dict[str, str], msg: str) -> str:
    for path, text in files.items():
        (repo / path).parent.mkdir(parents=True, exist_ok=True)
        (repo / path).write_text(text)
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", msg)
    return git(repo, "rev-parse", "HEAD")


def test_squash_commit_with_pr_suffix_is_proposed_with_its_parent_as_base(repo):
    parent = git(repo, "rev-parse", "HEAD")
    sha = _commit_on_main(repo, {"sqmod.py": "def ok():\n    return True\n",
                                 "tests/test_sq.py": GOOD_TEST.replace("goodmod", "sqmod")}, "Add sq (#12)")
    found = run(repo)
    c = next(c for c in found.candidates if c.merge_sha == sha)
    assert c.base_sha == parent and c.tests == ["tests/test_sq.py"]


def test_single_parent_commit_without_pr_suffix_is_not_scanned(repo):
    sha = _commit_on_main(repo, {"plmod.py": "def ok():\n    return True\n",
                                 "tests/test_pl.py": GOOD_TEST.replace("goodmod", "plmod")}, "plain commit")
    found = run(repo)
    assert sha not in [c.merge_sha for c in found.candidates]
    assert sha not in [d.merge_sha for d in found.dropped]


SVC_FILES = {"svc/pyproject.toml": "[project]\nname='svc'\n", "svc/.gitignore": ".venv\n"}
SVC_MERGE = {"svc/svcmod.py": "def ok():\n    return True\n",
             "svc/tests/test_svc.py": GOOD_TEST.replace("goodmod", "svcmod")}
# `python -m pytest` puts the cwd on sys.path, so svc/tests/test_svc.py imports `svcmod`
# only when the check runs from svc/ — from the repo root it is red at the merge too.
SVC_COMMAND = "python -m pytest -q -p no:cacheprovider -p no:randomly"


def _harvest_svc(repo: Path, monkeypatch) -> list:
    _commit_on_main(repo, SVC_FILES, "component")
    _commit_on_main(repo, SVC_MERGE, "Add svc (#13)")
    monkeypatch.setattr(H, "PY_COMMAND", SVC_COMMAND)
    found = H.harvest(repo, since="")
    return [c for c in found.candidates if c.title.startswith("Add svc")]


def test_component_in_subdirectory_is_harvested_red_to_green(repo, monkeypatch):
    cand = _harvest_svc(repo, monkeypatch)
    assert cand and cand[0].tests == ["svc/tests/test_svc.py"]


@pytest.mark.skipif(sys.platform == "win32", reason="the fake venv interpreter is a sh script")
def test_component_in_subdirectory_runs_from_its_own_venv(repo, monkeypatch, tmp_path):
    venv = repo / "svc" / ".venv" / "bin"
    venv.mkdir(parents=True)
    py = venv / "python"
    py.write_text(f"#!/bin/sh\npwd > \"$GB_CWD\"\nexec {shlex.quote(sys.executable)} \"$@\"\n")
    py.chmod(0o755)
    monkeypatch.setenv("GB_CWD", str(tmp_path / "cwd"))
    cand = _harvest_svc(repo, monkeypatch)
    assert cand and cand[0].tests == ["svc/tests/test_svc.py"]
    assert (tmp_path / "cwd").read_text().strip().endswith("/svc")


def test_bench_toml_override_wins_over_discovery(repo):
    (repo / ".gbfleet").mkdir()
    (repo / ".gbfleet" / "bench.toml").write_text(
        f"[[check]]\nprefix = 'svc/'\ncwd = 'svc'\ncommand = '{sys.executable} -m pytest -q'\n")
    from gbfleet.bench import validate as V
    tree = repo
    argv, cwd = V.resolve_check(repo, tree, V.Check("python -m pytest -q", ("svc/tests/test_x.py",)))
    assert argv == [sys.executable, "-m", "pytest", "-q", "tests/test_x.py"] and cwd == "svc"


def test_discovery_uses_nearest_manifest_and_relative_paths(repo):
    (repo / "svc" / "tests").mkdir(parents=True)
    (repo / "svc" / "pyproject.toml").write_text("")
    from gbfleet.bench import validate as V
    argv, cwd = V.resolve_check(repo, repo, V.Check("python -m pytest -q", ("svc/tests/test_x.py",)))
    assert cwd == "svc" and argv[-1] == "tests/test_x.py" and argv[0] == sys.executable
    argv, cwd = V.resolve_check(repo, repo, V.Check("python -m pytest -q", ("tests/test_good.py",)))
    assert cwd == "" and argv[-1] == "tests/test_good.py"


def test_venv_interpreter_path_is_one_argument(tmp_path):
    # A path with a space (and, on Windows, backslashes) must reach argv unsplit.
    root = tmp_path / "my repo"
    (root / "svc" / ".venv" / "bin").mkdir(parents=True)
    (root / "svc" / "pyproject.toml").write_text("")
    interp = root / "svc" / ".venv" / "bin" / "python"
    interp.write_text("")
    from gbfleet.bench import validate as V
    argv, cwd = V.resolve_check(root, root, V.Check("python -m pytest -q", ("svc/tests/test_x.py",)))
    assert argv == [str(interp), "-m", "pytest", "-q", "tests/test_x.py"] and cwd == "svc"


def test_relative_repo_still_finds_the_component_venv(tmp_path, monkeypatch):
    # `--repo .`: the check runs from the throwaway tree, so the interpreter must be absolute.
    (tmp_path / "svc" / ".venv" / "bin").mkdir(parents=True)
    (tmp_path / "svc" / "pyproject.toml").write_text("")
    (tmp_path / "svc" / ".venv" / "bin" / "python").write_text("")
    monkeypatch.chdir(tmp_path)
    from gbfleet.bench import validate as V
    argv, _ = V.resolve_check(Path("."), tmp_path, V.Check("python -m pytest", ("svc/tests/test_x.py",)))
    assert Path(argv[0]).is_absolute() and Path(argv[0]) == (tmp_path / "svc/.venv/bin/python").resolve()


def test_src_layout_component_tests_the_tree_not_an_editable_install(tmp_path):
    from gbfleet.bench import validate as V
    (tmp_path / "src").mkdir()
    assert V.check_env(tmp_path)["PYTHONPATH"].split(__import__("os").pathsep)[0] == str(tmp_path / "src")
    assert V.check_env(tmp_path / "nosrc").get("PYTHONPATH") == __import__("os").environ.get("PYTHONPATH")


def test_a_missing_test_runner_drops_the_merge_as_check_unrunnable_not_red(repo):
    found = H.harvest(repo, since="", commands={"py": f"{sys.executable} -m no_such_runner_mod",
                                                 "js": f"{sys.executable} -m no_such_runner_mod"})
    reasons = by_reason(found)
    assert reasons["good"] == "check_unrunnable"
    assert "red_at_merge" not in reasons.values() and not found.candidates


def test_a_conftest_importing_the_new_module_is_still_a_red_to_green_candidate(repo):
    """A conftest import error at the parent is a red tree (pytest exit 4), not a missing runner."""
    merge(repo, "conftestnew", {"cnewmod.py": "def ok():\n    return True\n",
                                "cpkg/tests/conftest.py": "import cnewmod\n",
                                "cpkg/tests/test_c.py": "def test_it():\n    assert True\n"})
    found = run(repo)
    assert "conftestnew" in [c.title.split()[1] for c in found.candidates], found.dropped


def test_a_js_merge_with_no_node_modules_drops_as_check_unrunnable(repo):
    merge(repo, "jsonly", {"web/package.json": "{}", "web/src/a.test.ts": "test('x', () => {})\n"})
    assert by_reason(run(repo))["jsonly"] == "check_unrunnable"


TWO_COMPONENTS = {
    "pa/pyproject.toml": "[project]\nname='pa'\n", "pb/pyproject.toml": "[project]\nname='pb'\n",
}


def test_a_merge_with_tests_in_two_components_is_one_candidate(repo):
    _commit_on_main(repo, TWO_COMPONENTS, "components")
    merge(repo, "twocomp", {
        "pa/pamod.py": "def ok():\n    return True\n",
        "pa/tests/test_pa.py": GOOD_TEST.replace("goodmod", "pamod"),
        "pb/pbmod.py": "def ok():\n    return True\n",
        "pb/tests/test_pb.py": GOOD_TEST.replace("goodmod", "pbmod")})
    cands = {c.title.split()[1]: c for c in run(repo).candidates}
    assert cands["twocomp"].tests == ["pa/tests/test_pa.py", "pb/tests/test_pb.py"]


def test_a_two_component_merge_green_at_parent_in_one_is_dropped(repo):
    """Every component's tests must be red at the parent, not just one of them."""
    _commit_on_main(repo, dict(TWO_COMPONENTS, **{"pb/tests/test_pb.py": "def test_it():\n    pass\n"}),
                    "components")
    merge(repo, "halfgreen", {
        "pa/pamod.py": "def ok():\n    return True\n",
        "pa/tests/test_pa.py": GOOD_TEST.replace("goodmod", "pamod"),
        "pb/tests/test_pb.py": "def test_it():\n    assert 1\n"})
    found = run(repo)
    assert by_reason(found)["halfgreen"] == "green_at_parent"
    assert [d.detail for d in found.dropped if d.title.split()[1] == "halfgreen"] == ["pb"]
