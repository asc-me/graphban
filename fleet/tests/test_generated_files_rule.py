"""GRPH-1424: `build.generated_files` decides which undeclared files the hold ignores.

Driven through `_note_touchpoints`, the reap-time hold itself: a test that called
`is_generated` alone would stay green with the rule unwired from the hold.
"""
from gbfleet import observe, supervisor as sup
from gbfleet.supervisor import Wave, _note_touchpoints

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
