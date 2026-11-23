from __future__ import annotations

import subprocess
import tempfile
import zipfile
from pathlib import Path

import pytest

from gbfleet.bench import suite as suite_mod
from gbfleet.bench.suite import LEAF_FLOOR, TEMPLATE_ID, load, parse
from gbfleet.bench.template import fixture
from gbfleet.cli import main

COVERED = {"A1", "A3", "A5", "C2", "C1", "E1", "E2", "H5", "F1", "F2"}
NOT_COVERED = ["A2", "A4", "B1", "B2", "B3", "B4", "B5", "B6", "B7", "B8", "H1", "H2", "H3", "H4",
               "C3", "E3", "F3"]


@pytest.fixture(scope="module")
def repo(tmp_path_factory) -> Path:
    return fixture.build(tmp_path_factory.mktemp("fx") / "repo")


def test_bench_list_names_template(capsys):
    assert main(["bench", "list"]) == 0
    tpl = load(suite_mod.template_manifest())
    out = capsys.readouterr().out
    assert f"{TEMPLATE_ID} v{tpl.version} ({len(tpl.tasks)} tasks)" in out
    assert tpl.version == "1.0.0" and len(tpl.tasks) >= 16


def test_template_claims_only_covered_leaves_with_floor():
    tpl = load(suite_mod.template_manifest())
    claimed = {c for t in tpl.tasks for c in t.capabilities}
    assert claimed == COVERED
    assert suite_mod.leaf_floor_problems(tpl) == []
    assert sum("F1" in t.capabilities and t.expect == "bounce" for t in tpl.tasks) >= 5
    assert sum("F2" in t.capabilities and t.expect == "sign_off" for t in tpl.tasks) >= 5


def test_bench_validate_template_every_task_discriminates(repo, capsys):
    rc = main(["bench", "validate", str(suite_mod.template_manifest()), "--repo", str(repo)])
    out = capsys.readouterr().out
    assert rc == 0, out
    lines = [ln for ln in out.splitlines() if ln.strip()]
    assert len(lines) == 16 and all(ln.endswith(": ok") for ln in lines)


def test_validate_refuses_leaf_with_fewer_than_five_tasks(repo, tmp_path, capsys):
    text = suite_mod.template_manifest().read_text(encoding="utf-8")
    # Drop one build task: every build leaf now has 5... drop two to get 4.
    blocks = text.split("[[task]]")
    kept = [blocks[0]] + [b for b in blocks[1:] if 'id = "build-roman"' not in b
                          and 'id = "build-word_count"' not in b]
    short = tmp_path / "short.toml"
    short.write_text("[[task]]".join(kept), encoding="utf-8")
    assert suite_mod.leaf_floor_problems(parse(short.read_text(encoding="utf-8")))
    rc = main(["bench", "validate", str(short), "--repo", str(repo)])
    err = capsys.readouterr().err
    assert rc == 1 and "A1" in err and str(LEAF_FLOOR) in err


def test_content_hash_recorded_next_to_version():
    version, recorded = suite_mod.recorded_hash()
    tpl = load(suite_mod.template_manifest())
    assert version == tpl.version
    assert suite_mod.content_hash() == recorded, (
        "template content changed: bump [suite] version in suite.toml and refresh hash.toml")


def test_hash_changes_with_content(tmp_path):
    for name in suite_mod._HASHED:
        (tmp_path / name).write_bytes((suite_mod.template_dir() / name).read_bytes())
    before = suite_mod.content_hash(tmp_path)
    assert before == suite_mod.content_hash()
    (tmp_path / "README.md").write_text("changed", encoding="utf-8")
    assert suite_mod.content_hash(tmp_path) != before


def test_fixture_is_deterministic(tmp_path):
    def shas(p: Path) -> str:
        return subprocess.run(["git", "-C", str(p), "show-ref", "--tags"], capture_output=True,
                              text=True, check=True).stdout
    assert shas(fixture.build(tmp_path / "a")) == shas(fixture.build(tmp_path / "b"))


def test_readme_lists_covered_and_not_covered():
    text = (suite_mod.template_dir() / "README.md").read_text(encoding="utf-8")
    covered, _, rest = text.partition("## Leaves not covered")
    assert rest
    for leaf in COVERED:
        assert leaf in covered.split("## Leaves covered")[1]
    for leaf in ("A2", "A4", "C3", "E3", "F3", "B1", "H1"):
        assert leaf in rest


def test_built_wheel_contains_template():
    build = pytest.importorskip("build")
    fleet = Path(__file__).resolve().parent.parent
    with tempfile.TemporaryDirectory() as out:
        try:
            build.ProjectBuilder(str(fleet)).build("wheel", out)
        except Exception as exc:  # no network for build isolation deps: not a verdict
            pytest.skip(f"cannot build a wheel here: {exc}")
        wheel = next(Path(out).glob("*.whl"))
        names = zipfile.ZipFile(wheel).namelist()
    for rel in ("suite.toml", "fixture.py", "README.md", "hash.toml"):
        assert f"gbfleet/bench/template/{rel}" in names
