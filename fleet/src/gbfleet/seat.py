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
import re
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


class UnrenderableSeat(RuntimeError):
    """The seat config cannot be expressed in the format this vendor reads."""


#: The two on-disk languages the vendors read. Not cosmetic: grok reads TOML and
#: nothing else, so writing it JSON produces a file it ignores in silence — which is
#: how a child ends up running with no tools and no error (GRPH-575).
JSON = "json"
TOML = "toml"


def _as_json(config: dict) -> str:
    return json.dumps(config, indent=2) + "\n"


#: Every key grok's TOML understands for one server, measured rather than assumed: on
#: 1.0.5, `grok mcp add --transport http graphban <url> --header "X-API-Key: ..."`
#: writes exactly `url`, `enabled` and a `headers` sub-table. There is no `type` or
#: `transport` key at all — grok infers HTTP from the presence of `url`.
_GROK_RENDERED = frozenset({"type", "url", "headers"})

_BARE_KEY = re.compile(r"^[A-Za-z0-9_-]+$")


def _toml_string(value: str) -> str:
    """A TOML basic string. Escaping the credential rather than trusting its alphabet:
    the api key and server url arrive from the server, and a `"` in either would
    otherwise emit a file that grok fails to parse — which reads, from the child's
    side, exactly like having no seat."""
    out = ['"']
    for ch in value:
        if ch == '"':
            out.append('\\"')
        elif ch == "\\":
            out.append("\\\\")
        elif ch == "\n":
            out.append("\\n")
        elif ch == "\r":
            out.append("\\r")
        elif ch == "\t":
            out.append("\\t")
        elif ch == "\b":
            out.append("\\b")
        elif ch == "\f":
            out.append("\\f")
        elif ord(ch) < 0x20 or ord(ch) == 0x7F:
            out.append(f"\\u{ord(ch):04X}")
        else:
            out.append(ch)
    out.append('"')
    return "".join(out)


def _toml_key(key: str) -> str:
    return key if _BARE_KEY.match(key) else _toml_string(key)


def _as_toml(config: dict) -> str:
    """`mcp_config`'s vendor-neutral dict in grok's TOML dialect.

    Note `mcp_servers`, not `mcpServers` — the case difference is the whole reason a
    hand-translated file would look right and load nothing.

    Unknown keys RAISE rather than being dropped. If `mcp_config` ever grows a field,
    the grok path has to fail loudly: silently omitting it would hand the child a seat
    that is missing the very thing that was added, and nothing downstream would say so.
    """
    servers = config.get("mcpServers") or {}
    blocks: list[str] = []
    for name, spec in servers.items():
        unknown = sorted(set(spec) - _GROK_RENDERED)
        if unknown:
            raise UnrenderableSeat(
                f"grok's config.toml has no place for {unknown} on server {name!r}. "
                "Dropping them would give the child a seat missing exactly what was "
                "added, so this refuses instead. Teach _as_toml the new field."
            )
        # `type` is carried by the neutral config but has no TOML spelling: grok reads
        # the transport off `url`. So it is CHECKED and then not emitted — a stdio seat
        # rendered this way would silently become an HTTP one.
        transport = spec.get("type", "http")
        if transport != "http":
            raise UnrenderableSeat(
                f"server {name!r} is transport {transport!r}, but grok infers transport "
                "from `url` and only HTTP can be expressed here. Rendering it anyway "
                "would silently turn it into an HTTP server."
            )
        table = f"mcp_servers.{_toml_key(name)}"
        lines = [f"[{table}]"]
        if "url" in spec:
            lines.append(f"url = {_toml_string(str(spec['url']))}")
        lines.append("enabled = true")
        headers = spec.get("headers") or {}
        if headers:
            lines.append("")
            lines.append(f"[{table}.headers]")
            for header, value in headers.items():
                lines.append(f"{_toml_key(header)} = {_toml_string(str(value))}")
        blocks.append("\n".join(lines))
    return "\n\n".join(blocks) + "\n"


_RENDERERS = {JSON: _as_json, TOML: _as_toml}


def write(path: Path, config: dict, fmt: str = JSON) -> Path:
    """Write a child's seat config 0600, refusing anything that declares parentage.

    The mode is set explicitly after creation rather than relying on `open`'s: the
    umask masks it, and a file already present keeps whatever mode it had.

    `fmt` picks the vendor's on-disk language. The parentage check runs FIRST and for
    every format — it is a property of the config, not of how it is spelled, and a
    guard that only covered JSON would be no guard at all the day a vendor needed TOML.
    """
    _reject_parentage(config)
    render = _RENDERERS.get(fmt)
    if render is None:
        raise UnrenderableSeat(
            f"no seat renderer for {fmt!r}; known formats are {sorted(_RENDERERS)}"
        )
    text = render(config)

    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)

    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, _FILE_MODE)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write(text)
    os.chmod(path, _FILE_MODE)
    return path


def remove(path: Path) -> bool:
    """Take the seat file away. True if there was one."""
    path = Path(path)
    if not path.exists():
        return False
    path.unlink()
    return True
