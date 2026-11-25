"""A release stamp runs what a release stamp can break, and nothing else.

`scripts/stamp_only.py` decides from the DIFF whether a change is precisely a stamp — one
version string in each of the four version files, agreeing, CalVer — and CI skips the
suites on `true`. The rule is strict in the safe direction, and these tests pin both edges:
the real stamp shape passes; one more file, one more line, a disagreement, a placeholder,
a stamp that forgot a file, or a re-lock riding along all answer `false` and pay for the
full run.

The last test is the one the detector cannot make: that the checked-in tree's own
`backend/uv.lock` still agrees with `backend/pyproject.toml` (GRPH-1933).
"""
from __future__ import annotations

import pathlib
import sys
import tomllib

import yaml

REPO = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "scripts"))
import stamp_only as so  # noqa: E402

WORKFLOW = REPO / ".github" / "workflows" / "ci.yml"


def _hunk(path: str, old: str, new: str) -> str:
    return (f"diff --git a/{path} b/{path}\nindex 1111111..2222222 100644\n"
            f"--- a/{path}\n+++ b/{path}\n@@ -1,1 +1,1 @@\n-{old}\n+{new}\n")


def _stamp(old="2026.09.15", new="2026.09.16", *, pkg_new=None) -> str:
    return (_hunk("backend/app/version.py", f'__version__ = "{old}"', f'__version__ = "{new}"')
            + _hunk("backend/pyproject.toml", f'version = "{old}"', f'version = "{new}"')
            + _hunk("backend/uv.lock", f'version = "{old}"', f'version = "{new}"')
            + _hunk("web/package.json", f'  "version": "{old}",', f'  "version": "{pkg_new or new}",'))


def test_a_real_stamp_is_a_stamp():
    ok, why = so.stamp_only(_stamp())
    assert ok, why
    assert "2026.09.15 -> 2026.09.16" in why


def test_one_more_file_is_not_a_stamp():
    diff = _stamp() + _hunk("backend/app/main.py", "x = 1", "x = 2")
    ok, why = so.stamp_only(diff)
    assert not ok and "backend/app/main.py" in why


def test_one_more_line_in_a_stamped_file_is_not_a_stamp():
    """A dependency bump in pyproject.toml rides in the same file the stamp writes. It
    must run the suites; the file name alone cannot tell them apart."""
    diff = _stamp().replace('-version = "2026.09.15"\n+version = "2026.09.16"\n',
                            '-version = "2026.09.15"\n+version = "2026.09.16"\n'
                            '-fastapi = "0.1"\n+fastapi = "0.2"\n')
    ok, why = so.stamp_only(diff)
    assert not ok and "pyproject.toml" in why


def test_disagreeing_versions_are_not_a_stamp():
    ok, why = so.stamp_only(_stamp(pkg_new="2026.09.17"))
    assert not ok and "disagree" in why


def test_a_stamp_that_forgot_a_file_is_not_a_stamp():
    diff = _stamp().split("diff --git a/web/package.json")[0]
    ok, why = so.stamp_only(diff)
    assert not ok and "web/package.json" in why


def test_a_stamp_that_forgot_the_lock_is_not_a_stamp():
    """GRPH-1933: pyproject stamped and the lock's copy of the same scalar not is the exact
    shape that left `uv lock --check` red on main for 45 stamps. It must not skip the suites."""
    diff = _stamp().replace(
        _hunk("backend/uv.lock", 'version = "2026.09.15"', 'version = "2026.09.16"'), "")
    assert "backend/uv.lock" not in diff, "the fixture's uv.lock hunk moved"
    ok, why = so.stamp_only(diff)
    assert not ok and "backend/uv.lock" in why


def test_a_stamp_that_also_relocked_a_dependency_is_not_a_stamp():
    """The lock's changed line is a bare `version = "…"`, indistinguishable from any
    dependency's. What stops a re-lock riding through as a stamp is the one-line-each
    guard, not the file name."""
    plain = _hunk("backend/uv.lock", 'version = "2026.09.15"', 'version = "2026.09.16"')
    relocked = ("diff --git a/backend/uv.lock b/backend/uv.lock\nindex 1111111..2222222 100644\n"
                "--- a/backend/uv.lock\n+++ b/backend/uv.lock\n@@ -1,2 +1,2 @@\n"
                '-version = "2026.09.15"\n-version = "1.2.3"\n'
                '+version = "2026.09.16"\n+version = "1.2.4"\n')
    diff = _stamp()
    assert plain in diff, "the fixture's uv.lock hunk moved"
    ok, why = so.stamp_only(diff.replace(plain, relocked))
    assert not ok and "backend/uv.lock" in why and "not one each" in why


def test_a_placeholder_or_non_calver_is_not_a_stamp():
    assert not so.stamp_only(_stamp(new="0.1.0"))[0]
    assert not so.stamp_only(_stamp(new="v2026.09.16"))[0]
    assert not so.stamp_only("")[0]


def test_the_workflow_gates_every_suite_on_it_and_not_the_wheel():
    """The output exists, every suite skips a stamp, and the one job a stamp can break —
    the non-editable wheel install — still runs."""
    wf = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    jobs = wf["jobs"]
    assert "stamp_only" in jobs["changes"]["outputs"]
    step_ids = [st.get("id") for st in jobs["changes"]["steps"]]
    assert "stamp" in step_ids, "the changes job never asks the question"
    guard = "needs.changes.outputs.stamp_only != 'true'"
    for job in ("backend-sqlite", "backend-postgres", "frontend", "fleet", "cli-tests"):
        assert guard in jobs[job]["if"], f"{job} still runs on a bare stamp"
    assert guard not in jobs["backend-packaging"]["if"], (
        "the wheel install is what a stamp can break; it must run")


def _lock_project_version(text: str) -> str:
    """The `graphban-api` package's own scalar in uv.lock.

    Read with a plain block walk, deliberately NOT with the regex `stamp()` writes it with:
    an assertion that shares its parser with the writer cannot catch that writer drifting.
    """
    for block in text.split("[[package]]")[1:]:
        lines = [line.strip() for line in block.splitlines() if line.strip()]
        if lines and lines[0] == 'name = "graphban-api"':
            for line in lines[1:]:
                if line.startswith("version = "):
                    return line.split('"')[1]
    return ""


def test_the_checked_in_lock_agrees_with_pyproject():
    """GRPH-1933. `uv lock --check` was red on main for 45 stamps: the stamp wrote
    pyproject's version and never the lock's copy of it, and nothing in CI compared the
    two. This is that comparison, on the files as checked in, so it runs at every head."""
    want = tomllib.loads(
        (REPO / "backend/pyproject.toml").read_text(encoding="utf-8"))["project"]["version"]
    got = _lock_project_version((REPO / "backend/uv.lock").read_text(encoding="utf-8"))
    assert got, "no graphban-api [[package]] block in backend/uv.lock — nothing to compare"
    assert got == want, (
        f"backend/uv.lock pins graphban-api {got} but backend/pyproject.toml is {want}; "
        f"`uv lock --check` stays red until they agree")
