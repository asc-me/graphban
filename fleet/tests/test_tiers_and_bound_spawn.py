"""PRD-36 PR 2 — spawn by tier, the bound instruction, and gbagent's `--item` (criteria 10, 11, 17)."""
from __future__ import annotations

from pathlib import Path

import pytest

from gbfleet import mcp
from gbfleet.mcp import Fleet, TOOLS, handle
from gbfleet.seat import Seat, instruction_for
from gbfleet.tiers import TierTable, UnknownTier
from gbfleet.adapters.gbagent import GbAgent
from gbfleet.worktree import Worktree
from tests.test_supervisor import _factory, _server


def _call(fleet: Fleet, tool: str, **args) -> dict:
    reply = handle(fleet, {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                           "params": {"name": tool, "arguments": args}})
    return reply["result"]


# ---- the table (D6, D16) ----------------------------------------------------------------------

def test_the_table_parses_name_adapter_model_and_splits_on_the_first_colon_only():
    table = TierTable.parse(["cheap=gbagent:qwen3.6:35b-a3b-coding-mtp-det", "frontier=claude:opus", "bare=cursor"])
    assert table.resolve("cheap").adapter == "gbagent"
    assert table.resolve("cheap").model == "qwen3.6:35b-a3b-coding-mtp-det"
    assert table.resolve("frontier").model == "opus"
    assert table.resolve("bare").model == ""
    assert table.describe()["cheap"] == {"adapter": "gbagent", "model": "qwen3.6:35b-a3b-coding-mtp-det"}


def test_an_unmapped_tier_is_refused_naming_the_flag():
    with pytest.raises(UnknownTier, match=r"--tier cheap=<adapter>"):
        TierTable.parse(["frontier=claude:opus"]).resolve("cheap")


@pytest.mark.parametrize("bad", ["cheap", "=gbagent", "cheap=", "cheap=:model"])
def test_a_malformed_mapping_is_refused_at_parse(bad):
    with pytest.raises(ValueError):
        TierTable.parse([bad])


# ---- spawn(tier) (criterion 10) ------------------------------------------------------------------

@pytest.fixture
def fleet(git_repo: Path, tmp_path: Path, scripts, state: Path) -> Fleet:
    workspace = tmp_path / "ws"
    seen: list[tuple[str, str]] = []

    def launch_for(name, model="", tuning=None):
        seen.append((name, model))
        return _factory(scripts, "works_then_waits", adapter=name)

    f = Fleet(repo=git_repo, workspace=workspace, client=_server(workspace), launch_for=launch_for,
              tiers=TierTable.parse(["cheap=fake:qwen-local", "frontier=fake:opus"]))
    f.seen = seen  # type: ignore[attr-defined]
    return f


def test_spawn_by_tier_resolves_adapter_and_model_and_says_so(fleet: Fleet):
    out = _call(fleet, "spawn", tier="cheap", enrolment_code="WORKER-1")
    assert not out.get("isError"), out
    got = out["structuredContent"]
    assert fleet.seen == [("fake", "qwen-local")]  # type: ignore[attr-defined]
    assert got["adapter"] == "fake" and got["model"] == "qwen-local" and got["tier"] == "cheap"
    assert got["assigned"] is None, "an unbound seat hands the child nothing, and says so"


def test_spawn_with_an_unmapped_tier_is_a_tool_error_naming_the_flag(fleet: Fleet):
    out = _call(fleet, "spawn", tier="turbo", enrolment_code="WORKER-1")
    assert out.get("isError")
    assert "--tier turbo=" in out["content"][0]["text"]
    assert fleet.children == []


def test_an_explicit_adapter_overrides_the_tier_and_the_reply_says_which_ran(fleet: Fleet):
    out = _call(fleet, "spawn", tier="cheap", adapter="fake", model="named", enrolment_code="WORKER-1")
    got = out["structuredContent"]
    assert fleet.seen == [("fake", "named")]  # type: ignore[attr-defined]
    assert got["tier"] is None and got["model"] == "named"


def test_spawn_echoes_the_roster_assigned_block(git_repo: Path, tmp_path: Path, scripts, state: Path):
    """Criterion 17 / D15: the child's registration reply is on the roster; spawn echoes it."""
    workspace = tmp_path / "ws"
    server = _server(workspace)
    # Wrap the roster so every agent row carries a bound-seat verdict.
    real = server.fleet_status

    def with_assigned(**kw):
        payload = real(**kw)
        for a in payload.get("agents") or []:
            a["assigned"] = {"item": "GRPH-7", "state": "claimed", "held_by": None}
        return payload

    server.fleet_status = with_assigned  # type: ignore[method-assign]
    f = Fleet(repo=git_repo, workspace=workspace, client=server,
              launch_for=lambda name, model="", tuning=None: _factory(scripts, "works_then_waits", adapter=name))
    got = _call(f, "spawn", adapter="fake", enrolment_code="WORKER-1", item="GRPH-7")["structuredContent"]
    assert got["assigned"] == {"item": "GRPH-7", "state": "claimed", "held_by": None}


# ---- the bound instruction and gbagent's --item (criterion 11) --------------------------------------

def test_a_bound_seat_instruction_names_the_item_and_forbids_claiming():
    seat = Seat(code="WORKER-1", server_url="http://gb.invalid", api_key="k", item="GRPH-7")
    text = instruction_for(seat, Path("/wt"), "gb/wave-1")
    assert "BOUND to GRPH-7" in text and "you HOLD GRPH-7" in text
    assert "Do NOT call claim_cluster or claim_next" in text
    assert "If `assigned.state` is `taken`" in text and "EXIT" in text
    assert "Do NOT set parent_agent_id" in text, "PRD-22 D-b survives the binding"
    unbound = instruction_for(Seat(code="WORKER-2", server_url="http://gb.invalid", api_key="k"), Path("/wt"), "b")
    assert "BOUND" not in unbound and "claim_cluster" in unbound


def test_gbagent_is_launched_with_item_only_for_a_bound_seat(tmp_path: Path):
    tree = Worktree(path=tmp_path / "wt", branch="gb/w-1", repo=tmp_path, base="main")
    adapter = GbAgent()
    bound = adapter.launch(Seat(code="W", server_url="http://gb.invalid", api_key="k", item="GRPH-7"),
                           tree, tmp_path / "i", tmp_path / "gbagent", model="qwen")
    unbound = adapter.launch(Seat(code="W", server_url="http://gb.invalid", api_key="k"),
                             tree, tmp_path / "i", tmp_path / "gbagent", model="qwen")
    assert "--item" in bound.argv and bound.argv[bound.argv.index("--item") + 1] == "GRPH-7"
    assert "--item" not in unbound.argv


def test_spawn_passes_turns_and_window_through_to_the_tuning(git_repo: Path, tmp_path: Path, scripts, state: Path):
    """gbagent refuses to guess either (PRD-24 D6/D7), and `spawn` had no way to say them —
    found by the PRD-36 criterion-18 check: the child exited 2 before registering."""
    workspace = tmp_path / "ws"
    seen: list = []

    def launch_for(name, model="", tuning=None):
        seen.append(tuning)
        return _factory(scripts, "works_then_waits", adapter=name)

    f = Fleet(repo=git_repo, workspace=workspace, client=_server(workspace), launch_for=launch_for)
    out = _call(f, "spawn", adapter="fake", enrolment_code="WORKER-1", turns=40, window=262144)
    assert not out.get("isError"), out
    assert seen[0].turns == "40" and seen[0].window == "262144"
    assert seen[0].named() == {"turns", "window"}
