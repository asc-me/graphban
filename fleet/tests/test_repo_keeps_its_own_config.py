"""GRPH-581: the supervisor must not destroy a file the repository committed.

The seat paths are `.grok/config.toml` and `.cursor/mcp.json`, and `seat.write` opens
with `O_TRUNC`. Both are files a repository can reasonably commit — `grok mcp add
--scope project` writes exactly the first one — and a worktree is cut from the repo, so
a committed one lands in every child's tree and is then overwritten.

**What made this worth refusing rather than warning about.** `.grok/config.toml` is not
only MCP configuration. grok merges `[permission]` rules from every project
`.grok/config.toml` from the repo root down, so it is where a repository states what its
agents may not do. Overwriting it removes the repo's own deny rules. And because
`SEAT_FILES` is excluded from salvage on purpose, the change is never committed, never
shown, and invisible for the rest of the child's life.

The one signal that did fire pointed at the wrong thing: at reap,
`_tracked_in_head(worktree, SEAT_FILES)` reports the path as `credential_in_history` —
"the worker committed a credential" — when in fact the repository tracked it before the
fleet existed.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from gbfleet import seat as seat_mod  # noqa: E402
from gbfleet.seat import Seat  # noqa: E402
from gbfleet.worktree import SEAT_FILES, SeatPathIsTracked, create  # noqa: E402

SEAT = Seat(code="c", server_url="https://cloud.graphban.dev", api_key="gb_sk_test")


def _git(root: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=root, capture_output=True, text=True, check=True
    ).stdout


def _commit_a_seat_path(repo: Path, relative: str, body: str) -> None:
    target = repo / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(body, encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", f"the repository commits {relative} on purpose")


# --- the mechanism, stated as a fact about seat.write --------------------------------

def test_writing_a_seat_truncates_whatever_was_there(tmp_path: Path):
    """The reason the guard has to exist, asserted rather than assumed.

    If `seat.write` merged, or refused, or backed up, the guard would be unnecessary.
    It truncates — so anything already at that path is gone.
    """
    target = tmp_path / "config.toml"
    target.write_text(
        '[permission]\ndeny = ["Bash(rm -rf *)"]\n\n[mcp_servers.team]\nurl = "x"\n',
        encoding="utf-8",
    )
    seat_mod.write(target, SEAT.mcp_config(), seat_mod.TOML)
    after = target.read_text(encoding="utf-8")

    assert "deny" not in after, "a committed deny rule survived — is the guard still needed?"
    assert "team" not in after


# --- the guard -----------------------------------------------------------------------

@pytest.mark.parametrize("relative", [f for f in SEAT_FILES if "/" in f])
def test_a_repo_that_commits_a_seat_path_is_refused(
    relative: str, git_repo: Path, tmp_path: Path
):
    """Generic over SEAT_FILES, not special-cased to grok: `.cursor/mcp.json` is the
    same trap for a Cursor child, and a guard that only knew about one vendor would let
    the other through."""
    _commit_a_seat_path(git_repo, relative, "team config\n")
    head = _git(git_repo, "rev-parse", "HEAD").strip()

    with pytest.raises(SeatPathIsTracked) as caught:
        create(git_repo, tmp_path / "w", "wave", "1")

    message = str(caught.value)
    assert relative in message, "the refusal does not name the file it is about"
    assert "Remedy" in message, "refused without telling the operator what to do"
    # Which COMMIT tracks it, not merely that something does. The natural response to
    # this refusal is "but my working tree has no such file" — and it may well not, on a
    # branch where it was deleted. Naming the commit is what answers that.
    assert head[:12] in message, (
        "the refusal never says which commit tracks the file, so an operator whose "
        "working tree looks clean has nowhere to go"
    )


def test_the_refusal_leaves_no_worktree_behind(git_repo: Path, tmp_path: Path):
    """Checked against the base commit BEFORE `git worktree add`. Checking afterwards
    would leave a worktree on the failure path for somebody else to find and reap."""
    _commit_a_seat_path(git_repo, ".grok/config.toml", "team config\n")
    target = tmp_path / "w"

    with pytest.raises(SeatPathIsTracked):
        create(git_repo, target, "wave", "1")

    assert not target.exists(), f"{target} was left behind by a refused create"
    assert "gb/wave-1" not in _git(git_repo, "branch", "--list", "gb/wave-1")


def test_a_repo_that_does_not_commit_one_is_unaffected(git_repo: Path, tmp_path: Path):
    """The control. Without it this guard would be indistinguishable from one that
    refuses every repository, which would stop the fleet entirely."""
    tree = create(git_repo, tmp_path / "w", "wave", "1")
    assert tree.path.exists()


def test_an_untracked_seat_file_on_disk_is_not_the_same_thing(git_repo: Path, tmp_path: Path):
    """A leftover `.grok/config.toml` that git does not track is exactly what the
    supervisor itself writes, and reaps. Refusing on it would make the fleet unable to
    run twice in the same checkout — so the check is against the COMMIT, not the disk."""
    stray = git_repo / ".grok" / "config.toml"
    stray.parent.mkdir(parents=True, exist_ok=True)
    stray.write_text("left over from a previous run\n", encoding="utf-8")

    tree = create(git_repo, tmp_path / "w", "wave", "1")
    assert tree.path.exists()


def test_a_repository_with_no_commits_does_not_crash_the_check(tmp_path: Path):
    """`ls-tree` against a ref that does not exist is an error, not an empty answer, and
    the check runs before anything has been committed in a brand-new repo."""
    from gbfleet.worktree import _tracked_at

    empty = tmp_path / "empty"
    empty.mkdir()
    _git(empty, "init", "-q", "-b", "main")
    assert _tracked_at(empty, "HEAD", SEAT_FILES) == []


# --- and the operator hears about it -------------------------------------------------

def test_the_wave_reports_it_rather_than_spawning_children_that_lose_their_config(
    git_repo: Path, tmp_path: Path, scripts, state: Path
):
    """`_start` already stops the wave on a creation failure, and its reasoning fits:
    the fault is identical for every seat, so spawning three more children to destroy
    three more copies of the repository's config tells nobody anything new."""
    from gbfleet.supervisor import Limits, up

    from test_supervisor import _factory, _seats, _server

    _commit_a_seat_path(git_repo, ".grok/config.toml", '[permission]\ndeny = ["Bash"]\n')
    workspace = tmp_path / "ws"

    wave = up(
        git_repo,
        _seats(2),
        _factory(scripts, "works_then_exits"),
        _server(workspace),
        limits=Limits(max_workers=2),
        state=state,
        workspace=workspace,
        poll=0.05,
    )

    assert not wave.spawned, "children were started into repositories about to be clobbered"
    assert wave.failures, "the wave ended clean despite refusing to start anything"
    assert any(".grok/config.toml" in f for f in wave.failures), wave.failures

    # And the repository is untouched, which is the entire point.
    assert "deny" in (git_repo / ".grok" / "config.toml").read_text(encoding="utf-8")
