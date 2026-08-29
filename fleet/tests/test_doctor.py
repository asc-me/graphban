"""GRPH-599: the preflight, and its one rule.

`up` and `stdio` both begin by creating worktrees and spending money. Every trap found
while porting this to Windows is one an operator meets on their first run, and several
are silent or attributed to the wrong component. None of them needs a child running to
be answerable.

**The rule these tests exist to hold down: a check that could not run is not a check
that passed.** Two outcomes force every unanswerable question into one of the answers,
and this repository's whole catalogue of defects is what that costs — a skip that reads
as verified, an absent file that reads as a clean tree, a `0o600` that means nothing on
the platform it was printed on. So there are three, and `UNKNOWN` is neither counted as
success nor left out of the summary.

The module earned its keep before it had tests: on its first run it found GRPH-600, a
regression that made the supervisor unable to take its own lock, and reported it as
`UNKNOWN` rather than `PASS`.
"""

from __future__ import annotations

import io
import json
import subprocess
import sys
from pathlib import Path

import httpx
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from gbfleet import doctor  # noqa: E402
from gbfleet.cli import main  # noqa: E402
from gbfleet.client import Graphban  # noqa: E402
from gbfleet.doctor import FAIL, PASS, UNKNOWN, Report  # noqa: E402


def _git(root: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=root, capture_output=True, text=True, check=True
    ).stdout


def _status(report: Report, name: str) -> str:
    return next(f.status for f in report.findings if f.name == name)


def _run(repo: Path, **kw) -> Report:
    return doctor.run(repo=repo, out=io.StringIO(), **kw)


# --- the rule ------------------------------------------------------------------------

def test_every_finding_carries_one_of_the_three_outcomes(git_repo: Path):
    report = _run(git_repo)
    assert report.findings, "the doctor reported nothing at all"
    for finding in report.findings:
        assert finding.status in (PASS, FAIL, UNKNOWN), (
            f"{finding.name} reported {finding.status!r}, which is not an outcome"
        )


def test_unknown_does_not_count_as_ok_in_the_summary(git_repo: Path):
    """A summary reading "0 failed" while three questions went unanswered is the exact
    shape this module exists to avoid."""
    out = io.StringIO()
    report = Report()
    report.add("something checkable", PASS)
    report.add("something that could not be checked", UNKNOWN, "no network")
    report.render(out)
    printed = out.getvalue()

    assert "1 ok, 0 failed, 1 unknown" in printed, printed
    assert "unknown is not ok" in printed, (
        "the summary counted an unanswered question without saying it was unanswered"
    )


def test_unknown_alone_does_not_stop_a_run(git_repo: Path):
    """Deliberate. Refusing to start because a check could not be MADE would ground the
    fleet on a slow network or an unreadable temp directory — so it is printed loudly
    and the operator decides."""
    report = Report()
    report.add("unanswerable", UNKNOWN, "no server given")
    assert report.ok is True
    report.add("broken", FAIL, "a real problem")
    assert report.ok is False


# --- the traps it exists to catch ----------------------------------------------------

def test_a_repo_that_commits_a_seat_path_fails(git_repo: Path):
    """GRPH-581, front-loaded. Today this refuses at worktree creation — after the run
    has started and after the operator has committed to it."""
    target = git_repo / ".grok"
    target.mkdir()
    (target / "config.toml").write_text('[permission]\ndeny = ["Bash"]\n', encoding="utf-8")
    _git(git_repo, "add", "-A")
    _git(git_repo, "commit", "-qm", "team grok config")

    report = _run(git_repo)
    assert _status(report, "repository does not commit a seat path") == FAIL
    assert not report.ok


def test_a_clean_repo_passes_that_check(git_repo: Path):
    """The control. A check that failed every repository would be worse than none."""
    report = _run(git_repo)
    assert _status(report, "repository does not commit a seat path") == PASS


def test_a_repo_with_no_commits_is_unknown_rather_than_clean(tmp_path: Path):
    """There is nothing to look at yet, which is not the same as having looked and found
    nothing — and it is the second reading that would let a committed seat path through
    on the next commit."""
    empty = tmp_path / "empty"
    empty.mkdir()
    _git(empty, "init", "-q", "-b", "main")

    report = _run(empty)
    assert _status(report, "repository does not commit a seat path") == UNKNOWN


def test_a_missing_api_key_fails_and_leaves_the_server_unknown(git_repo: Path):
    """Not PASS, and not silence. The server was never asked, so nothing is known about
    it, and saying so is the point."""
    report = _run(git_repo, server="https://graphban.invalid", api_key=None)
    assert _status(report, "api key") == FAIL
    assert _status(report, "server reachable") == UNKNOWN, (
        "the key was missing so the server was never contacted, and the report claimed "
        "to know something about it anyway"
    )


def test_an_unreachable_server_fails(git_repo: Path, monkeypatch):
    def unreachable(self, tool, /, **arguments):
        from gbfleet.client import ServerUnreachable

        raise ServerUnreachable("no route to host")

    monkeypatch.setattr(Graphban, "call", unreachable)
    report = _run(git_repo, server="https://graphban.invalid", api_key="k")
    assert _status(report, "server reachable") == FAIL


def test_a_reachable_server_passes(git_repo: Path, monkeypatch):
    """The control on the one above, and on the key check: without it, a doctor that
    reported every server unreachable would look just as correct."""
    monkeypatch.setattr(Graphban, "call", lambda self, tool, /, **kw: {"agents": []})
    report = _run(git_repo, server="https://graphban.invalid", api_key="k")
    assert _status(report, "server reachable") == PASS
    assert _status(report, "api key") == PASS


def test_an_adapter_without_a_debug_flag_is_unknown_not_pass(git_repo: Path):
    """`cursor-agent` and `gbagent` have no debug flag. An operator who reads a green
    line here and then turns `--debug` on would be reading an empty log wondering why."""
    report = _run(git_repo, adapter="cursor-agent")
    status = _status(report, "adapter cursor-agent supports --debug")
    assert status == UNKNOWN, f"reported {status}, so --debug looks available when it is not"


def test_an_unknown_adapter_name_fails(git_repo: Path):
    report = _run(git_repo, adapter="not-a-vendor")
    assert _status(report, "adapter") == FAIL
    assert not report.ok


def test_a_missing_seats_file_fails_and_an_empty_one_too(git_repo: Path, tmp_path: Path):
    """An empty seats file spawns nothing and reports nothing, which is the quietest
    possible way for a wave to do nothing at all."""
    assert _status(_run(git_repo, seats_file=str(tmp_path / "nope")), "seats file") == FAIL

    empty = tmp_path / "empty.txt"
    empty.write_text("\n\n", encoding="utf-8")
    assert _status(_run(git_repo, seats_file=str(empty)), "seats file") == FAIL

    real = tmp_path / "seats.txt"
    real.write_text("CODE-1\nCODE-2\n", encoding="utf-8")
    report = _run(git_repo, seats_file=str(real))
    assert _status(report, "seats file") == PASS


def test_a_directory_that_cannot_be_written_fails(git_repo: Path, tmp_path: Path):
    """The workspace is where every worktree goes."""
    blocker = tmp_path / "blocked"
    blocker.write_text("i am a file, not a directory", encoding="utf-8")
    report = _run(git_repo, workspace=blocker)
    assert _status(report, "workspace is writable") == FAIL


# --- the exit code -------------------------------------------------------------------

def test_the_command_exits_non_zero_only_on_a_real_failure(git_repo: Path, capsys):
    assert main(["doctor", "--repo", str(git_repo)]) == 0
    captured = capsys.readouterr().out
    assert "UNKNOWN" in captured, "a run with no server or adapter claimed to know everything"

    target = git_repo / ".cursor"
    target.mkdir()
    (target / "mcp.json").write_text("{}", encoding="utf-8")
    _git(git_repo, "add", "-A")
    _git(git_repo, "commit", "-qm", "committed seat path")

    assert main(["doctor", "--repo", str(git_repo)]) == 1


def test_the_report_names_a_remedy_for_what_it_refuses(git_repo: Path):
    """A preflight that says "no" without saying "instead" has moved the operator's
    problem rather than solved it."""
    report = _run(git_repo, server="https://x.invalid", api_key=None)
    failed = [f for f in report.findings if f.status == FAIL]
    assert failed
    for finding in failed:
        assert finding.remedy, f"{finding.name} failed and offered nothing to do about it"
