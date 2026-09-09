"""Share NAMED MCP servers with a child, on purpose (GRPH-816).

The companion to `--strict-mcp-config`. Children used to inherit every server on the
operator's machine — ten of them on a real wave, including that operator's Gmail, Drive and
Calendar (GRPH-802). Now they hold exactly what the supervisor writes into the seat file, and
that is what makes this safe to add: a server named here is a grant somebody typed, not
something a laptop happened to have.

**Exact names, never a pattern.** A glob is how the fixed bug comes back: `--mcp-server 'g*'`
on the wrong machine is Gmail. There is no syntax here for "and the rest".

**An unknown name is refused, not skipped.** Silently dropping a typo would hand the child a
wave's worth of work without the docs server it was supposed to have, and the operator would
read the empty result as the model being bad at the task.

**Sharing a server shares its credential.** A stanza's headers go into a file the child reads,
and a child can send what it reads anywhere. That is a fine trade for a docs server and a bad
one for anything holding write access, so the refusal message says it rather than assuming the
operator has thought it through.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

#: Where Claude Code keeps its servers. Named as a DEFAULT rather than hardcoded, because the
#: point of this module is that the operator says what is shared, and that includes from where.
DEFAULT_SOURCE = "~/.claude.json"

#: Never shareable, whatever the operator types. `graphban` is written by the seat itself with
#: the child's OWN credential; copying the operator's would hand a child a key with different
#: reach and make `independent()` meaningless — two agents, one credential.
RESERVED = frozenset({"graphban", "gbfleet"})


class ShareRefused(RuntimeError):
    """A named server could not be shared, and the wave should not start without it."""


def source_path(source: str = "") -> Path:
    return Path(os.path.expanduser(source or DEFAULT_SOURCE))


def available(source: str = "") -> dict[str, dict]:
    """Every server the source declares, global and per-project merged.

    Reading the whole file to pick one entry out of it is deliberate and is the reason
    `select` refuses patterns: the file is opened, but only exact names leave it.
    """
    path = source_path(source)
    try:
        blob = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ShareRefused(f"could not read {path}: {exc}") from exc
    if not isinstance(blob, dict):
        raise ShareRefused(f"{path} is not a JSON object")
    found: dict[str, dict] = {}
    for entry in (blob.get("mcpServers") or {}).items():
        found[entry[0]] = entry[1]
    for project in (blob.get("projects") or {}).values():
        if isinstance(project, dict):
            for name, server in (project.get("mcpServers") or {}).items():
                found.setdefault(name, server)
    return {k: v for k, v in found.items() if isinstance(v, dict)}


def select(names: list[str], source: str = "") -> dict[str, dict]:
    """The named servers, or a refusal naming the one that is missing.

    Deliberately does NOT list what was available. The file it just read is the one holding
    the operator's mail and calendar, and printing its contents into a wave log — or into a
    child's error output — would be a smaller version of the leak this whole area is about.
    """
    wanted = [n.strip() for n in names if n and n.strip()]
    if not wanted:
        return {}
    for name in wanted:
        if any(c in name for c in "*?["):
            raise ShareRefused(
                f"--mcp-server {name!r}: patterns are refused. Name each server exactly — a "
                "glob is how a child ends up holding servers nobody chose for it")
        if name in RESERVED:
            raise ShareRefused(
                f"--mcp-server {name!r}: reserved. The seat writes that one itself, with the "
                "child's own credential; sharing yours would give two agents one key")
    have = available(source)
    missing = [n for n in wanted if n not in have]
    if missing:
        raise ShareRefused(
            f"{', '.join(missing)} not in {source_path(source)}. Refusing rather than "
            "spawning without it: a child missing the server it was meant to have reads as a "
            "model that is bad at the task")
    return {n: have[n] for n in wanted}
