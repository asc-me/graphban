"""Getting a server-issued seat to a child, and taking it away again.

PRD-22 D-f. There is no single JSON here — it is each vendor's MCP config, and even
the mechanism for getting the path to the child differs. Claude Code takes
`--mcp-config <path>` and a private temp file works; Cursor has no per-invocation flag
and reads `.cursor/mcp.json` from the **project directory**, which for a child is its
own worktree.

**The file cannot be protected from the child, and does not need to be.** The child
runs as the same user and can edit anything it is given. It gains nothing: the seat is
single-use and already redeemed at `register_agent`, and the server decides the role
from the *seat*, not from the config on disk. Tampering breaks only the child's own
connection. The file is transport; the server-side enrolment is the artifact, and any
design leaning on the file's integrity is leaning on the wrong object.

What the supervisor is responsible for is narrower and worth stating: writing it 0600,
never inside the repo except where a vendor forces it, taking it away at reap, and
**never putting a parent agent id anywhere near it** (D-b).
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path

#: PRD-22 D-b. A spawned child is a separate PROCESS, not a subagent inside the
#: planner's turn, and `parent_agent_id` means the latter — `independent()` treats
#: siblings under one parent as one call tree, so declaring it would silently disable
#: review across the whole fleet.
#:
#: This is not a hypothetical mis-read. `register_agent`'s MCP schema describes the
#: field as "Set if you are a SUBAGENT: who spawned you", and a child launched by a
#: supervisor has an obvious and wrong answer to that question. So the supervisor never
#: writes it, and `INSTRUCTION` below tells the child not to either.
FORBIDDEN_KEYS = frozenset({"parent_agent_id", "parentAgentId", "parent"})

_FILE_MODE = 0o600


class WouldDeclareParentage(RuntimeError):
    """Refuse to hand a child anything that could make it claim a parent."""


@dataclass(frozen=True)
class Seat:
    """One server-issued enrolment, plus what the child needs to reach the server."""

    code: str
    server_url: str
    api_key: str

    def mcp_config(self, name: str = "graphban") -> dict:
        """The vendor-neutral core of every adapter's config file.

        Carries the CREDENTIAL, not the seat code: the code is an argument to
        `register_agent`, so it reaches the child as an instruction rather than as
        configuration. Both are secrets and both are bounded — the code is single-use
        with a 30-minute TTL.
        """
        return {
            "mcpServers": {
                name: {
                    "type": "http",
                    "url": self.server_url.rstrip("/") + "/api/mcp",
                    "headers": {"X-API-Key": self.api_key},
                }
            }
        }


#: What the child is told at startup. The negation is deliberate and is the supervisor's
#: half of D-b — weak on its own (a prompt is the weakest guard there is), which is why
#: GRPH-445 also narrows the tool description the child actually reads.
INSTRUCTION = (
    "Register with `register_agent` using enrolment_code={code!r}, worktree={worktree!r} "
    "and branch={branch!r}.\n"
    "You are a SEPARATE PROCESS, not a subagent. Do NOT set parent_agent_id — you have "
    "no parent. Declaring one would make you and your reviewer count as one call tree, "
    "and review across this fleet would stop meaning anything.\n"
    "Then claim work with wait_seconds=0 and EXIT when there is nothing to claim. "
    "Exiting on empty is the normal end of your run, not a failure."
)


def instruction_for(seat: Seat, worktree: Path, branch: str) -> str:
    return INSTRUCTION.format(code=seat.code, worktree=str(worktree), branch=branch)


def _reject_parentage(config: dict) -> None:
    """Refuse a config that mentions parentage anywhere in it, at any depth."""

    def walk(node: object, path: str) -> None:
        if isinstance(node, dict):
            for key, value in node.items():
                if key in FORBIDDEN_KEYS:
                    raise WouldDeclareParentage(
                        f"seat config would carry {path}{key!r}. PRD-22 D-b: a spawned "
                        "child is a separate process and has no parent; declaring one "
                        "makes it and its reviewer one call tree, and review across the "
                        "fleet silently stops meaning anything."
                    )
                walk(value, f"{path}{key}.")
        elif isinstance(node, list):
            for item in node:
                walk(item, path)

    walk(config, "")


def write(path: Path, config: dict) -> Path:
    """Write a child's seat config 0600, refusing anything that declares parentage.

    The mode is set explicitly after creation rather than relying on `open`'s: the
    umask masks it, and a file already present keeps whatever mode it had.
    """
    _reject_parentage(config)
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)

    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, _FILE_MODE)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        json.dump(config, handle, indent=2)
        handle.write("\n")
    os.chmod(path, _FILE_MODE)
    return path


def remove(path: Path) -> bool:
    """Take the seat file away. True if there was one."""
    path = Path(path)
    if not path.exists():
        return False
    path.unlink()
    return True
