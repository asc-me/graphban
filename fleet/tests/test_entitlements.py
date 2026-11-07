import json
from pathlib import Path

import pytest

from gbfleet import entitlements as ent
from gbfleet.adapters import ADAPTERS, EntitlementUnsupported
from gbfleet.cli import main
from gbfleet.spawn import Launch


@pytest.fixture
def path(tmp_path, monkeypatch):
    p = tmp_path / "ents.json"
    monkeypatch.setenv(ent.PATH_ENV, str(p))
    return p


def add(*args):
    return main(["entitlement", "add", *args])


def test_subscription_stores_exactly_four_fields(path):
    assert add("--name", "a", "--vendor", "claude", "--kind", "subscription",
               "--handle", "/home/me/.claude-a") == 0
    assert json.loads(path.read_text()) == [
        {"name": "a", "vendor": "claude", "kind": "subscription", "handle": "/home/me/.claude-a"}]


def test_fifth_field_refused_by_name_file_unchanged(path, capsys):
    add("--name", "a", "--vendor", "claude", "--kind", "subscription", "--handle", "h")
    before = path.read_text()
    rc = add("--json", json.dumps({"name": "b", "vendor": "claude", "kind": "subscription",
                                   "handle": "h", "token": "x"}))
    assert rc == 2
    assert "'token'" in capsys.readouterr().err
    assert path.read_text() == before


def test_token_looking_handle_is_stored(path):
    assert add("--name", "a", "--vendor", "claude", "--kind", "subscription",
               "--handle", "sk-ant-abcdef123456") == 0
    assert ent.load(path)[0].handle == "sk-ant-abcdef123456"


def test_api_key_stores_name_not_value(path, monkeypatch):
    monkeypatch.setenv("MY_KEY", "supersecretvalue")
    add("--name", "k", "--vendor", "claude", "--kind", "api_key", "--handle", "MY_KEY")
    assert "supersecretvalue" not in path.read_text()
    assert ent.load(path)[0].handle == "MY_KEY"


def test_list_and_remove(path, capsys):
    add("--name", "a", "--vendor", "claude", "--kind", "subscription", "--handle", "/a")
    add("--name", "b", "--vendor", "claude", "--kind", "subscription", "--handle", "/b")
    capsys.readouterr()
    main(["entitlement", "list"])
    out = capsys.readouterr().out
    assert "/a" in out and "/b" in out
    assert main(["entitlement", "remove", "a"]) == 0
    assert [r.name for r in ent.load(path)] == ["b"]


def _launch():
    return Launch(adapter="claude", argv=["claude"], seat_path=Path("/x"), config={}, instruction="")


def test_second_record_selects_second_handle():
    adapter = ADAPTERS["claude"]
    first = ent.Entitlement("a", "claude", "subscription", "/a")
    second = ent.Entitlement("b", "claude", "subscription", "/b")
    assert adapter.select_entitlement(_launch(), first).env["CLAUDE_CONFIG_DIR"] == "/a"
    assert adapter.select_entitlement(_launch(), second).env["CLAUDE_CONFIG_DIR"] == "/b"


def test_adapter_without_selection_refuses():
    adapter = ADAPTERS["grok"]
    with pytest.raises(EntitlementUnsupported):
        adapter.select_entitlement(_launch(), ent.Entitlement("g", "grok", "subscription", "/g"))
