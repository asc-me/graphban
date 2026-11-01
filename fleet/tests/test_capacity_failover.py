"""GRPH-1247 (Capacity S1): a vendor limit marks the adapter exhausted and the wave goes on,
or ends `capacity` — never `cap`, never the old `vendor_limit`."""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from gbfleet.spawn import VendorLimit
from gbfleet.supervisor import Limits, Wave
from gbfleet.until import Report, _skip_exhausted, run

from tests.test_supervisor import KEY
from tests.test_until import _clients

SAID = "You've hit your limit · resets 12:20pm (America/New_York)"


def _limited(seat, tree, instruction_file, debug_file=None):
    raise VendorLimit("out of quota", adapter="fake", said=SAID)


def test_no_candidate_left_ends_the_wave_capacity_and_quotes_the_reset(
    git_repo: Path, tmp_path: Path, state: Path,
):
    """Sabotage: delete the `CapError("capacity")` raise in `failover` and this comes back
    `vendor_limit`."""
    workspace = tmp_path / "ws"
    planner, supervisor = _clients(workspace, clusters=1, cluster_items=[["GRPH-7"]])
    result = run(
        git_repo, _limited, planner, supervisor, api_key=KEY, server="http://gb.invalid",
        adapter="fake", state=state, workspace=workspace, poll=0, sleep=lambda _: None,
        empty_ticks=1, limits=Limits(max_workers=1),
    )
    assert result.reason == "capacity"
    assert result.reason != "cap"
    assert result.exit == 1
    payload = result.as_json()
    assert [e["adapter"] for e in payload["exhausted"]] == ["fake"]
    assert payload["exhausted"][0]["said"] == SAID
    assert payload["exhausted"][0]["seen_at"] > 0


def test_exhausted_is_always_in_the_terminal_json():
    assert Report(ok=True, reason="idle", exit=0).as_json()["exhausted"] == []


def test_an_exhausted_adapter_is_dropped_from_resolution():
    wave = Wave()
    wave.exhausted["claude"] = {"adapter": "claude", "said": SAID, "seen_at": 1}
    check = _skip_exhausted(wave, lambda row: (True, ""))
    ok, why = check(SimpleNamespace(harness="claude"))
    assert not ok and "12:20pm" in why
    assert check(SimpleNamespace(harness="codex")) == (True, "")


def test_a_limit_continues_on_the_next_candidate_and_never_returns_to_the_exhausted_one(
    git_repo: Path, tmp_path: Path, scripts, state: Path, monkeypatch,
):
    """Sabotage: drop `_skip_exhausted` from `next_candidate` and the matrix hands the same
    winner back, so `asked` repeats the first harness and the wave ends `capacity`."""
    from gbfleet import matrix as matrix_mod
    from tests.test_supervisor import _factory

    monkeypatch.setattr(matrix_mod, "installed_checker", lambda *a, **k: (lambda row: (True, "")))
    workspace = tmp_path / "ws"
    planner, supervisor = _clients(
        workspace, clusters=1, cluster_items=[["GRPH-7"]], workers=0,
    )
    asked: list[str] = []
    good = _factory(scripts, "works_then_exits")

    def launch_for(adapter: str, model: str):
        asked.append(adapter)
        return _limited if len(asked) == 1 else good

    result = run(
        git_repo, good, planner, supervisor, api_key=KEY, server="http://gb.invalid",
        adapter="fake", state=state, workspace=workspace, poll=0, sleep=lambda _: None,
        empty_ticks=1, request="cheap", limits=Limits(max_workers=1),
        launch_for=launch_for, matrix=matrix_mod.load(),
    )
    assert result.reason != "capacity", result.detail
    assert result.spawned == 1
    assert len(asked) == 2 and asked[0] != asked[1]
    assert not result.wave.failures
    assert [e["adapter"] for e in result.as_json()["exhausted"]] == [asked[0]]
