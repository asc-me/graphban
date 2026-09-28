"""What a fleet child actually loads, per harness, and where those lists differ.

A Harness-page rate compares vendors as if they had been handed the same tools. They
have not. A grok child inherits the operator's skills and MCP servers; a claude child
is launched with `--strict-mcp-config` and sees the seat; gbagent has no skill loader
at all. A rate that moves after a skill was added on one machine is not a measurement
of the model, and nothing on the page could say so until somebody recorded the lists.

**Names only.** An MCP stanza's URL, headers and command are credentials. This module
copies `name` and a source *type* (`user`, `project`, `claudeJson`) and drops
everything else, including the path, which has the operator's home directory in it.

**A list we do not have is not an empty list.** `unknown` means the binary was absent
or the vendor's own report failed. `partial` means a directory scan, which can confirm
a skill is present and cannot confirm one is absent. Only `checked` — the vendor's own
report, or a fact of the adapter that launches the child — may put a harness on the
"does not have it" side of a discrepancy.
"""
from __future__ import annotations

import json
import socket
import subprocess
from pathlib import Path

CHECKED = "checked"
PARTIAL = "partial"
UNKNOWN = "unknown"

#: The seat writes this server into every child. It is the one name they share on purpose.
SEAT_SERVER = "graphban"

#: Past this, a checked list is no longer complete, so it is reported as partial: a name
#: we dropped must not become evidence that the child lacks it.
SKILL_CAP = 400
MCP_CAP = 80

#: Vendors the fleet can spawn, in the order a report should read. Codex is absent on
#: purpose — there is no adapter, and a guessed skill layout would be a fabricated one.
VENDORS = ("grok", "claude", "cursor-agent", "qwen-code", "gbagent")

_BINARIES = {
    "grok": "grok",
    "claude": "claude",
    "cursor-agent": "cursor-agent",
    "qwen-code": "qwen",
    "gbagent": "gbagent",
}

_SEAT_ONLY = (
    "The child is confined to the seat"
    f" (`{SEAT_SERVER}`) and any server a wave names explicitly."
    " Those grants are per wave and are not part of this check."
    " The operator's other MCP servers are not inherited."
)


def host_name() -> str:
    raw = socket.gethostname().strip().replace("\n", "").replace("\r", "")
    return (raw or "unknown-host")[:128]


def scan(repo: Path, *, which=None, inspect_grok=None) -> dict:
    """One inventory. `which` and `inspect_grok` are seams for tests; production uses PATH
    and `grok inspect --json`, which is the report a grok child loads from."""
    import shutil

    find = which or shutil.which
    repo = Path(repo)
    return {
        "harnesses": [
            _one(vendor, repo, find, inspect_grok or grok_inspect) for vendor in VENDORS
        ]
    }


def discrepancies(harnesses: list[dict]) -> dict:
    """Where the lists differ. Kept in step with `backend/app/services/harness_surface.py`.

    A name is a discrepancy when at least one harness positively has it (a checked list,
    or a partial scan that found it) and at least one *checked* harness does not. A
    partial scan that failed to find a name is not the second half of that sentence.
    """
    return {
        "skills": _gaps(harnesses, "skills"),
        "mcps": _gaps(harnesses, "mcps"),
        "notes": _notes(harnesses),
    }


def summary(inventory: dict) -> str:
    """One line for `gbfleet doctor`. Zero rows is said as a sentence, never as a count of
    zero that a reader can take for 'they match' when only one list was complete."""
    gaps = discrepancies(inventory["harnesses"])
    parts = []
    for kind, label in (("skills", "skill"), ("mcps", "mcp")):
        block = gaps[kind]
        if block["rows"]:
            sample = ", ".join(row["name"] for row in block["rows"][:6])
            extra = len(block["rows"]) - min(len(block["rows"]), 6)
            more = f" (+{extra})" if extra else ""
            parts.append(f"{len(block['rows'])} {label} discrepancies ({sample}{more})")
        elif block["reason"]:
            parts.append(f"{label}: {block['reason']}")
        else:
            parts.append(f"no {label} discrepancy among {', '.join(block['compared'])}")
    if gaps["notes"]:
        brief = ", ".join(
            f"{note['vendor']} {note['kind']} {note['status']}" for note in gaps["notes"][:6]
        )
        parts.append(f"not fully checked: {brief}")
    return "; ".join(parts)


def render(inventory: dict) -> str:
    """The `gbfleet surface` report. Groups, not one line per skill: a gbagent child with
    no loader would otherwise print every grok skill as its own paragraph."""
    gaps = discrepancies(inventory["harnesses"])
    lines = ["harness surface", ""]
    for kind, title in (("skills", "Skills"), ("mcps", "MCP servers")):
        block = gaps[kind]
        lines.append(title)
        if block["reason"] and not block["rows"]:
            lines.append(f"  {block['reason']}")
        elif not block["rows"]:
            lines.append(f"  no difference among {', '.join(block['compared'])}")
        else:
            for group in _group(block["rows"]):
                lines.append(f"  {_group_label(group)} ({len(group['names'])})")
                shown = group["names"][:12]
                for name in shown:
                    lines.append(f"    {name}")
                if len(group["names"]) > len(shown):
                    lines.append(f"    +{len(group['names']) - len(shown)} more")
        lines.append("")
    if gaps["notes"]:
        lines.append("Not a complete list")
        for note in gaps["notes"]:
            lines.append(f"  {note['vendor']} {note['kind']}: {note['status']} — {note['reason']}")
    return "\n".join(lines).rstrip() + "\n"


def grok_inspect(repo: Path) -> dict:
    """`grok inspect --json` from the repo. The child runs with `--cwd` on a worktree of
    this repo and `--trust`, so this is the list it loads, plus the seat added below."""
    proc = subprocess.run(
        ["grok", "inspect", "--json"],
        cwd=str(repo),
        capture_output=True,
        text=True,
        stdin=subprocess.DEVNULL,
        timeout=20,
    )
    if proc.returncode != 0:
        raise RuntimeError(_safe_error(proc.stderr or proc.stdout or f"exit {proc.returncode}"))
    raw = proc.stdout or ""
    start = raw.find("{")
    if start < 0:
        raise RuntimeError("grok inspect --json returned no JSON object")
    try:
        data = json.loads(raw[start:])
    except json.JSONDecodeError as exc:
        raise RuntimeError("grok inspect --json was not JSON") from exc
    if not isinstance(data, dict):
        raise RuntimeError("grok inspect --json was not an object")
    return data


def _one(vendor: str, repo: Path, find, inspect) -> dict:
    binary = _BINARIES[vendor]
    row = _blank(vendor)
    if find(binary) is None:
        reason = f"{binary} is not on PATH, so what a child would load was not checked"
        row["skills_reason"] = reason
        row["mcps_reason"] = reason
        return row
    row["installed"] = True
    if vendor == "grok":
        return _grok(row, repo, inspect)
    if vendor == "gbagent":
        row["skills_status"] = CHECKED
        row["skills_reason"] = "gbagent has no skill loader, so a child sees no skills"
        _seat_only(row, "gbagent launches with the seat file and no other MCP server")
        return row
    if vendor == "claude":
        _partial_skills(row, repo, ".claude")
        _seat_only(row, "a claude child is launched with --strict-mcp-config. " + _SEAT_ONLY)
        return row
    if vendor == "qwen-code":
        row["skills_status"] = UNKNOWN
        row["skills_reason"] = (
            "no skill listing was measured for qwen. That is not the same as a child with no skills"
        )
        _seat_only(
            row,
            "a qwen child is launched with --allowed-mcp-server-names limited to the seat. "
            + _SEAT_ONLY,
        )
        return row
    # cursor-agent. The seat replaces <worktree>/.cursor/mcp.json. Whether the binary also
    # merges ~/.cursor/mcp.json was not measured, so the user servers are not listed: listing
    # them would claim the child holds them.
    _partial_skills(row, repo, ".cursor")
    row["mcps_status"] = PARTIAL
    row["mcps"] = [{"name": SEAT_SERVER, "source": "seat"}]
    row["mcps_reason"] = (
        "the supervisor writes the seat into the worktree's .cursor/mcp.json. "
        "Whether cursor-agent also merges ~/.cursor/mcp.json was not measured, "
        "so a server missing from this list is not proof the child lacks it"
    )
    return row


def _blank(vendor: str) -> dict:
    return {
        "vendor": vendor,
        "installed": False,
        "skills_status": UNKNOWN,
        "skills_reason": "",
        "skills": [],
        "disabled": [],
        "mcps_status": UNKNOWN,
        "mcps_reason": "",
        "mcps": [],
    }


def _seat_only(row: dict, reason: str) -> None:
    row["mcps_status"] = CHECKED
    row["mcps"] = [{"name": SEAT_SERVER, "source": "seat"}]
    row["mcps_reason"] = reason


def _partial_skills(row: dict, repo: Path, dirname: str) -> None:
    home = Path.home() / dirname / "skills"
    project = repo / dirname / "skills"
    items, unreadable, truncated = _dir_skills([(home, "user"), (project, "project")])
    row["skills_status"] = PARTIAL
    row["skills"] = items
    reason = (
        f"read from {dirname}/skills only. Plugin skills and disable flags were not read, "
        "so a name missing from this list is not proof the child lacks it"
    )
    if truncated:
        reason += f". Stopped at {SKILL_CAP} skills; names past the cap were not compared"
    if unreadable:
        reason += ". Could not read: " + "; ".join(unreadable[:3])
    row["skills_reason"] = reason


def _grok(row: dict, repo: Path, inspect) -> dict:
    try:
        data = inspect(repo)
    except (OSError, subprocess.TimeoutExpired, RuntimeError) as exc:
        reason = _safe_error(str(exc) or type(exc).__name__)
        row["skills_reason"] = f"grok inspect failed: {reason}"
        row["mcps_reason"] = row["skills_reason"]
        return row
    if not isinstance(data, dict):
        row["skills_reason"] = "grok inspect returned no object"
        row["mcps_reason"] = row["skills_reason"]
        return row
    if "skills" not in data:
        row["skills_status"] = UNKNOWN
        row["skills_reason"] = "grok inspect returned no skills field, so the list was not checked"
    else:
        skills, disabled = [], []
        for entry in data.get("skills") or []:
            item = _from_inspect(entry)
            if item is None:
                continue
            if isinstance(entry, dict) and entry.get("compatibilityStatus") == "disabled":
                disabled.append(item)
            else:
                skills.append(item)
        row["skills"] = _dedupe(skills)[:SKILL_CAP]
        row["disabled"] = _dedupe(disabled)[:SKILL_CAP]
        row["skills_status"] = CHECKED
        row["skills_reason"] = "grok inspect --json, which is what a grok child loads in this repo"
        if len(skills) > SKILL_CAP or len(disabled) > SKILL_CAP:
            row["skills_status"] = PARTIAL
            row["skills_reason"] += (
                f". Stopped at {SKILL_CAP}; names past the cap are not evidence of absence"
            )
    if "mcpServers" not in data:
        row["mcps_status"] = UNKNOWN
        row["mcps_reason"] = "grok inspect returned no mcpServers field, so the list was not checked"
    else:
        mcps = []
        for entry in data.get("mcpServers") or []:
            item = _from_inspect(entry)
            if item is not None:
                mcps.append(item)
        if not any(item["name"] == SEAT_SERVER for item in mcps):
            mcps.append({"name": SEAT_SERVER, "source": "seat"})
        row["mcps"] = _dedupe(mcps)[:MCP_CAP]
        row["mcps_status"] = CHECKED if len(mcps) <= MCP_CAP else PARTIAL
        row["mcps_reason"] = (
            "grok inspect --json, plus the seat's graphban server a child always receives"
        )
    return row


def _from_inspect(entry) -> dict | None:
    """Name and source type. The path and the target stay in the process that read them."""
    if not isinstance(entry, dict):
        return None
    name = entry.get("name")
    if not isinstance(name, str) or not name.strip() or any(c in name for c in "\n\r"):
        return None
    return {"name": name.strip()[:80], "source": _source_type(entry.get("source"))}


def _source_type(source) -> str:
    if isinstance(source, dict):
        text = source.get("type")
        if isinstance(text, str) and text.strip():
            return text.strip()[:32]
    if isinstance(source, str) and source.strip():
        return source.strip()[:32]
    return "unreported"


def _dir_skills(roots: list[tuple[Path, str]]) -> tuple[list[dict], list[str], bool]:
    items: list[dict] = []
    unreadable: list[str] = []
    truncated = False
    for root, source in roots:
        if not root.exists():
            continue
        try:
            paths = sorted(root.rglob("SKILL.md"))
        except OSError as exc:
            unreadable.append(f"{root.name}: {exc.__class__.__name__}")
            continue
        for path in paths:
            if len(items) >= SKILL_CAP:
                truncated = True
                break
            try:
                name = _skill_name(path)
            except OSError:
                unreadable.append(path.parent.name)
                continue
            if name:
                items.append({"name": name[:80], "source": source})
    return _dedupe(items), unreadable, truncated


def _skill_name(path: Path) -> str:
    text = path.read_text(encoding="utf-8", errors="replace")[:2000]
    if text.startswith("---"):
        end = text.find("\n---", 3)
        block = text[3:end] if end != -1 else ""
        for line in block.splitlines():
            if line.startswith("name:"):
                value = line.split(":", 1)[1].strip().strip("\"'")
                if value:
                    return value
    return path.parent.name


def _dedupe(items: list[dict]) -> list[dict]:
    seen: set[str] = set()
    out = []
    for item in items:
        if item["name"] in seen:
            continue
        seen.add(item["name"])
        out.append(item)
    return out


def _safe_error(text: str) -> str:
    lowered = text.lower()
    if any(word in lowered for word in ("token", "api_key", "api-key", "bearer", "secret", "password")):
        return "the command failed"
    line = text.strip().splitlines()[-1] if text.strip() else "the command failed"
    return line[:200]


def _names(harness: dict, field: str) -> set[str]:
    return {item["name"] for item in harness.get(field) or [] if isinstance(item, dict) and item.get("name")}


def _gaps(harnesses: list[dict], field: str) -> dict:
    status_field = f"{field}_status"
    checked = [h for h in harnesses if h.get(status_field) == CHECKED]
    partial = [h for h in harnesses if h.get(status_field) == PARTIAL]
    compared = [h["vendor"] for h in checked]
    partial_vendors = [h["vendor"] for h in partial]
    if not checked:
        return {
            "compared": [],
            "partial": partial_vendors,
            "rows": [],
            "reason": "no harness was fully checked, so a missing name is not a discrepancy",
        }
    enabled = {h["vendor"]: _names(h, field) for h in checked}
    disabled = {h["vendor"]: _names(h, "disabled") for h in checked} if field == "skills" else {}
    partial_names = {h["vendor"]: _names(h, field) for h in partial}
    universe: set[str] = set()
    for names in enabled.values():
        universe |= names
    for names in disabled.values():
        universe |= names
    for names in partial_names.values():
        universe |= names
    rows = []
    for name in sorted(universe):
        present = [v for v in compared if name in enabled.get(v, set())]
        disabled_on = [v for v in compared if name in disabled.get(v, set()) and v not in present]
        absent = [v for v in compared if v not in present and v not in disabled_on]
        partial_present = [v for v, names in partial_names.items() if name in names]
        if not present and not partial_present:
            continue
        if not absent and not disabled_on:
            continue
        rows.append({
            "name": name,
            "present": present,
            "absent": absent,
            "disabled_on": disabled_on,
            "partial_present": partial_present,
        })
    reason = ""
    if len(checked) == 1 and not rows:
        reason = (
            f"only {compared[0]} has a complete list, so there is no second list to compare it with"
        )
    return {"compared": compared, "partial": partial_vendors, "rows": rows, "reason": reason}


def _notes(harnesses: list[dict]) -> list[dict]:
    notes = []
    for harness in harnesses:
        for kind in ("skills", "mcps"):
            status = harness.get(f"{kind}_status")
            if status == CHECKED:
                continue
            notes.append({
                "vendor": harness["vendor"],
                "kind": kind,
                "status": status or UNKNOWN,
                "reason": harness.get(f"{kind}_reason") or "",
            })
    return notes


def _group(rows: list[dict]) -> list[dict]:
    groups: dict[tuple, dict] = {}
    for row in rows:
        key = (
            tuple(row["present"]), tuple(row["absent"]), tuple(row["disabled_on"]),
            tuple(row.get("partial_present") or []),
        )
        group = groups.get(key)
        if group is None:
            group = {
                "present": list(row["present"]),
                "absent": list(row["absent"]),
                "disabled_on": list(row["disabled_on"]),
                "partial_present": list(row.get("partial_present") or []),
                "names": [],
            }
            groups[key] = group
        group["names"].append(row["name"])
    return list(groups.values())


def _group_label(group: dict) -> str:
    parts = []
    if group["present"]:
        parts.append("on " + ", ".join(group["present"]))
    elif group.get("partial_present"):
        parts.append("seen by " + ", ".join(group["partial_present"]))
    if group["disabled_on"]:
        parts.append("disabled on " + ", ".join(group["disabled_on"]))
    if group["absent"]:
        parts.append("absent from " + ", ".join(group["absent"]))
    return "; ".join(parts) if parts else "differs"
