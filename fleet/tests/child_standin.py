"""A real fleet member, minus the model.

The acceptance walk needs a child that redeems a real seat against a real server,
reports its worktree and branch, claims real work and exits. What it does NOT need is a
vendor CLI burning tokens to prove that `register_agent` accepts an enrolment code.

So this is a genuine MCP client — same wire, same seat, same registration — with the
thinking removed. Everything it exercises is exactly what a vendor child would exercise;
the part it stands in for (argv construction, config placement, version pinning) is
already verified against real binaries in `test_adapters.py`.

Reads its config the way a vendor does: MCP server URL and key from the config file it
was handed, enrolment code from the instruction on stdin.
"""

from __future__ import annotations

import json
import re
import sys
import urllib.request
from pathlib import Path


def call(url: str, key: str, tool: str, **args) -> dict:
    body = json.dumps({
        "jsonrpc": "2.0", "id": 1, "method": "tools/call",
        "params": {"name": tool, "arguments": args},
    }).encode()
    request = urllib.request.Request(
        url, data=body, headers={"Content-Type": "application/json", "X-API-Key": key}
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        payload = json.load(response)
    result = payload.get("result") or {}
    if result.get("isError"):
        raise RuntimeError(f"{tool}: {result['content'][0]['text']}")
    if "error" in payload:
        raise RuntimeError(f"{tool}: {payload['error']}")
    return result.get("structuredContent") or {}


def main() -> int:
    config = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
    server = config["mcpServers"]["graphban"]
    url = server["url"]
    key = server["headers"]["X-API-Key"]

    instruction = sys.stdin.read()
    code = re.search(r"enrolment_code='([^']+)'", instruction).group(1)
    worktree = re.search(r"worktree='([^']+)'", instruction).group(1)
    branch = re.search(r"branch='([^']+)'", instruction).group(1)

    # PRD-22 D-b: a spawned child is a separate PROCESS, not a subagent, so it declares
    # NO parentage. The instruction says so in words and this is the half that matters.
    me = call(
        url, key, "register_agent",
        label=f"standin @ {branch}", enrolment_code=code,
        worktree=worktree, branch=branch,
        capabilities={"vendor": "standin", "host": "walk"},
    )
    print(json.dumps({"agent_id": me["agent_id"], "role": me.get("active_role")}), flush=True)

    if "--claim" in sys.argv:
        claimed = call(url, key, "claim_next", agent_id=me["agent_id"], wait_seconds=0)
        item = claimed.get("item") or {}
        # D-c: work what you got, then EXIT on empty. Exiting is the normal end of a
        # worker's life, not a failure.
        if item:
            Path(worktree, "worked.py").write_text("print('did the thing')\n", encoding="utf-8")
            call(
                url, key, "update_item", id=item["id"], status="review",
                agent_id=me["agent_id"],
                evidence=[{"kind": "note", "detail": "built by the acceptance walk standin"}],
            )
        print(json.dumps({"claimed": item.get("id")}), flush=True)

    if "--linger" in sys.argv:
        import time

        time.sleep(300)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
