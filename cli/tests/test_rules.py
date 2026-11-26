"""GRPH-1952 / P92 D4: `gban rules import --prose` opens a run for the customer's agent."""
from __future__ import annotations

import subprocess

import pytest
from gban import cli as cli_mod
from gban import config, rules
from gban.cli import main
from gban.client import EXIT_REFUSED


def _repo(tmp_path, files):
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    for path, text in files.items():
        f = tmp_path / path
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(text)
    subprocess.run(["git", "-C", str(tmp_path), "add", "-A"], check=True)
    subprocess.run(["git", "-C", str(tmp_path), "-c", "user.email=t@t", "-c", "user.name=t",
                    "commit", "-qm", "x"], check=True)
    return tmp_path


@pytest.fixture()
def home(tmp_path, monkeypatch):
    monkeypatch.setenv(config.HOME_ENV, str(tmp_path / "home"))
    for var in (config.URL_ENV, config.PROJECT_ENV, config.API_KEY_ENV, "GBFLEET_API_KEY"):
        monkeypatch.delenv(var, raising=False)
    config.save_settings(url="http://gb.invalid", project="core")
    config.save_session("refresh-token", user="alex@example.com")


class Rec:
    def __init__(self):
        self.calls = []

    def call(self, method, path, body=None):
        self.calls.append((method, path, body))
        return {"id": 7, "commit": body["commit"], "sources_unread": body["sources_unread"],
                "clean": not body["sources_unread"]}


FILES = {"CONTRIBUTING.md": "run tests", "AGENTS.md": "a", "docs/adr/0001-db.md": "pg",
         "docs/architecture.md": "arch", "src/app.py": "x", "docs/guide.md": "not prose",
         ".github/copilot-instructions.md": "c"}


def test_prose_sources_are_listed_unread_for_the_agent(tmp_path):
    repo = _repo(tmp_path / "r", FILES)
    body = rules.run_body(repo)
    assert len(body["commit"]) == 40
    assert [s["path"] for s in body["sources_unread"]] == [
        ".github/copilot-instructions.md", "AGENTS.md", "CONTRIBUTING.md",
        "docs/adr/0001-db.md", "docs/architecture.md"]
    assert all(s["reader"] == "agent" for s in body["sources_unread"])
    assert "sources_read" not in body


def test_an_unreadable_repo_is_refused_never_an_empty_run(tmp_path, home, monkeypatch, capsys):
    """Sabotage: let `prose_sources` return [] when git fails, and this fails."""
    not_a_repo = tmp_path / "plain"
    not_a_repo.mkdir()
    with pytest.raises(rules.Unreadable):
        rules.prose_sources(not_a_repo, "HEAD")
    rec = Rec()
    monkeypatch.setattr(cli_mod, "authenticated", lambda url, act="": rec)
    code = main(["rules", "import", "--prose", "--repo", str(not_a_repo)])
    assert code == EXIT_REFUSED and rec.calls == []
    assert "no import run was opened" in capsys.readouterr().err


def test_prose_opens_one_run_and_prints_the_skill_invocation(tmp_path, home, monkeypatch, capsys):
    repo = _repo(tmp_path / "r", FILES)
    rec = Rec()
    monkeypatch.setattr(cli_mod, "authenticated", lambda url, act="": rec)
    assert main(["rules", "import", "--prose", "--repo", str(repo)]) == 0
    assert len(rec.calls) == 1
    method, path, _body = rec.calls[0]
    assert (method, path) == ("POST", "/api/projects/core/rules/import-runs")
    out = capsys.readouterr().out
    assert "/graphban-rules-import import_run=7 commit=" in out
    assert "unread  CONTRIBUTING.md" in out and "`propose_rule`" in out


def test_without_prose_nothing_is_opened(home, monkeypatch, capsys):
    rec = Rec()
    monkeypatch.setattr(cli_mod, "authenticated", lambda url, act="": rec)
    assert main(["rules", "import"]) == EXIT_REFUSED and rec.calls == []
