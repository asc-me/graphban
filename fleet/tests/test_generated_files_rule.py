"""GRPH-1424: `build.generated_files` decides which undeclared files the hold ignores.

Driven through `_note_touchpoints`, the reap-time hold itself: a test that called
`is_generated` alone would stay green with the rule unwired from the hold.

GRPH-1823: `check-touchpoints` applies that same list. A test that only called
`_generated_rule` would stay green if the command still passed the built-in list.
"""
import json
from pathlib import Path

import pytest

from gbfleet import observe, supervisor as sup
from gbfleet.cli import RULE_UNREAD, main
from gbfleet.client import ALLOWED_TOOLS
from gbfleet.supervisor import Wave, _note_touchpoints
from gbfleet import touchpoints as tp_mod

DECLARED = ["backend/app/services"]


class _Child:
    def __init__(self, branch, held):
        self.branch, self.held_items = branch, held


class FakeClient:
    """Answers `get_item_details` with DECLARED and `get_rule_toggles` with `toggles`."""

    def __init__(self, toggles=None, exc=None):
        self.toggles, self.exc = toggles, exc

    def call(self, tool, **kw):
        if tool == "get_item_details":
            return {"id": kw["id"], "touchpoints": list(DECLARED)}
        if self.exc:
            raise self.exc
        return {"toggles": self.toggles or []}


def _rule(paths):
    return [{"rule_id": sup.GENERATED_RULE, "params": {"value": {"paths": paths}}}]


def _events(monkeypatch):
    seen = []
    monkeypatch.setattr(observe, "emit", lambda event, **f: seen.append((event, f)))
    return seen


def _reap(client, changed):
    wave = Wave()
    wave.touched["gb/w-1"] = ["backend/app/services/x.py", *changed]
    _note_touchpoints(wave, _Child("gb/w-1", ["GRPH-1"]), client)
    return wave


def _allowed(seen):
    return [f["detail"] for e, f in seen if e == "generated_files_allowed"]


def test_unset_rule_allows_todays_files_and_logs(monkeypatch):
    seen = _events(monkeypatch)
    wave = _reap(FakeClient([]), ["AGENTS.md", ".claude/agents/x.md"])
    assert wave.undeclared == {}
    assert _allowed(seen) == [".claude/agents/x.md, AGENTS.md"]
    assert ("generated_files_rule_unread" in [e for e, _ in seen])


def test_configured_paths_replace_the_default(monkeypatch):
    """Sabotage: `is_generated` ignoring `paths`, or `_note_touchpoints` calling it without
    the resolved list, and AGENTS.md is allowed again — this goes red."""
    seen = _events(monkeypatch)
    wave = _reap(FakeClient(_rule(["client/generated/**"])),
                 ["client/generated/api.ts", "AGENTS.md"])
    assert wave.undeclared == {"gb/w-1": ["AGENTS.md"]}
    assert _allowed(seen) == ["client/generated/api.ts"]
    assert "generated_files_rule_unread" not in [e for e, _ in seen]


def test_unreadable_rule_uses_todays_list_and_says_why(monkeypatch):
    seen = _events(monkeypatch)
    wave = _reap(FakeClient(exc=RuntimeError("down")), ["AGENTS.md", "client/generated/api.ts"])
    assert wave.undeclared == {"gb/w-1": ["client/generated/api.ts"]}
    assert _allowed(seen) == ["AGENTS.md"]
    unread = [f["detail"] for e, f in seen if e == "generated_files_rule_unread"]
    assert unread and "down" in unread[0]
    assert sup.generated_paths(FakeClient(exc=RuntimeError("down"))) == sup.DEFAULT_GENERATED


def test_up_client_may_read_the_rule(monkeypatch):
    """`gbfleet up` reaps with `SPAWN_READS`; without the read the rule never applies there."""
    from gbfleet.cli import SPAWN_READS
    seen = _events(monkeypatch)
    client = FakeClient(_rule(["client/generated/**"]))
    client.allowed = SPAWN_READS
    assert sup.generated_paths(client) == ("client/generated/**",)
    client.allowed = frozenset({"get_item_details"})
    assert sup.generated_paths(client) == sup.DEFAULT_GENERATED
    assert "may not call" in seen[-1][1]["detail"]


# ---- GRPH-1823: check-touchpoints applies the same list the supervisor would ---------------

GEN = "client/generated/admin.py"
STRAY = "notes/stray.py"
KEPT = "src/kept.py"
_DECLARED = [KEPT]


def _paths():
    return list(sup.DEFAULT_GENERATED) + ["client/generated/"]


def _plant(repo: Path, *rels: str) -> None:
    for rel in rels:
        path = repo / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("x\n", encoding="utf-8")


def _seat_toml(repo: Path) -> None:
    _plant(repo, ".grok/config.toml")
    (repo / ".grok" / "config.toml").write_text(
        '[mcp_servers.graphban]\n'
        'url = "http://ledger.test/api/mcp"\n'
        "enabled = true\n"
        "\n"
        "[mcp_servers.graphban.headers]\n"
        'X-API-Key = "gb_sk_test"\n',
        encoding="utf-8",
    )


def _seat_json(repo: Path) -> None:
    path = repo / ".cursor" / "mcp.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({
        "mcpServers": {
            "graphban": {
                "type": "http",
                "url": "http://ledger.test/api/mcp",
                "headers": {"X-API-Key": "gb_sk_test"},
            }
        }
    }), encoding="utf-8")


def _held_names(out: str) -> list[str]:
    marker = "this branch: "
    if marker not in out:
        return []
    return [p.strip() for p in out.split(marker, 1)[1].splitlines()[0].split(", ") if p.strip()]


class _ToggleClient:
    """What both sides read: the item's touchpoints, and `build.generated_files`."""

    def __init__(self, toggles, exc=None):
        self.toggles, self.exc = toggles, exc
        self.allowed = frozenset({"get_rule_toggles", "get_item_details"})
        self.calls: list[str] = []

    def call(self, tool, **kw):
        self.calls.append(tool)
        if self.exc and tool == "get_rule_toggles":
            raise self.exc
        if tool == "get_item_details":
            return {"id": kw["id"], "touchpoints": list(_DECLARED)}
        if tool == "get_rule_toggles":
            return {"toggles": self.toggles}
        raise AssertionError(tool)

    def post_observation(self, **payload):
        return {"recorded": True}


class _Graphban(_ToggleClient):
    """The client `check-touchpoints` constructs from the seat file."""

    def __init__(self, base_url, api_key, toggles, exc=None, **kw):
        super().__init__(toggles, exc)
        self.base_url, self.api_key = base_url, api_key
        # Match Graphban: omit `allowed` and the read is refused, not silently made.
        self.allowed = kw.get("allowed", ALLOWED_TOOLS)
        self.closed = False

    def close(self):
        self.closed = True


def _supervisor_held(changed: list[str], client) -> list[str]:
    wave = Wave()
    wave.touched["gb/w-1"] = list(changed)
    _note_touchpoints(wave, _Child("gb/w-1", ["GRPH-1"]), client)
    return list(wave.undeclared.get("gb/w-1") or [])


def _run_both(git_repo, monkeypatch, capsys, toggles, seat, exc=None):
    monkeypatch.setattr(observe, "emit", lambda *a, **k: None)
    built = []

    def factory(*a, **k):
        client = _Graphban(*a, toggles=toggles, exc=exc, **k)
        built.append(client)
        return client

    monkeypatch.setattr("gbfleet.cli.Graphban", factory)
    seat(git_repo)
    _plant(git_repo, GEN, STRAY, KEPT)
    code = main(["check-touchpoints", "--path", str(git_repo), "--item", "GRPH-1",
                 "--declared", KEPT])
    out = capsys.readouterr().out
    measured = tp_mod.measure_checkout(str(git_repo))
    sup_held = _supervisor_held(measured, _ToggleClient(toggles, exc))
    return code, out, _held_names(out), sup_held, built


@pytest.mark.parametrize("seat", [_seat_toml, _seat_json])
def test_check_touchpoints_agrees_with_the_supervisor_when_the_rule_adds_a_prefix(
        git_repo, monkeypatch, capsys, seat):
    """An adopted `client/generated/` lets admin.py through on both sides, and still holds
    a file the rule does not name.

    Sabotage: make check-touchpoints pass `ignore=is_generated` with no paths — the
    built-in list only. admin.py is named, the two sides disagree, and this goes red.
    """
    code, out, cli_held, sup_held, built = _run_both(
        git_repo, monkeypatch, capsys, _rule(_paths()), seat)
    assert cli_held == sup_held == [STRAY]
    assert code == 1 and GEN not in out
    assert built and built[0].base_url == "http://ledger.test"
    assert built[0].api_key == "gb_sk_test" and built[0].closed
    assert "get_rule_toggles" in built[0].calls
    assert RULE_UNREAD not in out


def test_an_observing_generated_files_rule_still_holds_the_file_on_both_sides(
        git_repo, monkeypatch, capsys):
    toggles = [{"rule_id": sup.GENERATED_RULE, "state": "observing",
                "params": {"value": {"paths": _paths()}}}]
    code, out, cli_held, sup_held, built = _run_both(
        git_repo, monkeypatch, capsys, toggles, _seat_toml)
    assert cli_held == sup_held == [GEN, STRAY]
    assert code == 1 and GEN in out and RULE_UNREAD not in out
    assert built[0].calls == ["get_rule_toggles"]


def test_an_unreadable_rule_uses_the_built_in_list_on_both_sides_and_says_so(
        git_repo, monkeypatch, capsys):
    code, out, cli_held, sup_held, _built = _run_both(
        git_repo, monkeypatch, capsys, _rule(_paths()), _seat_toml,
        exc=RuntimeError("down"))
    assert cli_held == sup_held == [GEN, STRAY]
    assert code == 1 and RULE_UNREAD in out and "down" in out
