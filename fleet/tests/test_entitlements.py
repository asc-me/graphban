import json
import os
from pathlib import Path

import pytest

from gbfleet import entitlements as ent
from gbfleet.adapters import ADAPTERS, EntitlementUnsupported
from gbfleet import matrix as matrix_mod
from gbfleet.cli import main, with_foreign_variables_stripped
from gbfleet.spawn import Launch, inherited_environment


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


# --- where the file lives, and what it does not touch -------------------------------------

def _run_every_command():
    assert add("--name", "a", "--vendor", "claude", "--kind", "subscription", "--handle", "/a") == 0
    assert add("--name", "k", "--vendor", "claude", "--kind", "api_key", "--handle", "KEY_A") == 0
    assert main(["entitlement", "list"]) == 0
    assert main(["entitlement", "remove", "a"]) == 0


@pytest.fixture
def home_in_worktree(tmp_path, monkeypatch):
    """A HOME and a worktree side by side, with no path override: the real default applies."""
    home = tmp_path / "home"
    tree = tmp_path / "worktree"
    home.mkdir()
    tree.mkdir()
    # Path.home() reads HOME on POSIX and USERPROFILE on Windows; set both so the
    # default lands in this temp home on every runner, never the real one.
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.delenv(ent.PATH_ENV, raising=False)
    monkeypatch.chdir(tree)
    return home, tree


def test_file_is_outside_the_worktree(home_in_worktree):
    home, tree = home_in_worktree
    stored = ent.default_path()
    # Guard first: a default outside the temp home would write the real user's file.
    assert stored.is_relative_to(home)
    _run_every_command()
    assert stored.exists()
    assert stored.is_relative_to(home)
    assert not stored.resolve().is_relative_to(tree.resolve())
    # Nor under the repository these tests run from: salvage commits a worktree, not HOME.
    assert not stored.resolve().is_relative_to(Path(__file__).resolve().parents[2])
    assert list(tree.iterdir()) == []


def test_session_and_supervisor_files_are_not_created(home_in_worktree):
    home, _tree = home_in_worktree
    _run_every_command()
    assert not (home / ".graphban" / "session.json").exists()
    assert not (home / ".graphban" / "supervisor.json").exists()


def test_session_and_supervisor_files_are_not_modified(home_in_worktree):
    home, _tree = home_in_worktree
    gb = home / ".graphban"
    gb.mkdir()
    before = {}
    for name in ("session.json", "supervisor.json"):
        f = gb / name
        f.write_text(json.dumps({"keep": name}))
        os.utime(f, (1_000_000, 1_000_000))
        before[name] = (f.read_bytes(), f.stat().st_mtime_ns)
    _run_every_command()
    for name, (body, mtime) in before.items():
        f = gb / name
        assert (f.read_bytes(), f.stat().st_mtime_ns) == (body, mtime)


# --- two subscriptions on one vendor are two records and one matrix row ---------------------

ONE_CLAUDE_ROW = """
[[rows]]
harness = "claude"
model = "sonnet"
vendor = "anthropic"
lane = "any"
tier = "cheap"
status = "unverified"
order = 1
cost_class = "cheap"
local = false
evidence = []
"""


def test_two_subscriptions_same_vendor_two_records_one_matrix_row(path, tmp_path, capsys):
    toml = tmp_path / "matrix.toml"
    toml.write_text(ONE_CLAUDE_ROW)

    def claude_rows():
        return [r.key for r in matrix_mod.load(toml).rows if r.vendor == "anthropic"]

    assert claude_rows() == ["claude:sonnet"]
    add("--name", "work", "--vendor", "claude", "--kind", "subscription", "--handle", "/w")
    add("--name", "home", "--vendor", "claude", "--kind", "subscription", "--handle", "/h")
    capsys.readouterr()
    main(["entitlement", "list"])
    listed = capsys.readouterr().out.splitlines()
    assert [line.split("\t")[0] for line in listed] == ["work", "home"]
    # Two records, still one row for that vendor and model: records do not multiply rows.
    assert claude_rows() == ["claude:sonnet"]
    # And the shipped matrix is the same before and after the records exist.
    shipped = matrix_mod.load().rows
    main(["entitlement", "remove", "home"])
    assert matrix_mod.load().rows == shipped


# --- an api_key child gets its own variable, and not the other records' --------------------

def _records():
    return [
        ent.Entitlement("a", "claude", "api_key", "KEY_A"),
        ent.Entitlement("b", "claude", "api_key", "KEY_B"),
        ent.Entitlement("s", "claude", "subscription", "/s"),
    ]


def _child_env(selected, monkeypatch):
    monkeypatch.setenv("KEY_A", "value-a")
    monkeypatch.setenv("KEY_B", "value-b")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "ambient")
    monkeypatch.setenv("UNRELATED", "kept")
    launch = ADAPTERS["claude"].select_entitlement(_launch(), selected)
    launch = with_foreign_variables_stripped(launch, selected, _records())
    return inherited_environment(launch)


def test_api_key_child_does_not_receive_other_records_variables(monkeypatch):
    env = _child_env(_records()[1], monkeypatch)
    assert env["ANTHROPIC_API_KEY"] == "value-b"
    assert "KEY_A" not in env
    assert env["KEY_B"] == "value-b"
    assert env["UNRELATED"] == "kept"


def test_subscription_child_uses_its_directory_and_no_key(monkeypatch):
    env = _child_env(_records()[2], monkeypatch)
    assert env["CLAUDE_CONFIG_DIR"] == "/s"
    assert "ANTHROPIC_API_KEY" not in env
    assert "KEY_A" not in env and "KEY_B" not in env
    assert env["UNRELATED"] == "kept"


def test_shared_variable_name_is_not_stripped_from_its_own_launch():
    records = [ent.Entitlement("a", "claude", "api_key", "SHARED"),
               ent.Entitlement("b", "claude", "api_key", "SHARED")]
    assert ent.foreign_variables(records[1], records) == ()

