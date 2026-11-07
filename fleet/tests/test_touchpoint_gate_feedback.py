"""GRPH-1393: the touchpoint PR gate can be run by the child, while it can still declare."""
from __future__ import annotations

import subprocess
from pathlib import Path

from gbfleet import touchpoints as tp
from gbfleet.cli import gate_report, main


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=str(cwd), capture_output=True, text=True,
                          check=True).stdout


def test_gate_report_names_files_and_the_declaring_call():
    out = gate_report(tp.gate_missing(["a/x.py", "b/y.py"], ["a/x.py"]), item_id="GRPH-1")
    assert "b/y.py" in out and "a/x.py" not in out
    assert 'update_item(id="GRPH-1"' in out and "touchpoints=['b/y.py']" in out
    assert '"kind": "note"' in out


def test_gate_report_is_empty_when_covered_or_undeclared_or_generated():
    assert tp.gate_missing(["a/x.py"], ["a"]) == []
    assert tp.gate_missing(["a/x.py"], []) == []
    assert tp.gate_missing(["AGENTS.md"], ["a"], ignore=lambda f: f == "AGENTS.md") == []


def test_cli_check_touchpoints_exit_codes(git_repo: Path, capsys):
    (git_repo / "new.py").write_text("x = 1\n", encoding="utf-8")
    assert main(["check-touchpoints", "--path", str(git_repo), "--item", "GRPH-9",
                 "--declared", "other.py"]) == 1
    assert "new.py" in capsys.readouterr().out
    assert main(["check-touchpoints", "--path", str(git_repo), "--declared", "new.py"]) == 0
