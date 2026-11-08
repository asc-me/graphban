"""GRPH-1424: `build.generated_files` decides which undeclared files the hold ignores."""
from gbfleet import observe, supervisor as sup


class FakeClient:
    def __init__(self, toggles=None, exc=None):
        self.toggles, self.exc = toggles, exc

    def call(self, tool, **kw):
        if self.exc:
            raise self.exc
        return {"toggles": self.toggles or []}


def _rule(paths):
    return [{"rule_id": sup.GENERATED_RULE, "params": {"value": {"paths": paths}}}]


def _events(monkeypatch):
    seen = []
    monkeypatch.setattr(observe, "emit", lambda event, **f: seen.append((event, f)))
    return seen


def _held(paths, changed):
    return [f for f in changed if not sup.is_generated(f, paths)]


def test_unset_rule_allows_todays_files_and_logs(monkeypatch):
    seen = _events(monkeypatch)
    paths = sup.generated_paths(FakeClient([]))
    assert _held(paths, ["AGENTS.md", ".claude/agents/x.md"]) == []
    assert seen and seen[0][0] == "generated_files_rule_unread"


def test_configured_paths_replace_the_default(monkeypatch):
    _events(monkeypatch)
    paths = sup.generated_paths(FakeClient(_rule(["client/generated/**"])))
    assert _held(paths, ["client/generated/api.ts"]) == []
    assert _held(paths, ["AGENTS.md"]) == ["AGENTS.md"]


def test_unreadable_rule_uses_todays_list_and_says_why(monkeypatch):
    seen = _events(monkeypatch)
    paths = sup.generated_paths(FakeClient(exc=RuntimeError("down")))
    assert paths == sup.DEFAULT_GENERATED
    assert "down" in seen[0][1]["detail"]
