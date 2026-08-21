"""What the supervisor says about each child, and the things it must never say.

PRD-22 S6. The failures here are quiet ones: a field omitted rather than reported, a
credential written somewhere that outlives it, a distinction collapsed so that disk
fills while every line reads fine.
"""

from __future__ import annotations

import io
import json
import logging
from pathlib import Path

import httpx
import pytest

from gbfleet import observe
from gbfleet.observe import LOG_FILE, NEVER_REGISTERED, ChildRecord
from gbfleet.supervisor import Limits, up
from gbfleet.worktree import Disposition

from tests.test_supervisor import CODE, KEY, _factory, _seats, _server


def _lines(source) -> list[dict]:
    """Read the emitted lines, from a stream or from the rotating file.

    `up()` calls `configure()` itself — it has to, since it owns the state dir — which
    replaces any handler a test installed first. So anything that goes THROUGH `up` is
    read off the file, which is the path that actually ships. A test asserting on a
    stream `up` had already discarded would have been asserting on nothing.
    """
    text = source.getvalue() if isinstance(source, io.StringIO) else Path(source).read_text(
        encoding="utf-8"
    )
    return [json.loads(line) for line in text.splitlines() if line.strip()]


@pytest.fixture(autouse=True)
def _quiet_logger():
    yield
    logger = logging.getLogger(observe.LOGGER)
    for handler in list(logger.handlers):
        logger.removeHandler(handler)
        handler.close()


# --- registration latency, the load-bearing field ----------------------------------


def test_a_child_that_registered_reports_how_long_it_took(
    git_repo: Path, tmp_path: Path, scripts, state: Path
):
    workspace = tmp_path / "ws"
    up(
        git_repo, _seats(1), _factory(scripts, "works_then_exits"), _server(workspace),
        state=state, workspace=workspace,
    )
    records = [r for r in _lines(state / LOG_FILE) if r["event"] == "child"]
    assert len(records) == 1
    assert isinstance(records[0]["registration_latency"], (int, float))
    assert records[0]["agent_id"]


def test_a_child_that_never_registered_says_so_rather_than_saying_nothing():
    """The distinction the whole field exists for.

    Omitting it reads as "nothing to report" and zeroing it reads as "instant". What
    actually happened is a process that ran, spent money, produced nothing, and left the
    roster one agent short — which is indistinguishable from a slow start unless
    something puts a word on it.
    """
    record = ChildRecord(
        adapter="grok", binary_version="1.0.5", worktree="/w", branch="gb/w-1", pid=42
    )
    assert record.registration_latency == NEVER_REGISTERED

    payload = record.as_dict()
    assert "registration_latency" in payload, "omitted reads as nothing to report"
    assert payload["registration_latency"] != 0, "zero reads as instant"


def test_a_child_that_ran_and_never_registered_is_recorded_as_such(
    git_repo: Path, tmp_path: Path, scripts, state: Path
):
    """The end-to-end silent drop, which no other test here reaches.

    The server is up and answering and simply never sees the child — a broken adapter,
    not a partition. The child is killed inside the registration window, and its record
    has to SAY that rather than report a latency of zero: zero reads as instant, which
    is the one thing that did not happen.

    A sabotage replacing NEVER_REGISTERED with 0.0 survived until this existed, because
    every other test uses a child that registers and never reaches the else branch.
    """
    workspace = tmp_path / "ws"
    wave = up(
        git_repo, _seats(1), _factory(scripts, "sleeper"),
        _server(workspace, blind=True),
        limits=Limits(registration_window=1.0),
        state=state, workspace=workspace, poll=0.05,
    )
    assert wave.ok is False

    record = next(r for r in _lines(state / LOG_FILE) if r["event"] == "child")
    assert record["registration_latency"] == NEVER_REGISTERED
    assert record["agent_id"] is None
    assert record["stopped_because"] == "never_registered"


# --- the credential must not reach the log -----------------------------------------


def test_no_seat_code_reaches_stdout_or_the_file(
    git_repo: Path, tmp_path: Path, scripts, state: Path
):
    """A seat is single-use and lives thirty minutes. A rotating log file is neither, so
    a code written there outlives every protection the seat design gives it."""
    workspace = tmp_path / "ws"
    up(
        git_repo, _seats(2), _factory(scripts, "works_then_exits"), _server(workspace),
        state=state, workspace=workspace,
    )
    on_disk = (state / LOG_FILE).read_text(encoding="utf-8")
    for blob in (on_disk,):
        assert CODE not in blob
        assert KEY not in blob, "the API key reached the log"


def test_the_record_carries_the_seat_id_which_is_not_the_code(
    git_repo: Path, tmp_path: Path, scripts, state: Path
):
    """`seat_id` is the enrolment's ROW id, readable off the roster since GRPH-451 put
    it there. Before that a supervisor could not name the seat its own child redeemed."""
    workspace = tmp_path / "ws"
    up(
        git_repo, _seats(1), _factory(scripts, "works_then_exits"), _server(workspace),
        state=state, workspace=workspace,
    )
    record = next(r for r in _lines(state / LOG_FILE) if r["event"] == "child")
    assert record["seat_id"], "the roster carries enrolment_id and nothing read it"
    assert record["seat_id"] != CODE


# --- the fields S6 names ------------------------------------------------------------


def test_the_record_names_the_build_not_just_the_vendor(
    git_repo: Path, tmp_path: Path, scripts, state: Path
):
    """"Adapter and resolved binary version" is the first entry in S6's field list. A
    record naming only the vendor cannot answer "did this start failing when they
    shipped 2.2?"."""
    workspace = tmp_path / "ws"
    factory = _factory(scripts, "works_then_exits")

    def versioned(seat, tree, instruction):
        from dataclasses import replace

        return replace(factory(seat, tree, instruction), binary_version="9.9.9 (fake)")

    up(git_repo, _seats(1), versioned, _server(workspace), state=state, workspace=workspace)
    record = next(r for r in _lines(state / LOG_FILE) if r["event"] == "child")
    assert record["binary_version"] == "9.9.9 (fake)"


@pytest.mark.parametrize(
    "field", ["adapter", "worktree", "branch", "pid", "exit_code", "reap"]
)
def test_every_field_s6_asks_for_is_present(
    field: str, git_repo: Path, tmp_path: Path, scripts, state: Path
):
    workspace = tmp_path / "ws"
    up(
        git_repo, _seats(1), _factory(scripts, "works_then_exits"), _server(workspace),
        state=state, workspace=workspace,
    )
    record = next(r for r in _lines(state / LOG_FILE) if r["event"] == "child")
    assert record.get(field) is not None, f"S6 names {field} and the record omits it"


def test_a_salvaged_reap_is_reported_as_salvaged(
    git_repo: Path, tmp_path: Path, scripts, state: Path
):
    """`left_dirty` must survive as a distinct outcome or disk fills while every line
    reads fine. Asserted here on the positive case, with the enum's own values so a
    renamed disposition cannot silently become an unrecognised string."""
    workspace = tmp_path / "ws"
    up(
        git_repo, _seats(1), _factory(scripts, "works_then_exits"), _server(workspace),
        state=state, workspace=workspace,
    )
    record = next(r for r in _lines(state / LOG_FILE) if r["event"] == "child")
    assert record["reap"] in {d.value for d in Disposition}
    assert record["reap"] == Disposition.SALVAGED.value


def test_a_credential_in_branch_history_is_in_the_record():
    record = ChildRecord(
        adapter="cursor-agent", binary_version="2026.04.17", worktree="/w",
        branch="gb/w-1", pid=1, credential_in_history=[".cursor/mcp.json"],
    )
    assert record.as_dict()["credential_in_history"] == [".cursor/mcp.json"]


# --- where it writes -----------------------------------------------------------------


def test_it_writes_to_the_state_dir_and_rotates(state: Path):
    """Rotation matters because the supervisor is the one component expected to run
    unattended for a long time. `RotatingFileHandler` is stdlib — reaching for it rather
    than writing rotation is the whole of the reasoning."""
    observe.configure(state)
    for i in range(50):
        observe.emit("noise", i=i, filler="x" * 200)

    path = state / LOG_FILE
    assert path.exists()
    assert path.read_text(encoding="utf-8").strip().splitlines()

    handler = next(
        h for h in logging.getLogger(observe.LOGGER).handlers
        if isinstance(h, logging.handlers.RotatingFileHandler)
    )
    assert handler.maxBytes > 0 and handler.backupCount > 0, "an unbounded log is not a log"


def test_it_never_opens_a_network_connection():
    """S6 says never a telemetry endpoint, and the reason is D-i: this is the component
    whose job is to keep working when the network is gone. A logger that phones home
    puts a network dependency in exactly the wrong place."""
    source = Path(observe.__file__).read_text(encoding="utf-8")
    for bad in ("httpx", "urllib", "requests", "socket", "http.client", "SocketHandler"):
        assert bad not in source, f"observe.py reaches for {bad}"


def test_configuring_twice_does_not_double_every_line(state: Path):
    """Called once per wave, and a supervisor that runs several would otherwise report
    each child twice, then four times."""
    out = io.StringIO()
    observe.configure(state, stream=out)
    observe.configure(state, stream=out)
    observe.emit("once")
    assert len(_lines(out)) == 1
