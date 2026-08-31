"""GRPH-579: whether the supervisor can tell a working child from a stuck one.

The question these tests exist to keep answerable is narrow and was previously
unanswerable: **a child is running — is it doing anything?** Before this, the only
signal was the server roster's `offline` flag, and `Limits.disowned_after` says plainly
what that is worth — `offline` is what a revoked child and a *busy* one both produce, so
it is set to 1800s and a stuck child is invisible for half an hour and then reported as
a network problem.

Every assertion here has a control. "This child was reported quiet" means nothing unless
an equally long-lived child that *did* produce output was not reported, so both run.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from gbfleet.adapters import ADAPTERS  # noqa: E402
from gbfleet.observe import LOG_FILE  # noqa: E402
from gbfleet.progress import NEVER_WROTE, Output  # noqa: E402
from gbfleet.supervisor import Limits, up  # noqa: E402

from test_supervisor import _factory, _seats, _server  # noqa: E402


def _lines(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]


# --- the reading itself -------------------------------------------------------------

def test_a_child_that_never_wrote_is_not_reported_as_silent_for_zero_seconds(tmp_path: Path):
    """`NEVER_WROTE` and `0.0` are different findings and must not collapse.

    Zero reads as "it wrote something just now". A child that has never made a sound may
    never have started properly at all, which is a worse thing and a different one.
    """
    watch = Output.watching([tmp_path / "stdout"], started_at=0.0)
    assert watch.sample(30.0).silent_for == NEVER_WROTE


def test_silence_accumulates_and_resets_when_the_child_speaks(tmp_path: Path):
    out = tmp_path / "stdout"
    watch = Output.watching([out], started_at=0.0)

    out.write_bytes(b"hello")
    assert watch.sample(1.0).silent_for == 0.0

    assert watch.sample(11.0).silent_for == 10.0
    assert watch.sample(21.0).silent_for == 20.0

    out.write_bytes(b"hello again")
    reading = watch.sample(22.0)
    assert reading.silent_for == 0.0, "the child spoke and is still counted as silent"
    assert reading.new_bytes > 0


def test_a_missing_log_is_not_counted_as_silence(tmp_path: Path):
    """A file that is not there is not a quiet child — it is a file that is not there.

    Counting it as zero bytes would report a fault against the child for something that
    happened to its log, which sends the reader to the wrong place entirely.
    """
    present, absent = tmp_path / "stdout", tmp_path / "nope"
    present.write_bytes(b"x" * 10)
    watch = Output.watching([present, absent], started_at=0.0)
    reading = watch.sample(1.0)
    assert reading.total_bytes == 10
    assert reading.watched == 1


def test_a_truncated_log_does_not_produce_negative_output(tmp_path: Path):
    """Rotation or truncation makes the file shrink. Reporting a negative delta would
    poison every rate derived from it for the rest of the run."""
    out = tmp_path / "stdout"
    out.write_bytes(b"x" * 100)
    watch = Output.watching([out], started_at=0.0)
    watch.sample(1.0)
    out.write_bytes(b"y" * 5)
    reading = watch.sample(2.0)
    assert reading.new_bytes >= 0
    assert reading.total_bytes == 5


# --- what each vendor can actually be asked for -------------------------------------

@pytest.mark.parametrize(
    "name, expected",
    [("grok", True), ("claude", True), ("cursor-agent", False), ("gbagent", False)],
)
def test_debug_support_is_declared_per_vendor_as_measured(name: str, expected: bool):
    """Measured from each `--help`, not assumed, and they genuinely differ.

    `grok`: `--debug`, `--debug-file <FILE>`. `claude`: `--debug-file <path>`, which its
    help says implicitly enables debug. `cursor-agent`: nothing — it has
    `--output-format stream-json`, which is a different feature reached a different way,
    and substituting it here would be a fabrication. `gbagent`: nothing yet.

    The falses are the important half. They are why `--debug` has to report its own gaps.
    """
    assert bool(ADAPTERS[name].debug_argv(Path("/tmp/d.log"))) is expected


@pytest.mark.parametrize("name", ["grok", "claude"])
def test_the_debug_flag_lands_before_any_positional_argument(name: str, tmp_path: Path,
                                                             git_repo: Path):
    """`claude` and `cursor-agent` end their argv with a positional prompt pointer, so a
    flag appended at the end is read as prompt text rather than as a flag. This is why
    each adapter places its own debug flags instead of the caller appending them."""
    from gbfleet.seat import Seat
    from gbfleet.worktree import create

    tree = create(git_repo, tmp_path / f"w-{name}", "wave", "1")
    seat = Seat(code="c", server_url="https://x.invalid", api_key="k")
    debug = tmp_path / "debug.log"
    launch = ADAPTERS[name].launch(
        seat, tree, tmp_path / "instr", Path(ADAPTERS[name].binary), debug_file=debug
    )

    assert str(debug) in launch.argv
    assert launch.debug_path == debug
    flag_at = launch.argv.index("--debug-file")
    trailing = [a for a in launch.argv[flag_at:] if not a.startswith("-")]
    # The value itself is allowed to follow; nothing else non-flag may precede the flag.
    leading = [a for a in launch.argv[1:flag_at] if not a.startswith("-")]
    assert not leading or all(str(x) != "--" for x in leading), launch.argv
    assert str(debug) in trailing


def test_an_adapter_without_a_debug_flag_reports_nothing_rather_than_something(
    tmp_path: Path, git_repo: Path
):
    """The branch that must stay honest. A vendor with no debug flag has to leave
    `debug_path` as None, because that None is what makes the supervisor say so."""
    from gbfleet.seat import Seat
    from gbfleet.worktree import create

    tree = create(git_repo, tmp_path / "w-cursor", "wave", "1")
    seat = Seat(code="c", server_url="https://x.invalid", api_key="k")
    launch = ADAPTERS["cursor-agent"].launch(
        seat, tree, tmp_path / "instr", Path("cursor-agent"),
        debug_file=tmp_path / "debug.log",
    )
    assert launch.debug_path is None
    assert not [a for a in launch.argv if "debug" in a]


# --- the supervisor, end to end ------------------------------------------------------

def test_a_child_that_produces_nothing_is_reported_quiet(
    git_repo: Path, tmp_path: Path, scripts, state: Path
):
    workspace = tmp_path / "ws"
    wave = up(
        git_repo,
        _seats(1),
        _factory(scripts, "silent_then_exits"),
        _server(workspace),
        limits=Limits(max_workers=1, quiet_after=0.1),
        state=state,
        workspace=workspace,
        poll=0.05,
    )
    assert wave.silent, (
        "a child ran for over a second writing nothing and the wave reported no silence "
        "— which is the state the roster also cannot see, so nobody would know"
    )
    key, seconds = next(iter(wave.silent.items()))
    assert seconds > 0


def test_a_child_that_worked_and_then_stalled_is_reported_with_a_duration(
    git_repo: Path, tmp_path: Path, scripts, state: Path
):
    """The realistic stuck child, and the one the test above does NOT cover.

    `silent_then_exits` never writes at all, so it only ever exercises `NEVER_WROTE`.
    Sabotage found that: disabling the numeric silence path entirely left every test
    green. A child that produced output and then stopped is both the commoner failure
    and the one an operator most needs named.
    """
    workspace = tmp_path / "ws"
    wave = up(
        git_repo,
        _seats(1),
        _factory(scripts, "talks_then_stalls"),
        _server(workspace),
        limits=Limits(max_workers=1, quiet_after=0.2),
        state=state,
        workspace=workspace,
        poll=0.05,
    )
    assert wave.silent, "a child that wrote once and then stalled was never reported"
    key, seconds = next(iter(wave.silent.items()))
    assert isinstance(seconds, float) and seconds >= 0.2

    # It really did speak first — otherwise this is the NEVER_WROTE path again wearing
    # a different name, and the numeric branch is still untested.
    stdout = next((workspace / "logs").rglob("stdout.log"))
    assert stdout.stat().st_size > 0, "this child never wrote, so it proves nothing new"


def test_a_child_that_goes_quiet_and_comes_back_is_no_longer_reported(
    git_repo: Path, tmp_path: Path, scripts, state: Path
):
    """Being quiet is a state, not a mark on a permanent record.

    Without the reset, a child that stalled for a minute and then worked productively
    for an hour is still named as quiet in the summary — an accusation the operator has
    to disprove by hand. `_catch_the_disowned` makes the same correction for `quiet`.
    """
    workspace = tmp_path / "ws"
    wave = up(
        git_repo,
        _seats(1),
        _factory(scripts, "stalls_then_talks"),
        _server(workspace),
        limits=Limits(max_workers=1, quiet_after=0.2),
        state=state,
        workspace=workspace,
        poll=0.05,
    )
    assert not wave.silent, (
        f"a child that went quiet and then started writing again is still reported "
        f"quiet: {wave.silent}"
    )


def test_a_child_that_keeps_writing_is_not_reported_quiet(
    git_repo: Path, tmp_path: Path, scripts, state: Path
):
    """The control. Without it the test above passes just as well against code that
    reports every child as quiet, which would be worse than reporting none."""
    workspace = tmp_path / "ws"
    wave = up(
        git_repo,
        _seats(1),
        _factory(scripts, "chatty_then_exits"),
        _server(workspace),
        limits=Limits(max_workers=1, quiet_after=1.0),
        state=state,
        workspace=workspace,
        poll=0.05,
    )
    assert not wave.silent, (
        f"a child writing every 50ms was reported as quiet: {wave.silent}. A quiet "
        "report that fires on working children is one an operator learns to ignore."
    )


def test_debug_mode_emits_a_reading_per_poll_and_plain_mode_does_not(
    git_repo: Path, tmp_path: Path, scripts, state: Path
):
    """Always measured, printed only on request.

    The measurement is one `stat` per file and is what the wave summary needs. Printing
    it every poll for an hour would bury the lines that matter in the file meant to
    carry them, so it is the debug flag that turns the printing on — and the test asserts
    both halves, because a flag that changes nothing is the easiest kind to ship.
    """
    workspace = tmp_path / "ws"
    up(
        git_repo, _seats(1), _factory(scripts, "silent_then_exits"), _server(workspace),
        limits=Limits(max_workers=1, quiet_after=0.1), state=state,
        workspace=workspace, poll=0.05, debug=True,
    )
    with_debug = [r for r in _lines(state / LOG_FILE) if r["event"] == "pulse"]
    assert with_debug, "--debug produced no per-child readings at all"
    assert {"total_bytes", "silent_for", "age"} <= set(with_debug[0])

    # Truncated, not unlinked. `RotatingFileHandler` still holds this file open, and
    # Windows refuses to delete an open file — POSIX happily unlinks one, which is why
    # this passed here and failed there with WinError 32 (GRPH-588).
    (state / LOG_FILE).write_text("", encoding="utf-8")
    workspace2 = tmp_path / "ws2"
    up(
        git_repo, _seats(1), _factory(scripts, "silent_then_exits", adapter="fake2"),
        _server(workspace2), limits=Limits(max_workers=1, quiet_after=0.1), state=state,
        workspace=workspace2, poll=0.05, wave_name="wave2",
    )
    assert not [r for r in _lines(state / LOG_FILE) if r["event"] == "pulse"], (
        "readings were printed without --debug; an hour-long run would bury every "
        "other line in the log"
    )


def test_debug_names_the_adapters_that_cannot_honour_it(
    git_repo: Path, tmp_path: Path, scripts, state: Path
):
    """The gap has to be announced, not inferred.

    An operator who passes `--debug` and sees a quiet log for a `cursor-agent` child
    would reasonably conclude the child is fine. Nothing was ever going to be written.
    """
    workspace = tmp_path / "ws"
    wave = up(
        git_repo, _seats(1), _factory(scripts, "silent_then_exits"), _server(workspace),
        limits=Limits(max_workers=1), state=state, workspace=workspace,
        poll=0.05, debug=True,
    )
    assert wave.debug_gaps, (
        "an adapter with no debug flag ran under --debug and the wave said nothing "
        "about it"
    )
    assert any(r["event"] == "debug_unavailable" for r in _lines(state / LOG_FILE))


def test_no_gap_is_reported_when_debug_was_not_asked_for(
    git_repo: Path, tmp_path: Path, scripts, state: Path
):
    """The control for the one above: "this vendor cannot do debug" is only worth saying
    to somebody who asked for debug."""
    workspace = tmp_path / "ws"
    wave = up(
        git_repo, _seats(1), _factory(scripts, "silent_then_exits"), _server(workspace),
        limits=Limits(max_workers=1), state=state, workspace=workspace, poll=0.05,
    )
    assert not wave.debug_gaps


# --- the operator has to be able to SEE it ------------------------------------------

def test_both_silences_reach_the_summary(capsys):
    """`wave.quiet` was populated by the supervisor and printed by nothing at all before
    this ticket — the field existed, its docstring said it was there so an operator would
    not have to work it out afterwards, and no output surface mentioned it.

    Both are printed and both are labelled, because they are different claims on
    different evidence: `silent` is this machine watching the child's own log files,
    `quiet` is the server reporting no heartbeat, and a partition produces the second
    without the first.
    """
    from gbfleet.cli import report
    from gbfleet.supervisor import Wave

    wave = Wave()
    wave.silent["grok:4242"] = 900.0
    wave.quiet["GRPH-A1"] = 1200.0
    wave.debug_gaps.append("cursor-agent: no debug flag; output sampling only")

    report(wave, sys.stdout)
    printed = capsys.readouterr().out

    assert "grok:4242" in printed and "900" in printed
    assert "GRPH-A1" in printed and "1200" in printed
    assert "local" in printed and "server" in printed, (
        "both silences printed but neither says which evidence it rests on"
    )
    assert "cursor-agent" in printed


def test_the_operator_command_wires_quiet_debug_and_the_summary():
    """Tests drive `up()` and `report()` as functions. The operator command is
    `gbfleet up`. Dropping `report(wave)`, `quiet_after=args.quiet_after`, or
    `debug=args.debug` from `main()` left 32 passed.
    """
    import ast
    import inspect

    from gbfleet import cli

    tree = ast.parse(inspect.getsource(cli.main))
    quiet = debug = reports = False
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            fn = node.func
            name = fn.id if isinstance(fn, ast.Name) else (
                fn.attr if isinstance(fn, ast.Attribute) else ""
            )
            keywords = {k.arg: k.value for k in node.keywords if k.arg}
            if name == "Limits" and "quiet_after" in keywords:
                q = keywords["quiet_after"]
                if isinstance(q, ast.Attribute) and q.attr == "quiet_after":
                    quiet = True
            if name == "up" and "debug" in keywords:
                d = keywords["debug"]
                if isinstance(d, ast.Attribute) and d.attr == "debug":
                    debug = True
            if name == "report":
                reports = True
    assert quiet, "gbfleet up never passes --quiet-after into Limits"
    assert debug, "gbfleet up never passes --debug into up()"
    assert reports, "gbfleet up never calls report(wave) so the operator sees no summary"


def test_a_seat_that_could_not_be_restricted_is_reported(
    git_repo: Path, tmp_path: Path, scripts, state: Path, monkeypatch
):
    """The defect this ticket is about was silence, so the report is the fix.

    D-k says none of this is a security boundary, and a workspace on a filesystem with
    no permissions at all must not stop the fleet — so it does not refuse. It must not
    be quiet either, which is exactly what `os.chmod` on Windows was.
    """
    import gbfleet.spawn as spawn_mod

    monkeypatch.setattr(spawn_mod, "is_owner_only", lambda path: False)

    workspace = tmp_path / "ws"
    wave = up(
        git_repo, _seats(1), _factory(scripts, "works_then_exits"), _server(workspace),
        limits=Limits(max_workers=1), state=state, workspace=workspace, poll=0.05,
    )

    assert wave.spawned, "refused to run instead of reporting"
    exposed = [r for r in _lines(state / LOG_FILE) if r["event"] == "credential_unrestricted"]
    assert exposed, "a seat file that is readable by others was written without a word"
    assert exposed[0]["what"] == "api key"
