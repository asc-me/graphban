#!/usr/bin/env python3
"""Claim-manifest writer — keeps `.cursor/graphban-claim.json` in step with the claim.

`gb_after_file_edit.py` warns when an edit is outside the touchpoints in that file, but the
server cannot write the checkout, so this local script does. It is the one writer; every
call site runs it:

* Cursor: `afterMCPExecution` in `.cursor/hooks.json` (event JSON on stdin).
* Grok: a `PostToolUse` matcher group in `.grok/hooks/` (same script; Grok maps
  `afterMCPExecution` to `PostToolUse`).
* gbagent: `Graphban.execute` calls `apply_tool_result` directly.

Rules: a `claim_next`/`claim_cluster` result with `claimed: true` replaces the file
(`item_id` = the seed, `touchpoints` = union over the result's items, `agent_id` =
`claimed_by` when present). `claimed: false` or an empty queue leaves the file alone. A
successful `release_item` removes it. Every other tool is ignored. Fail-open: never raises.
"""
import json
import os
import sys
from pathlib import Path

CLAIM_TOOLS = ("claim_next", "claim_cluster")
RELEASE_TOOL = "release_item"
DEFAULT_PATH = ".cursor/graphban-claim.json"


def manifest_path(root=None):
    env = os.environ.get("GRAPHBAN_CLAIM_FILE") or os.environ.get("AGENTLEDGER_CLAIM_FILE")
    if env:
        return Path(env)
    return Path(root or ".") / DEFAULT_PATH


def short_tool(name):
    """`mcp__graphban__claim_next` / `graphban:claim_next` / `claim_next` -> `claim_next`."""
    name = str(name or "")
    for sep in ("__", ":", "."):
        name = name.rsplit(sep, 1)[-1]
    return name


def _items(result):
    if isinstance(result.get("items"), list):
        return [i for i in result["items"] if isinstance(i, dict)]
    if isinstance(result.get("item"), dict):
        return [result["item"]]
    return []


def build_manifest(result):
    """The manifest for a claim result, or None when nothing was claimed."""
    if not isinstance(result, dict) or result.get("claimed") is not True:
        return None
    items = _items(result)
    if not items:
        return None
    touchpoints = []
    for item in items:
        for tp in item.get("touchpoints") or []:
            if tp not in touchpoints:
                touchpoints.append(tp)
    manifest = {"item_id": items[0].get("id"), "touchpoints": touchpoints}
    agent = items[0].get("claimed_by") or result.get("claimed_by")
    if agent:
        manifest["agent_id"] = agent
    return manifest


def _parse(value):
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except Exception:
            return None
    if isinstance(value, dict):
        # MCP envelope: {"structuredContent": {...}} or {"content": [{"text": "{...}"}]}
        if isinstance(value.get("structuredContent"), dict):
            return value["structuredContent"]
        content = value.get("content")
        if isinstance(content, list) and content and isinstance(content[0], dict) \
                and "claimed" not in value and "released" not in value:
            return _parse(content[0].get("text"))
        return value
    return None


def apply_tool_result(tool_name, result, root=None):
    """Write or clear the manifest for one finished tool call. Returns what happened."""
    try:
        tool = short_tool(tool_name)
        result = _parse(result)
        path = manifest_path(root)
        if tool in CLAIM_TOOLS:
            manifest = build_manifest(result)
            if manifest is None:
                return "unchanged"
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(manifest, indent=2) + "\n")
            return "written"
        if tool == RELEASE_TOOL:
            if not isinstance(result, dict) or result.get("error") or result.get("ok") is False \
                    or result.get("released") is False:
                return "unchanged"
            if path.exists():
                path.unlink()
                return "removed"
        return "unchanged"
    except Exception:
        return "unchanged"


def main():
    try:
        event = json.loads(sys.stdin.read() or "{}")
    except Exception:
        print("{}")
        return
    roots = event.get("workspace_roots") or []
    root = roots[0] if roots else event.get("cwd")
    result = event.get("result_json")
    if result is None:
        result = event.get("tool_response", event.get("tool_output"))
    apply_tool_result(event.get("tool_name"), result, root)
    print("{}")


if __name__ == "__main__":
    main()
