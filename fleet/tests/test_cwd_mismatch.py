"""GRPH-928: gbfleet mcp refuses when --repo doesn't match the session's cwd.

A user-level MCP config (~/.claude.json) can shadow the repo's .mcp.json, causing
gbfleet mcp to start with --repo pointing at a different repository than the session's
cwd. The planner then sees fleet tools for the wrong project and believes they work.

The fix: `mcp` compares --repo's git root with cwd's git root and refuses to start
when they differ. `until` is an operator command, not a harness-spawned server, so
the shadowing rationale does not apply — it is intentionally not guarded.
"""
from __future__ import annotations

import argparse
from pathlib import Path
from unittest.mock import patch

from gbfleet import cli
from gbfleet.state import repo_root


def _make_mcp_args(repo: str | Path, **overrides) -> argparse.Namespace:
    defaults = dict(
        repo=str(repo),
        server="http://localhost:8080",
        project="test",
        workspace=None,
        tier=[],
        matrix="",
        max_workers=4,
        child_wall_clock=3600.0,
        mcp_server=[],
        allow=[],
        deny=[],
    )
    defaults.update(overrides)
    return argparse.Namespace(**defaults)


def test_mcp_refuses_when_repo_differs_from_cwd(
    git_repo: Path, other_repo: Path, monkeypatch, capsys,
):
    """THE FIX. --repo points at git_repo but cwd is other_repo: exit 2, named error."""
    monkeypatch.setenv(cli.API_KEY_ENV, "gb_sk_test_key")
    monkeypatch.chdir(other_repo)

    args = _make_mcp_args(git_repo)
    code = cli._serve_stdio(args)

    assert code == 2
    captured = capsys.readouterr()
    assert "does not match" in captured.err
    assert "user-level MCP config" in captured.err
    assert repo_root(git_repo).name in captured.err


def test_mcp_starts_when_repo_matches_cwd(
    git_repo: Path, monkeypatch, capsys,
):
    """The control: --repo matches cwd. The check passes; the process continues
    (and fails later on the server connection, which is fine — the cwd check passed)."""
    monkeypatch.setenv(cli.API_KEY_ENV, "gb_sk_test_key")
    monkeypatch.chdir(git_repo)

    args = _make_mcp_args(git_repo)
    # Patch `hold` to avoid actually serving — we just want to verify the cwd check passes
    with patch("gbfleet.cli.hold") as mock_hold:
        mock_hold.side_effect = RuntimeError("stop here")
        try:
            cli._serve_stdio(args)
        except RuntimeError as exc:
            assert "stop here" in str(exc)

    captured = capsys.readouterr()
    # The mismatch error must NOT appear
    assert "does not match" not in captured.err


def test_mcp_allows_non_repo_cwd(
    git_repo: Path, tmp_path: Path, monkeypatch, capsys,
):
    """When cwd is not inside a git repo, the check is skipped (cwd_root = None)."""
    monkeypatch.setenv(cli.API_KEY_ENV, "gb_sk_test_key")
    non_repo = tmp_path / "not_a_repo"
    non_repo.mkdir()
    monkeypatch.chdir(non_repo)

    args = _make_mcp_args(git_repo)
    with patch("gbfleet.cli.hold") as mock_hold:
        mock_hold.side_effect = RuntimeError("stop here")
        try:
            cli._serve_stdio(args)
        except RuntimeError as exc:
            assert "stop here" in str(exc)

    captured = capsys.readouterr()
    assert "does not match" not in captured.err
