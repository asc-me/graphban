"""GRPH-1402: a claude child keeps the repo's skills and drops the operator's."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from gbfleet.adapters.claude import ClaudeCode
from gbfleet.seat import Seat


def _argv(tmp_path: Path) -> list[str]:
    import inspect
    adapter = ClaudeCode()
    params = inspect.signature(Seat).parameters
    seat = Seat(**{k: "x" for k, p in params.items() if p.default is p.empty})
    tree = SimpleNamespace(path=tmp_path)
    return adapter.launch(seat, tree, tmp_path / "i.txt", Path("claude")).argv


def test_user_settings_source_is_dropped_project_kept(tmp_path):
    argv = _argv(tmp_path)
    sources = argv[argv.index("--setting-sources") + 1].split(",")
    assert "user" not in sources
    assert {"project", "local"} <= set(sources)


def test_subscription_auth_and_skills_not_disabled(tmp_path):
    argv = _argv(tmp_path)
    assert "--bare" not in argv
    assert "--disable-slash-commands" not in argv
    assert argv[-1].startswith("Follow the instructions")
