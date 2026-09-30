"""Sharing a named MCP server with a child, on purpose (GRPH-816).

The companion to `--strict-mcp-config` (GRPH-802), and only safe because of it. Children used
to inherit every server on the operator's machine — ten on a real wave, including that
operator's Gmail, Drive and Calendar. Now they hold exactly what the supervisor writes, so a
server named here is a grant somebody typed rather than something a laptop happened to have.

Everything below is about the ways an opt-in grant turns back into an implicit one.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from gbfleet import mcpshare
from gbfleet.seat import Seat


@pytest.fixture()
def config(tmp_path) -> Path:
    """A source file shaped like a real one: a global section, a per-project section, and
    servers nobody should be sharing."""
    path = tmp_path / "claude.json"
    path.write_text(json.dumps({
        "mcpServers": {
            "context7": {"type": "http", "url": "http://c7", "headers": {"k": "secret"}},
            "gmail": {"type": "http", "url": "http://mail"},
        },
        "projects": {"/some/repo": {"mcpServers": {"railway": {"url": "http://rw"}}}},
    }))
    return path


# ---- what may be shared -------------------------------------------------------------------

def test_an_exactly_named_server_is_shared(config):
    got = mcpshare.select(["context7"], str(config))

    assert list(got) == ["context7"]
    assert got["context7"]["url"] == "http://c7"


def test_a_per_project_server_is_reachable_by_name(config):
    assert list(mcpshare.select(["railway"], str(config))) == ["railway"]


def test_naming_nothing_shares_nothing(config):
    assert mcpshare.select([], str(config)) == {}


def test_only_what_was_named_comes_out(config):
    """The whole file is read to find one entry. The test that matters is that the rest of it
    does not leave."""
    got = mcpshare.select(["context7"], str(config))

    assert "gmail" not in got and "railway" not in got


# ---- what may not ---------------------------------------------------------------------------

def test_a_pattern_is_refused(config):
    """A glob is how the fixed bug comes back: `--mcp-server 'g*'` on the wrong machine is
    Gmail."""
    with pytest.raises(mcpshare.ShareRefused) as exc:
        mcpshare.select(["g*"], str(config))

    assert "patterns are refused" in str(exc.value)


def test_an_unknown_name_refuses_rather_than_skipping(config):
    """Silently dropping a typo hands the child a wave's work without the server it was
    meant to have, and the operator reads the empty result as a bad model."""
    with pytest.raises(mcpshare.ShareRefused) as exc:
        mcpshare.select(["contex7"], str(config))

    assert "contex7" in str(exc.value)


def test_the_refusal_does_not_print_what_was_available(config):
    """The file it just read holds the operator's mail. Listing its contents into a wave log
    would be a smaller version of the leak this area is about."""
    with pytest.raises(mcpshare.ShareRefused) as exc:
        mcpshare.select(["nope"], str(config))

    assert "gmail" not in str(exc.value)


@pytest.mark.parametrize("name", sorted(mcpshare.RESERVED))
def test_the_ledgers_own_name_cannot_be_shared(name, config):
    """Copying the operator's graphban credential in would give two agents one key and make
    `independent()` meaningless."""
    with pytest.raises(mcpshare.ShareRefused):
        mcpshare.select([name], str(config))


def test_an_unreadable_source_refuses(tmp_path):
    with pytest.raises(mcpshare.ShareRefused):
        mcpshare.select(["context7"], str(tmp_path / "absent.json"))


# ---- and what the child actually gets ----------------------------------------------------------

def _seat(**kw) -> Seat:
    return Seat(code="X", server_url="http://gb", api_key="k", role="worker", **kw)


def test_the_shared_server_reaches_the_seat_file():
    servers = _seat(shared={"context7": {"url": "http://c7"}}).mcp_config()["mcpServers"]

    assert set(servers) == {"graphban", "context7"}


def test_the_seats_own_entry_cannot_be_displaced():
    """Written last on purpose. A shared stanza called `graphban` would otherwise replace the
    child's credential with the operator's."""
    servers = _seat(shared={"graphban": {"url": "http://evil"}}).mcp_config()["mcpServers"]

    assert servers["graphban"]["url"] == "http://gb/api/mcp"


def test_sharing_nothing_is_the_default():
    """The safe default has to be the one you get by not thinking about it."""
    assert set(_seat().mcp_config()["mcpServers"]) == {"graphban"}


# ---- the allowlist and the config must agree ------------------------------------------------

def test_qwen_allows_the_servers_it_was_given(tmp_path):
    """qwen enforces its own allowlist. A shared server present in the config and absent from
    the flag is a docs server that exists and cannot be called — worse than not sharing it,
    because it looks configured."""
    import subprocess

    from gbfleet.adapters import ADAPTERS
    from gbfleet.worktree import Worktree

    tree = tmp_path / "wt"
    tree.mkdir()
    subprocess.run(["git", "init", "-q", str(tree)], capture_output=True)
    instruction = tmp_path / "instr"
    instruction.write_text("x")

    argv = ADAPTERS["qwen-code"].launch(
        _seat(shared={"context7": {}}), Worktree(path=tree, branch="b", repo=tree),
        instruction, Path("/usr/bin/true")).argv

    allowed = argv[argv.index("--allowed-mcp-server-names") + 1].split(",")
    assert "graphban" in allowed and "context7" in allowed


def test_claude_still_gets_strict_so_the_grant_is_the_whole_list(tmp_path):
    """THE PRECONDITION. Sharing is only a grant because the child holds nothing else — drop
    --strict-mcp-config and `--mcp-server context7` silently becomes "context7 and whatever
    this laptop has"."""
    import subprocess

    from gbfleet.adapters import ADAPTERS
    from gbfleet.worktree import Worktree

    tree = tmp_path / "wt2"
    tree.mkdir()
    subprocess.run(["git", "init", "-q", str(tree)], capture_output=True)
    instruction = tmp_path / "instr2"
    instruction.write_text("x")

    argv = ADAPTERS["claude"].launch(
        _seat(shared={"context7": {}}), Worktree(path=tree, branch="b", repo=tree),
        instruction, Path("/usr/bin/true")).argv

    assert "--strict-mcp-config" in argv


# ---- the project's own grant: .gbfleet/servers (GRPH-998) -----------------------------------

def _project(tmp_path, text: str | None) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir(exist_ok=True)
    if text is not None:
        (repo / ".gbfleet").mkdir(exist_ok=True)
        (repo / ".gbfleet" / "servers").write_text(text)
    return repo


def test_a_name_only_in_the_project_file_is_granted(tmp_path, config):
    repo = _project(tmp_path, "context7\n")

    assert list(mcpshare.grants([], repo, str(config))) == ["context7"]


def test_the_flag_unions_with_the_file(tmp_path, config):
    """Neither list displaces the other: the flag is for this invocation, the file for every
    wave."""
    repo = _project(tmp_path, "context7\n")

    assert set(mcpshare.grants(["railway"], repo, str(config))) == {"context7", "railway"}


def test_an_absent_file_grants_exactly_what_the_flag_did(tmp_path, config):
    """"This project listed no build servers" — never "share what the laptop has"."""
    repo = _project(tmp_path, None)

    assert list(mcpshare.grants(["context7"], repo, str(config))) == ["context7"]
    assert mcpshare.grants([], repo, str(config)) == {}


def test_a_file_that_names_nothing_grants_nothing(tmp_path, config):
    repo = _project(tmp_path, "# nothing yet\n\n   \n")

    assert mcpshare.grants([], repo, str(config)) == {}


def test_comments_blank_lines_and_inline_comments_are_ignored(tmp_path, config):
    repo = _project(tmp_path, "# docs\n\ncontext7  # the docs server\n  railway\n")

    assert mcpshare.project_names(repo) == ["context7", "railway"]


def test_a_file_name_missing_from_the_source_refuses_and_names_the_file(tmp_path, config):
    """A typo committed to the file must not tell the operator to fix a flag they never
    typed."""
    repo = _project(tmp_path, "contex7\n")

    with pytest.raises(mcpshare.ShareRefused) as exc:
        mcpshare.grants([], repo, str(config))

    msg = str(exc.value)
    assert "contex7" in msg and ".gbfleet/servers" in msg and "--mcp-server" not in msg
    assert "gmail" not in msg


def test_a_pattern_in_the_file_is_refused(tmp_path, config):
    repo = _project(tmp_path, "g*\n")

    with pytest.raises(mcpshare.ShareRefused) as exc:
        mcpshare.grants([], repo, str(config))

    assert "patterns are refused" in str(exc.value)


@pytest.mark.parametrize("name", sorted(mcpshare.RESERVED))
def test_a_reserved_name_in_the_file_is_refused(name, tmp_path, config):
    repo = _project(tmp_path, f"{name}\n")

    with pytest.raises(mcpshare.ShareRefused):
        mcpshare.grants([], repo, str(config))


def test_a_grant_file_that_exists_but_cannot_be_read_refuses(tmp_path, config):
    """Not the same as absent. A broken grant read as an empty one is a wave that runs without
    the servers the project said it needs."""
    repo = _project(tmp_path, None)
    (repo / ".gbfleet" / "servers").mkdir(parents=True)

    with pytest.raises(mcpshare.ShareRefused):
        mcpshare.grants([], repo, str(config))


def test_the_cli_reads_the_file_from_the_repo_root(tmp_path, config, monkeypatch):
    """THE CALL. A correct `grants` that nobody calls would pass every test above. And a
    `--repo` pointing at a subdirectory still finds the ROOT's file."""
    import argparse
    import subprocess

    from gbfleet import cli

    repo = _project(tmp_path, "context7\n")
    subprocess.run(["git", "init", "-q", str(repo)], capture_output=True, check=True)
    (repo / "backend").mkdir()
    monkeypatch.setattr(mcpshare, "DEFAULT_SOURCE", str(config))

    got = cli._shared_servers(argparse.Namespace(repo=str(repo / "backend"), mcp_server=["railway"]))

    assert set(got) == {"context7", "railway"}


def test_the_cli_refuses_the_wave_on_a_bad_file_name(tmp_path, config, monkeypatch, capsys):
    import argparse
    import subprocess

    from gbfleet import cli

    repo = _project(tmp_path, "contex7\n")
    subprocess.run(["git", "init", "-q", str(repo)], capture_output=True, check=True)
    monkeypatch.setattr(mcpshare, "DEFAULT_SOURCE", str(config))

    with pytest.raises(SystemExit) as exc:
        cli._shared_servers(argparse.Namespace(repo=str(repo), mcp_server=[]))

    assert exc.value.code == 2
    assert ".gbfleet/servers" in capsys.readouterr().err


def test_qwen_allows_a_server_named_only_in_the_project_file(tmp_path, config):
    """Present and not on qwen's allowlist is a server that exists and cannot be called."""
    import subprocess

    from gbfleet.adapters import ADAPTERS
    from gbfleet.worktree import Worktree

    repo = _project(tmp_path, "context7\n")
    tree = tmp_path / "wt3"
    tree.mkdir()
    subprocess.run(["git", "init", "-q", str(tree)], capture_output=True)
    instruction = tmp_path / "instr3"
    instruction.write_text("x")

    argv = ADAPTERS["qwen-code"].launch(
        _seat(shared=mcpshare.grants([], repo, str(config))),
        Worktree(path=tree, branch="b", repo=tree), instruction, Path("/usr/bin/true")).argv

    allowed = argv[argv.index("--allowed-mcp-server-names") + 1].split(",")
    assert "context7" in allowed


def test_this_repository_commits_its_build_servers():
    """The grant the ticket asked for. Tracked, not ignored — a `.gbfleet/` ignore rule would
    make every other clone's wave grant nothing while looking configured."""
    root = Path(__file__).resolve().parents[2]

    assert mcpshare.project_names(root) == ["context7", "browser", "browser-use"]
