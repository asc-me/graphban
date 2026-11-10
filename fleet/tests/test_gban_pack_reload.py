"""GRPH-1493: after `gban` applies a pack, a NEW `gbagent` process reads the new skill body.

A process that was already running keeps what it loaded; that is outside this test.
"""
import subprocess
import sys
from pathlib import Path

CLI_SRC = Path(__file__).resolve().parents[2] / "cli" / "src"
sys.path.insert(0, str(CLI_SRC))
from gban import playbooks  # noqa: E402  (stdlib-only package, read from the checkout)

SKILL = ".claude/skills/graphban-work/SKILL.md"


class Fake:
    def __init__(self, sha, body):
        self.sha, self.body = sha, body

    def call(self, method, path, body=None):
        if path == "/health":
            return {"git_sha": self.sha}
        return {"git_sha": self.sha, "files": [
            {"path": SKILL, "kind": "skill",
             "body": f"---\nname: graphban-work\n---\n# Work\n{self.body}\n"}]}


def _child_prompt(root: Path) -> str:
    code = ("import sys; from pathlib import Path; from gbagent.cli import project_instructions;"
            "print(project_instructions(Path(sys.argv[1])))")
    return subprocess.run([sys.executable, "-c", code, str(root)], capture_output=True,
                          text=True, check=True).stdout


def test_a_new_gbagent_process_reads_the_applied_skill(tmp_path):
    client = Fake("1", "OLD-PROCEDURE")
    playbooks.poll("http://x", tmp_path, client=client, auth=lambda: client, now=0)
    assert "OLD-PROCEDURE" in _child_prompt(tmp_path)
    client = Fake("2", "NEW-PROCEDURE")
    r = playbooks.poll("http://x", tmp_path, client=client, auth=lambda: client, now=5000)
    assert SKILL in r["written"] and ".grok/skills/graphban-work/SKILL.md" in r["written"]
    prompt = _child_prompt(tmp_path)
    assert "NEW-PROCEDURE" in prompt and "OLD-PROCEDURE" not in prompt
