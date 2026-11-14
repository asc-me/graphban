"""GRPH-1648 — a supervisor that cannot record its seats refuses to start, and stops mid-wave.

A planner over MCP minted the seats on one key; `gbfleet up` ran on another. Every
observation was a masked `404 no such item`, the supervisor printed `FAILED … 404` per call
and kept spawning, and items reached `done` with no base, lease or PR recorded.

Sabotage: delete the `seats_unrecordable` block in `supervisor.up` and
`test_up_refuses_a_seat_this_key_cannot_record_before_any_child` spawns a child. Delete
the one in `until.run` and `test_until_refuses_a_seats_file_this_key_cannot_record` mints
and spawns. Drop the `receipts_blocked` early return in `heartbeat.note_refused` and
`test_a_404_mid_wave_is_reported_once_and_stops_spawning` sees one line per call.
"""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

import httpx

from gbfleet.client import ObservationRefused
from gbfleet.seat import Seat
from gbfleet.supervisor import Limits, Wave, _observe, seats_unrecordable, up
from gbfleet.until import run

from tests.test_supervisor import KEY, _factory, _seats, _server
from tests.test_until import _clients

REFUSAL = ("the seat was minted by agent GRPH-A7 on an UNSCOPED key (gbk_abcd), which can "
           "never supervise its own seats; run the planner on the project-scoped key "
           "`gban setup` mints")


def _seat_answer(rows):
    def answer(request: httpx.Request) -> httpx.Response | None:
        if request.url.path != "/api/fleet/observations":
            return None
        body = json.loads(request.content)
        if body.get("kind") != "seats":
            return None
        return httpx.Response(200, json={"kind": "seats", "ok": False, "checked": rows(body)})
    return answer


def _intercept(gb, answer):
    """The same client, with the seats check answered by `answer` first."""
    inner = gb.transport

    class Front(httpx.BaseTransport):
        def handle_request(self, request):
            got = answer(request)
            return got if got is not None else inner.handle_request(request)

    return replace(gb, transport=Front(), _http=None)


def _refusing(body):
    return [{"index": i, "seat": "WORKER-", "ok": i != 0, "minted_by": "GRPH-A7",
             **({} if i else {"reason": REFUSAL})}
            for i, _ in enumerate(body.get("seats") or [])]


class _Client:
    def __init__(self, reply=None, exc=None):
        self.reply, self.exc, self.posts = reply, exc, []

    def post_observation(self, **payload):
        self.posts.append(payload)
        if self.exc:
            raise self.exc
        return self.reply


def test_the_check_names_each_refused_seat_and_never_the_code():
    seats = _seats(2)
    client = _Client(reply={"checked": [
        {"index": 0, "seat": "WORKER-", "ok": False, "reason": REFUSAL},
        {"index": 1, "seat": "WORKER-", "ok": True}]})
    got = seats_unrecordable(client, seats)
    assert client.posts == [{"item_id": "", "kind": "seats",
                             "seats": [s.code for s in seats]}]
    assert got == [f"seat #1 (WORKER-): {REFUSAL}"]
    assert seats[0].code not in got[0]


def test_a_check_that_did_not_run_is_a_refusal_not_a_pass():
    assert seats_unrecordable(_Client(reply={"recorded": True}), _seats(1))
    old = _Client(exc=ObservationRefused("HTTP 422: an observation names an item", status=422))
    assert "HTTP 422" in seats_unrecordable(old, _seats(1))[0]
    refused = _Client(exc=ObservationRefused("HTTP 403", status=403))
    assert "cannot record observations at all" in seats_unrecordable(refused, _seats(1))[0]
    # Unreachable is the allocation read's to report; it parks every seat on its own.
    assert seats_unrecordable(_Client(exc=ObservationRefused("timeout")), _seats(1)) == []
    assert seats_unrecordable(_Client(reply={"checked": []}), []) == []


def test_up_refuses_a_seat_this_key_cannot_record_before_any_child(
    git_repo: Path, tmp_path: Path, scripts, state: Path,
):
    workspace = tmp_path / "ws"
    client = _intercept(_server(workspace), _seat_answer(_refusing))
    wave = up(git_repo, _seats(2), _factory(scripts, "works_then_exits"), client,
              limits=Limits(max_workers=4), state=state, workspace=workspace)
    assert wave.spawned == []
    assert wave.ok is False and wave.reason == "seats-unrecordable"
    assert wave.unused_seats == 2
    said = " ".join(wave.failures)
    assert "refusing to start" in said and "GRPH-A7" in said and "gban setup" in said


def test_up_starts_when_every_seat_is_recordable(
    git_repo: Path, tmp_path: Path, scripts, state: Path,
):
    """The control: the same wave on a key that may record every seat spawns."""
    workspace = tmp_path / "ws"
    wave = up(git_repo, _seats(1), _factory(scripts, "works_then_exits"), _server(workspace),
              limits=Limits(max_workers=4), state=state, workspace=workspace)
    assert len(wave.spawned) == 1, wave.failures


def test_until_refuses_a_seats_file_this_key_cannot_record(
    git_repo: Path, tmp_path: Path, scripts, state: Path,
):
    workspace = tmp_path / "ws"
    calls: list = []
    planner, supervisor = _clients(workspace, clusters=1, workers=1, calls=calls)
    supervisor = _intercept(supervisor, _seat_answer(_refusing))
    result = run(
        git_repo, _factory(scripts, "works_then_exits"),
        planner, supervisor, api_key=KEY, server="http://gb.invalid", adapter="fake",
        seats=[Seat(code="WORKER-AAAAAA", server_url="http://gb.invalid", api_key=KEY)],
        state=state, workspace=workspace, poll=0, sleep=lambda _: None, empty_ticks=1,
    )
    assert result.reason == "config" and result.exit == 2 and result.ok is False
    assert "GRPH-A7" in result.detail and "UNSCOPED" in result.detail
    assert "WORKER-AAAAAA" not in result.detail
    assert "mint_enrolment" not in calls and "delegate" not in calls
    assert result.spawned == 0


def test_a_404_mid_wave_is_reported_once_and_stops_spawning():
    wave = Wave()

    class Masked:
        def post_observation(self, **_):
            raise ObservationRefused("HTTP 404: no such item", status=404)

    for item in ("GRPH-1", "GRPH-2", "GRPH-3"):
        _observe(wave, Masked(), item, "base not recorded", kind="cut_from", branch="gb/x")
    assert wave.observations_refused == 3
    assert len(wave.failures) == 1
    assert "receipts aren't being recorded" in wave.failures[0]
    assert "No more children are spawned" in wave.receipts_blocked


def test_a_transient_refusal_still_reads_one_line_each_and_does_not_block():
    wave = Wave()

    class Flaky:
        def post_observation(self, **_):
            raise ObservationRefused("HTTP 502", status=502)

    _observe(wave, Flaky(), "GRPH-1", "base not recorded", kind="cut_from", branch="gb/x")
    _observe(wave, Flaky(), "GRPH-2", "base not recorded", kind="cut_from", branch="gb/x")
    assert len(wave.failures) == 2 and wave.receipts_blocked == ""


def test_up_parks_the_rest_once_receipts_are_blocked(
    git_repo: Path, tmp_path: Path, scripts, state: Path, monkeypatch,
):
    """Mid-wave: once a receipt was refused outright, the seats still pending never start.

    The first child's receipts are refused (simulated at its start, which is where a real
    wave's first `cut_from` posts). Sabotage: drop the `receipts_blocked` gate in `refill`
    and all three seats spawn."""
    from gbfleet import supervisor as sup

    real = sup._start

    def start_then_refused(wave, *a, **kw):
        real(wave, *a, **kw)
        sup.note_refused(wave, "GRPH-1", "base not recorded",
                         ObservationRefused("HTTP 404: no such item", status=404))

    monkeypatch.setattr(sup, "_start", start_then_refused)
    workspace = tmp_path / "ws"
    wave = up(git_repo, _seats(3), _factory(scripts, "works_then_exits"), _server(workspace),
              limits=Limits(max_workers=1), state=state, workspace=workspace)
    assert len(wave.spawned) == 1
    assert wave.unused_seats == 2
    assert all("receipts aren't being recorded" in u for u in wave.unredeemed)
    assert sum("receipts aren't being recorded" in f for f in wave.failures) == 1
    assert wave.ok is False
