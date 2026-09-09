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
