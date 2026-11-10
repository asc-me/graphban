#!/usr/bin/env python3
"""Cursor `sessionStart` hook — inject the Graphban operating loop into context.

Cursor subagents start with a clean context window; this hook hands them the loop up
front so a cheap model doesn't rediscover it. Always injects the static primer; if
GRAPHBAN_MCP_URL + GRAPHBAN_API_KEY are set (AGENTLEDGER_* still honoured), it also pulls a live `get_context`
snapshot. Fail-open — any error still returns the primer.

I/O (Cursor hooks contract): reads the baseline event JSON on stdin, writes
`{"additional_context": "..."}` on stdout (injected into the session's system context).
"""
import json
import os
import sys
import urllib.request

PRIMER = """You are working this repo through Graphban (MCP). Follow the loop:
1. get_context — orient (your project, scopes, what you can read/write). Call it FIRST.
2. get_backlog / suggest_next / prd_coverage — find ready or specced-but-unbuilt work.
3. claim_next (or next_cluster for a code-neighborhood) — atomically claim; two agents
   never take the same item. heartbeat(id) holds the lease while you work.
4. Do the work. Keep edits inside the claimed item's touchpoints.
5. update_item(status="review"|"done") + extract_lessons — close the loop.
Set touchpoints on items you create/update — that is what keeps parallel agents from
colliding. Errors are typed: branch on structuredContent.error.code and read the hint."""

# When the hook already fetched the snapshot, step 1 must not send the model back for it.
FIRST_STEP = "1. get_context — orient (your project, scopes, what you can read/write). Call it FIRST."
FETCHED_STEP = ("1. get_context — already in this context (see Live get_context below); "
                "do not call it again this session.")


def live_context():
    # New names first, old ones still honoured — an exported AGENTLEDGER_* keeps working.
    url = os.environ.get("GRAPHBAN_MCP_URL") or os.environ.get("AGENTLEDGER_MCP_URL")
    key = os.environ.get("GRAPHBAN_API_KEY") or os.environ.get("AGENTLEDGER_API_KEY")
    if not url or not key:
        return None
    body = json.dumps(
        {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
         "params": {"name": "get_context", "arguments": {}}}
    ).encode()
    req = urllib.request.Request(
        url, data=body,
        headers={"Content-Type": "application/json", "X-API-Key": key},
    )
    with urllib.request.urlopen(req, timeout=5) as resp:  # noqa: S310 (configured URL)
        data = json.load(resp)
    sc = data.get("result", {}).get("structuredContent")
    return json.dumps(sc) if sc else None


def main():
    try:
        sys.stdin.read()  # baseline event payload; unused for the static primer
    except Exception:
        pass
    context = PRIMER
    try:
        live = live_context()
        if live:
            context = PRIMER.replace(FIRST_STEP, FETCHED_STEP)
            context += "\n\nLive get_context: " + live
    except Exception:
        pass  # fail-open: the primer alone is still useful
    print(json.dumps({"additional_context": context}))


if __name__ == "__main__":
    main()
