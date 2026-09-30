"""gbagent's prompt carries the worktree's AGENTS.md, CLAUDE.md and project skills (GRPH-999).

Grok and Claude read those themselves; gbagent only sees what its prompt says. Worktree only —
never a home directory, never through a symlink that leaves — and one shared budget that says
when it cut something rather than dropping it silently.
"""
from __future__ import annotations

import os

from gbagent import cli


def _write(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def test_absence_is_normal_and_adds_nothing(tmp_path):
    assert cli.project_instructions(tmp_path) == ""
    assert cli.system_prompt(tmp_path) == cli.SYSTEM


def test_agents_claude_and_every_project_skill_dir_are_read(tmp_path):
    _write(tmp_path / "AGENTS.md", "run the one service layer")
    _write(tmp_path / "CLAUDE.md", "see AGENTS.md")
    _write(tmp_path / ".claude/skills/a/SKILL.md", "claude skill body")
    _write(tmp_path / ".grok/skills/b/SKILL.md", "grok skill body")
    _write(tmp_path / ".agents/skills/c/SKILL.md", "agents skill body")
    _write(tmp_path / ".claude/skills/a/notes.md", "not a skill file")

    prompt = cli.system_prompt(tmp_path)

    assert prompt.startswith(cli.SYSTEM)
    for text in ("run the one service layer", "see AGENTS.md", "claude skill body",
                 "grok skill body", "agents skill body"):
        assert text in prompt
    assert "not a skill file" not in prompt
    assert "--- .claude/skills/a/SKILL.md ---" in prompt
    assert prompt.index("run the one service layer") < prompt.index("claude skill body")


def test_the_operators_home_skills_are_never_read(tmp_path, monkeypatch):
    home = tmp_path / "home"
    _write(home / ".claude/skills/private/SKILL.md", "PERSONAL SKILL")
    _write(home / ".grok/skills/private/SKILL.md", "PERSONAL GROK SKILL")
    monkeypatch.setenv("HOME", str(home))
    worktree = tmp_path / "wt"
    worktree.mkdir()

    assert cli.project_instructions(worktree) == ""


def test_a_symlink_that_leaves_the_worktree_is_not_followed(tmp_path):
    outside = tmp_path / "outside"
    _write(outside / "AGENTS.md", "OUTSIDE AGENTS")
    _write(outside / "skills/x/SKILL.md", "OUTSIDE SKILL")
    _write(outside / "loose.md", "OUTSIDE FILE")
    wt = tmp_path / "wt"
    (wt / ".claude").mkdir(parents=True)
    (wt / "AGENTS.md").symlink_to(outside / "AGENTS.md")
    (wt / ".claude/skills").symlink_to(outside / "skills")
    (wt / ".grok/skills/y").mkdir(parents=True)
    (wt / ".grok/skills/y/SKILL.md").symlink_to(outside / "loose.md")
    (wt / ".agents/skills").mkdir(parents=True)
    os.symlink(outside / "skills", wt / ".agents/skills/linked")

    assert cli.project_instructions(wt) == ""


def test_a_link_that_stays_inside_is_read(tmp_path):
    _write(tmp_path / "docs/agents.md", "INSIDE TARGET")
    (tmp_path / "AGENTS.md").symlink_to(tmp_path / "docs/agents.md")
    assert "INSIDE TARGET" in cli.project_instructions(tmp_path)


def test_one_budget_and_a_cut_says_what_went_unread(tmp_path):
    _write(tmp_path / "AGENTS.md", "A" * 80)
    _write(tmp_path / ".claude/skills/s/SKILL.md", "S" * 50)
    _write(tmp_path / ".grok/skills/t/SKILL.md", "T" * 30)

    text = cli.project_instructions(tmp_path, cap=100)

    assert text.count("A") >= 80 and "A" * 81 not in text
    assert "S" * 20 in text and "S" * 21 not in text
    assert "30 of 50 characters left unread" in text
    assert "T" * 30 not in text
    assert "--- .grok/skills/t/SKILL.md ---" in text
    assert "30 characters unread" in text


def test_this_repository_is_truncated_not_dropped():
    """The real worktree: AGENTS.md alone is over the cap, so the cut is the common case."""
    root = cli.Path(__file__).resolve().parents[2]
    text = cli.project_instructions(root)
    assert "--- AGENTS.md ---" in text
    assert "left unread" in text
    assert "--- .claude/skills/" in text
