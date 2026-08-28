"""AL-214: the Cursor hooks are valid, wired to real handlers, and decide correctly.

Runs each handler as a subprocess with a sample event payload — the same contract
Cursor uses (event JSON on stdin, JSON decision on stdout).
"""
import json
import os
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
HOOKS_JSON = REPO / ".cursor" / "hooks.json"

# Cursor v3.11 agent-hook event names (docs: cursor.com/docs/agent/hooks).
KNOWN_EVENTS = {
    "sessionStart", "sessionEnd", "preToolUse", "postToolUse", "postToolUseFailure",
    "subagentStart", "subagentStop", "beforeShellExecution", "afterShellExecution",
    "beforeMCPExecution", "afterMCPExecution", "beforeReadFile", "afterFileEdit",
    "beforeSubmitPrompt", "preCompact", "stop", "afterAgentResponse", "afterAgentThought",
    "workspaceOpen",
}


def _run(script_rel, payload, env=None):
    environ = dict(os.environ)
    environ.update(env or {})
    proc = subprocess.run(
        [sys.executable, str(REPO / script_rel)],
        input=json.dumps(payload), capture_output=True, text=True, cwd=REPO, env=environ,
    )
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout or "{}")


def test_hooks_json_valid_and_wired():
    cfg = json.loads(HOOKS_JSON.read_text())
    assert cfg["version"] == 1
    assert cfg["hooks"], "no hooks configured"
    for event, entries in cfg["hooks"].items():
        assert event in KNOWN_EVENTS, f"unknown hook event {event!r}"
        for entry in entries:
            script = entry["command"].split()[-1]  # "python3 .cursor/hooks/x.py"
            assert (REPO / script).exists(), f"{script} referenced by {event} is missing"


def test_session_start_injects_the_loop_primer():
    out = _run(".cursor/hooks/gb_session_start.py",
               {"hook_event_name": "sessionStart", "workspace_roots": [str(REPO)]})
    assert "additional_context" in out
    assert "get_context" in out["additional_context"]


def test_after_file_edit_silent_in_cluster(tmp_path):
    manifest = tmp_path / "claim.json"
    manifest.write_text(json.dumps({"item_id": "AL-1", "touchpoints": ["backend/app/**"]}))
    out = _run(".cursor/hooks/gb_after_file_edit.py",
               {"file_path": str(REPO / "backend/app/x.py"), "workspace_roots": [str(REPO)]},
               env={"GRAPHBAN_CLAIM_FILE": str(manifest)})
    assert out == {}


def test_after_file_edit_warns_out_of_cluster(tmp_path):
    manifest = tmp_path / "claim.json"
    manifest.write_text(json.dumps({"item_id": "AL-1", "touchpoints": ["backend/app/**"]}))
    out = _run(".cursor/hooks/gb_after_file_edit.py",
               {"file_path": str(REPO / "web/src/App.tsx"), "workspace_roots": [str(REPO)]},
               env={"GRAPHBAN_CLAIM_FILE": str(manifest)})
    assert "user_message" in out and "AL-1" in out["user_message"]


def test_after_file_edit_silent_without_manifest(tmp_path):
    out = _run(".cursor/hooks/gb_after_file_edit.py",
               {"file_path": str(REPO / "web/src/App.tsx"), "workspace_roots": [str(REPO)]},
               env={"GRAPHBAN_CLAIM_FILE": str(tmp_path / "absent.json")})
    assert out == {}


def test_stop_nudges_only_on_completed():
    done = _run(".cursor/hooks/gb_stop.py", {"status": "completed", "loop_count": 0})
    assert "followup_message" in done
    aborted = _run(".cursor/hooks/gb_stop.py", {"status": "aborted", "loop_count": 0})
    assert aborted == {}


# ---- rename compatibility (AL-266) -------------------------------------------------
# The hooks read real environment configuration, so the rename gives it the same
# dual-accept treatment the CLI got in AL-262: prefer the new name, still honour the old
# one, and never move or delete a file the operator already has.
def test_the_old_claim_file_env_var_still_works(tmp_path):
    manifest = tmp_path / "claim.json"
    manifest.write_text(json.dumps({"item": "AL-1", "touchpoints": ["backend/**"]}))

    for var in ("GRAPHBAN_CLAIM_FILE", "AGENTLEDGER_CLAIM_FILE"):
        out = _run(
            ".cursor/hooks/gb_after_file_edit.py",
            {"file_path": str(REPO / "backend" / "app" / "main.py")},
            env={var: str(manifest)},
        )
        assert out is not None, f"{var} was ignored"


def test_the_old_claim_file_path_is_still_read(tmp_path, monkeypatch):
    """An operator mid-rename still has `.cursor/agentledger-claim.json` on disk."""
    from pathlib import Path as _P

    old = REPO / ".cursor" / "agentledger-claim.json"
    if old.exists():  # never clobber a real one
        return
    old.write_text(json.dumps({"item": "AL-1", "touchpoints": ["backend/**"]}))
    try:
        env = {k: v for k, v in os.environ.items()
               if k not in ("GRAPHBAN_CLAIM_FILE", "AGENTLEDGER_CLAIM_FILE")}
        proc = subprocess.run(
            [sys.executable, str(REPO / ".cursor/hooks/gb_after_file_edit.py")],
            input=json.dumps({"file_path": str(REPO / "backend" / "app" / "main.py")}),
            capture_output=True, text=True, cwd=REPO, env=env,
        )
        assert proc.returncode == 0, proc.stderr
    finally:
        _P(old).unlink()


def test_no_hook_or_agent_file_kept_the_old_prefix():
    """A rename that leaves `al_stop.py` behind while hooks.json points at `gb_stop.py`
    fails silently — Cursor just stops running the hook."""
    searched = (".cursor/hooks", ".cursor/agents", ".claude/agents", ".codex/agents")

    # THE CONTROL. `assert not stale` is satisfied by finding nothing, and finding nothing is
    # also what a renamed directory produces — measured, pointing these at paths that do not
    # exist left this file passing 9 tests. The directories being swept are exactly the ones a
    # toolchain rename moves, which is the event this test exists for.
    missing = [d for d in searched if not (REPO / d).is_dir()]
    assert not missing, (
        f"this sweep is looking in directories that do not exist: {missing} — it would report "
        "no stale files whether or not any were left behind")

    stale = sorted(
        p.name
        for d in searched
        for p in (REPO / d).glob("al[-_]*")
    )
    assert not stale, f"stale al-prefixed tooling left behind: {stale}"
