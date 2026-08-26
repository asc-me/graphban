"""S1 — the execution tools, and the boundary they all go through (PRD-24 D2, AC 1-2).

**The boundary is the whole slice.** PRD-22 D-k says the supervisor claims no sandbox: a vendor
child runs with `--dangerously-skip-permissions` and could write anywhere. `gbagent` is the one
fleet member where that is not true, and the only reason is that we own the write tool — so
these tests are about the escape attempts, not the happy path.

Every case here resolves BEFORE it checks, because each of the three shapes below defeats a
check that looks at the string: `../..` climbs out, an absolute path wins a join, and a symlink
is entirely innocent-looking until you follow it.
"""
from __future__ import annotations

import os
from pathlib import Path

import pytest

from gbagent import tools
from gbagent.workspace import OutsideWorktree, ToolError, safe_path


@pytest.fixture()
def wt(tmp_path: Path) -> Path:
    """A worktree with a symlink pointing out of it, because that is the interesting case."""
    root = tmp_path / "wt"
    (root / "backend" / "app").mkdir(parents=True)
    (root / "backend" / "app" / "items.py").write_text("def claim():\n    return None\n")
    (root / "README.md").write_text("# repo\n")
    os.symlink(tmp_path / "outside", root / "escape")
    (tmp_path / "outside").mkdir()
    (tmp_path / "outside" / "secret.txt").write_text("not yours\n")
    return root


# ---- the boundary ------------------------------------------------------------------


@pytest.mark.parametrize("path,why", [
    ("../../etc/passwd", "climbs out with dot segments"),
    ("/etc/passwd", "absolute, and an absolute path WINS a join"),
    ("escape/secret.txt", "through a symlink whose target is outside"),
    ("backend/../../outside/secret.txt", "climbs out from inside"),
])
def test_a_path_that_resolves_outside_is_refused(wt, path, why):
    with pytest.raises(OutsideWorktree) as exc:
        safe_path(wt, path)

    # The refusal names what it RESOLVED to. Echoing only the request hides the fact the
    # reader needs — `escape/secret.txt` looks entirely innocent until you follow it.
    assert path in str(exc.value)
    assert "outside the worktree" in str(exc.value)


def test_the_refusal_names_the_resolved_target_not_just_the_request(wt):
    with pytest.raises(OutsideWorktree) as exc:
        safe_path(wt, "escape/secret.txt")

    assert "secret.txt" in str(exc.value)
    assert str(wt.resolve()) in str(exc.value), "the boundary itself must be named"


@pytest.mark.parametrize("path", [
    "backend/app/items.py",
    "./backend/../backend/app/items.py",
    "newdir/deeper/brand_new.py",
])
def test_a_path_inside_is_allowed_including_one_that_does_not_exist_yet(wt, path):
    """`write_file` has to work for a file in a directory that is not there yet, which means
    the resolve cannot be strict."""
    assert safe_path(wt, path).is_relative_to(wt.resolve())


def test_an_absolute_path_INSIDE_the_worktree_is_allowed(wt):
    """Refusing every absolute path would be a rule about leading slashes rather than about
    the boundary. A model holding the worktree root may legitimately build one."""
    inside = str((wt / "backend" / "app" / "items.py").resolve())

    assert safe_path(wt, inside).is_relative_to(wt.resolve())


def test_the_worktree_root_is_resolved_too(tmp_path):
    """On macOS a worktree under /tmp really lives at /private/tmp. Comparing an unresolved
    root against a resolved path would refuse every legitimate write on this machine."""
    real = tmp_path / "real"
    (real / "sub").mkdir(parents=True)
    link = tmp_path / "link"
    os.symlink(real, link)

    # Root given via the symlink, path given relative to it.
    assert safe_path(link, "sub").is_relative_to(real.resolve())


def test_gbagent_and_the_supervisor_agree_on_inside(wt):
    """Two definitions of "inside the worktree" that can drift is one too many.

    `gbfleet.supervisor._inside` decides whether a seat file is in the worktree; `safe_path`
    decides whether the agent may write there. They must never disagree about a path, so this
    pins them together rather than trusting that both were written carefully once.
    """
    from gbfleet.supervisor import _inside

    for path in ("backend/app/items.py", "../../etc/passwd", "escape/secret.txt"):
        try:
            resolved = safe_path(wt, path)
            allowed = True
        except OutsideWorktree:
            resolved, allowed = (Path(wt) / path), False
        assert _inside(resolved, wt) is allowed, f"{path}: the two disagree about 'inside'"


# ---- the tools ----------------------------------------------------------------------


def test_write_creates_missing_directories_inside_the_worktree(wt):
    out = tools.write_file(wt, "fleet/src/gbagent/loop.py", "x = 1\n")

    assert out["created"] is True
    assert (wt / "fleet" / "src" / "gbagent" / "loop.py").read_text() == "x = 1\n"


def test_a_refused_write_creates_nothing_at_all(wt, tmp_path):
    """The boundary check runs BEFORE any mkdir, so a refused path leaves no trace. A guard
    that refuses the write after creating its parents has already escaped."""
    with pytest.raises(OutsideWorktree):
        tools.write_file(wt, "../../etc/nope/deep/x.py", "x")

    assert not (tmp_path.parent / "etc").exists()
    assert not (wt.parent / "etc").exists()


def test_write_then_read_round_trips(wt):
    tools.write_file(wt, "notes.md", "hello\nworld\n")

    assert tools.read_file(wt, "notes.md")["text"] == "hello\nworld"


def test_read_can_take_a_line_range(wt):
    tools.write_file(wt, "many.txt", "\n".join(str(i) for i in range(1, 21)) + "\n")

    out = tools.read_file(wt, "many.txt", start=5, count=3)

    assert out["text"] == "5\n6\n7"
    assert out["total_lines"] == 20 and out["truncated"] is True


def test_reading_something_enormous_refuses_rather_than_answering(wt, monkeypatch):
    """A 40 MB answer would blow the window that compaction exists to protect (D7)."""
    monkeypatch.setattr(tools, "MAX_READ_BYTES", 10)
    tools.write_file(wt, "big.txt", "x" * 50)

    with pytest.raises(ToolError) as exc:
        tools.read_file(wt, "big.txt")

    assert "grep" in str(exc.value), "the refusal should say what to do instead"


def test_a_missing_file_says_what_paths_are_relative_TO(wt):
    """From a live run: `qwen3-coder:30b` asked for `calc.py` when the file was
    `backend/calc.py`, having taken the declared test command's `cwd` for the worktree root.
    A refusal the model can act on costs one turn; one it cannot costs the run."""
    with pytest.raises(ToolError) as exc:
        tools.read_file(wt, "items.py")

    assert "WORKTREE ROOT" in str(exc.value)
    assert "list_dir" in str(exc.value), "say what to do next"


def test_list_dir_names_kinds(wt):
    names = {e["name"]: e["kind"] for e in tools.list_dir(wt, ".")["entries"]}

    assert names["backend"] == "dir"
    assert names["README.md"] == "file"


def test_grep_finds_a_line_and_reports_where(wt):
    hits = tools.grep(wt, r"def claim")["hits"]

    assert len(hits) == 1
    assert hits[0]["path"].endswith("items.py") and hits[0]["line"] == 1


def test_grep_skips_the_git_directory(wt):
    (wt / ".git").mkdir()
    (wt / ".git" / "COMMIT_EDITMSG").write_text("def claim(): pass\n")

    assert all(".git" not in h["path"] for h in tools.grep(wt, r"def claim")["hits"])


def test_edit_replaces_an_exact_match(wt):
    tools.edit_file(wt, "backend/app/items.py", "return None", "return 42")

    assert "return 42" in (wt / "backend" / "app" / "items.py").read_text()


def test_edit_refuses_an_ambiguous_match_rather_than_taking_the_first(wt):
    """THE EXPENSIVE MISTAKE. Replacing the first of nine `return None` produces a wrong edit
    that looks like a right one, and at ~30s a turn, finding that through a failing test costs
    far more than the turn this refusal spends."""
    tools.write_file(wt, "many.py", "return None\nreturn None\nreturn None\n")

    with pytest.raises(ToolError) as exc:
        tools.edit_file(wt, "many.py", "return None", "return 1")

    assert "3 times" in str(exc.value)
    assert (wt / "many.py").read_text().count("return None") == 3, "nothing was written"


def test_edit_refuses_a_string_that_is_not_there(wt):
    with pytest.raises(ToolError) as exc:
        tools.edit_file(wt, "README.md", "nonexistent anchor", "x")

    assert "does not appear" in str(exc.value)


@pytest.mark.parametrize("fn,args", [
    (tools.read_file, ("../../etc/passwd",)),
    (tools.list_dir, ("../..",)),
    (tools.write_file, ("../../etc/x", "x")),
    (tools.edit_file, ("../../etc/x", "a", "b")),
    (tools.grep, ("x",)),
])
def test_every_tool_goes_through_the_boundary(wt, fn, args):
    """A tool that takes a path and does not resolve it is a hole in the only property this
    agent has over a vendor child. This is the sweep that says there is no such tool."""
    kwargs = {"path": "../../etc"} if fn is tools.grep else {}
    with pytest.raises(OutsideWorktree):
        fn(wt, *args, **kwargs)


# ---- the walk, not just the argument (GRPH-487) ----------------------------------------
#
# `safe_path` on grep's `path` argument proves where the search STARTS. It says nothing
# about what `rglob` then reaches. A symlink to a FILE outside the worktree is an ordinary
# entry — not a directory, so nothing skips it — and it was read and printed.
#
# The original fixture's `escape` symlink points at a DIRECTORY, which is why 27 tests and
# a 7-mutation sabotage all passed over this: rglob does not descend into symlinked
# directories and `is_dir()` drops them, so the outward target was never reached. The one
# shape that escapes was the one shape not built.


@pytest.fixture()
def linked(tmp_path: Path) -> Path:
    """A worktree with a symlink to a FILE outside it — the shape that escapes."""
    root = tmp_path / "wt"
    root.mkdir()
    (root / "ok.py").write_text("nothing to see\n")
    (tmp_path / "secret.txt").write_text("SUPER_SECRET_TOKEN=hunter2\n")
    os.symlink(tmp_path / "secret.txt", root / "leak.txt")
    return root


def test_read_file_refuses_a_symlink_that_leaves_the_worktree(linked):
    """The control, and the reason the gap was visible at all: the boundary DOES hold for
    the tool that takes the path as an argument. Two tools disagreeing about one path is
    what makes this a defect rather than a design choice."""
    with pytest.raises(OutsideWorktree):
        tools.read_file(linked, "leak.txt")


def test_grep_does_not_return_the_contents_of_a_file_outside_the_worktree(linked):
    """The same worktree, the same path, the other tool."""
    hits = tools.grep(linked, "SUPER_SECRET_TOKEN")["hits"]

    assert hits == [], f"grep read outside the worktree: {hits}"


def test_grep_still_finds_what_is_genuinely_inside(linked):
    """The control for the control. Without it the fix above is satisfied by a grep that
    returns nothing at all, which would pass every assertion and break the tool."""
    (linked / "inside.py").write_text("SUPER_SECRET_TOKEN=mine\n")

    hits = tools.grep(linked, "SUPER_SECRET_TOKEN")["hits"]

    assert [h["path"] for h in hits] == ["inside.py"]


def test_a_symlinked_directory_pointing_out_is_still_not_walked(wt):
    """Directories were never the hole, and this pins that they stay closed. `escape` in the
    shared fixture points at a directory holding secret.txt; rglob does not descend into
    symlinked directories, so it was never reached even before the fix."""
    hits = tools.grep(wt, "not yours")["hits"]

    assert hits == []


def test_the_escape_is_caught_in_a_SUBDIRECTORY_too(tmp_path: Path):
    """A sabotage found this missing. Checking `f.name` instead of the full path passes
    every test above, because at the worktree root the two are the same string.

    One level down they are not: `f.name` for `sub/leak.txt` is `leak.txt`, which resolves
    against the ROOT to a path that is comfortably inside — so the check says yes and the
    symlink is read. The boundary has to be judged on where the file actually is.
    """
    root = tmp_path / "wt"
    (root / "sub" / "deep").mkdir(parents=True)
    (tmp_path / "secret.txt").write_text("SUPER_SECRET_TOKEN=hunter2\n")
    os.symlink(tmp_path / "secret.txt", root / "sub" / "deep" / "leak.txt")

    assert tools.grep(root, "SUPER_SECRET_TOKEN")["hits"] == []


# ---- the truncation guard (S7 walk finding) ------------------------------------------------


def test_replacing_an_established_file_with_a_stub_is_refused(wt):
    """FOUND BY THE S7 ACCEPTANCE WALK, and it is the sharpest thing the walk found.

    Told it could not move an item to review without changing something, `qwen3-coder:30b`
    replaced 856 lines of `services/items.py` with six, opening with the comment "This is a
    placeholder file to simulate the fix". `write_file` writes what it is given, and nothing
    objected.
    """
    tools.write_file(wt, "big.py", "\n".join(f"line {i}" for i in range(200)))

    with pytest.raises(ToolError) as exc:
        tools.write_file(wt, "big.py", "# placeholder\nx = 1\n")

    assert "refusing to replace 200 lines with 2" in str(exc.value)
    assert "edit_file" in str(exc.value), "name the tool it should have used"
    assert "line 199" in (wt / "big.py").read_text(), "nothing was written"


def test_a_genuine_rewrite_that_halves_a_file_still_goes_through(wt):
    """The control. A guard that refused every shrink would make write_file useless for the
    legitimate case, and the model would have no way to replace a file at all."""
    tools.write_file(wt, "mid.py", "\n".join(f"line {i}" for i in range(200)))

    tools.write_file(wt, "mid.py", "\n".join(f"kept {i}" for i in range(100)))

    assert (wt / "mid.py").read_text().startswith("kept 0")


def test_a_small_file_can_be_replaced_freely(wt):
    """Below SHRINK_MIN_LINES there is nothing to lose by rewriting from memory."""
    tools.write_file(wt, "small.py", "a\nb\nc\n")

    tools.write_file(wt, "small.py", "x\n")

    assert (wt / "small.py").read_text() == "x\n"


def test_a_new_file_is_never_refused(wt):
    """write_file exists for new files; there is no content to destroy."""
    out = tools.write_file(wt, "brand_new.py", "x = 1\n")

    assert out["created"] is True


def test_the_guard_has_no_opinion_on_a_file_it_cannot_read(wt):
    """A binary blob has no line count to compare, and refusing on that would block a
    legitimate overwrite for a reason nobody could act on."""
    (wt / "blob.bin").write_bytes(b"\xff\xfe" * 500)

    tools.write_file(wt, "blob.bin", "now it is text\n")

    assert (wt / "blob.bin").read_text() == "now it is text\n"
