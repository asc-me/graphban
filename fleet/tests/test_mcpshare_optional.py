"""Optional (`name?`) entries in .gbfleet/servers (GRPH-1397)."""
from __future__ import annotations

import json
import logging

import pytest

from gbfleet import mcpshare


@pytest.fixture()
def setup(tmp_path):
    src = tmp_path / "claude.json"
    src.write_text(json.dumps({"mcpServers": {"context7": {"type": "http", "url": "u"}}}))
    repo = tmp_path / "repo"
    (repo / ".gbfleet").mkdir(parents=True)
    return repo, str(src)


def _write(repo, text):
    (repo / ".gbfleet" / "servers").write_text(text)


def test_missing_optional_server_is_skipped_and_logged(setup, caplog):
    repo, src = setup
    _write(repo, "context7\nbrowser?\n")
    with caplog.at_level(logging.WARNING, logger="gbfleet.mcpshare"):
        got = mcpshare.grants([], repo, src)
    assert list(got) == ["context7"]
    assert "browser" in caplog.text and "skipped" in caplog.text


def test_present_optional_server_is_shared(setup):
    repo, src = setup
    _write(repo, "context7?\n")
    assert list(mcpshare.grants([], repo, src)) == ["context7"]


def test_missing_required_server_still_refuses(setup):
    repo, src = setup
    _write(repo, "context7\nbrowser\n")
    with pytest.raises(mcpshare.ShareRefused):
        mcpshare.grants([], repo, src)


def test_reserved_optional_still_refuses(setup):
    repo, src = setup
    _write(repo, "graphban?\n")
    with pytest.raises(mcpshare.ShareRefused):
        mcpshare.grants([], repo, src)
