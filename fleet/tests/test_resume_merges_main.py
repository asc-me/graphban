"""GRPH-1440 — a resumed branch gets main merged in before the next child starts."""
from __future__ import annotations

import subprocess
from pathlib import Path

from gbfleet import worktree as wt
from gbfleet.seat import Seat, instruction_for
from gbfleet.supervisor import Wave, _record_resume_merge

_CFG = ["-c", "user.email=a@b.c", "-c", "user.name=t"]


def _git(repo: Path, *args: str) -> str:
    done = subprocess.run(["git", *args], cwd=repo, capture_output=True, text=True)
    assert done.returncode == 0, done.stderr
    return done.stdout.strip()


def _commit(repo: Path, name: str, text: str, msg: str) -> None:
    (repo / name).write_bytes(text.encode())
    _git(repo, "add", "-A")
    _git(repo, *_CFG, "commit", "-qm", msg)


def _prior(tmp_path: Path) -> Path:
    repo = tmp_path / "r"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "core.autocrlf", "false")
    _commit(repo, "AGENTS.md", "old\n", "base")
    _git(repo, "checkout", "-q", "-b", "gb/w-1")
    _commit(repo, "built", "work\n", "prior attempt")
    _git(repo, "checkout", "-q", "main")
    return repo


class _Client:
    def __init__(self):
        self.posts = []

    def post_observation(self, **payload):
        self.posts.append(payload)
        return {"ok": True}


def test_clean_resume_starts_on_a_head_with_mains_commits(tmp_path):
    """Sabotage: skip the merge and AGENTS.md stays `old`."""
    repo = _prior(tmp_path)
    _commit(repo, "AGENTS.md", "new\n", "main moved")
    tree = wt.resume_bounced(repo, tmp_path / "wt", "gb/w-1", base="main")
    assert (tree.path / "AGENTS.md").read_bytes().decode().replace("\r\n", "\n") == "new\n"
    assert (tree.path / "built").read_bytes().decode().replace("\r\n", "\n") == "work\n"
    assert not tree.behind_conflicts
    client, wave = _Client(), Wave()
    _record_resume_merge(wave, client, "GRPH-1", tree)
    assert "merged main@" in client.posts[0]["reason"]
    assert "before resuming" in client.posts[0]["reason"]


def test_conflicting_resume_is_not_merged_and_names_the_files(tmp_path):
    repo = _prior(tmp_path)
    _git(repo, "checkout", "-q", "gb/w-1")
    _commit(repo, "AGENTS.md", "branch\n", "branch edit")
    _git(repo, "checkout", "-q", "main")
    _commit(repo, "AGENTS.md", "main\n", "main edit")
    tree = wt.resume_bounced(repo, tmp_path / "wt", "gb/w-1", base="main")
    assert tree.behind_conflicts == ["AGENTS.md"]
    assert (tree.path / "AGENTS.md").read_bytes().decode().replace("\r\n", "\n") == "branch\n"
    assert _git(tree.path, "status", "--porcelain") == ""
    text = instruction_for(Seat(code="X", item="GRPH-1", server_url="u", api_key="k"), tree.path, tree.branch,
                           behind_ref=tree.behind_ref, conflicts=tree.behind_conflicts)
    assert "AGENTS.md" in text and "conflicts" in text
    client = _Client()
    _record_resume_merge(Wave(), client, "GRPH-1", tree)
    assert "AGENTS.md" in client.posts[0]["reason"]
