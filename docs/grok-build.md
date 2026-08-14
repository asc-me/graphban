# Using Graphban with Grok Build

[Grok Build](https://docs.x.ai/build) is xAI's agentic coding CLI (`grok`). It's an MCP
*client*, so it can drive Graphban's tools directly — the same operating loop Claude Code
and Cursor use. This guide connects it and primes it on the loop.

## Transport: a direct match (no bridge)

Graphban's MCP endpoint (`POST /api/mcp`) is **Streamable-HTTP compatible** — JSON-RPC 2.0,
single-JSON responses, `X-API-Key` auth (see [mcp.md](mcp.md)). Grok Build accepts
**Streamable HTTP or SSE** transports and, per xAI's docs, *not* stdio. The two match, so Grok
Build connects **straight to `/api/mcp`** — no `mcp-remote` stdio bridge.

> Graphban returns single-JSON responses (no SSE server-push); that's the request/response
> half of Streamable HTTP, which is all tool calls need — streamable-HTTP clients already
> connect this way (e.g. OpenClaw). Grok Build is 0.1 beta, so run `grok mcp doctor` (below) to
> confirm on your version.

## 1. Connect

Mint a key in **Settings → API Keys** (scoped to your project — the plaintext shows once).
Then, using the documented `grok mcp add`:

```bash
grok mcp add --transport http graphban https://<your-host>/api/mcp \
  --header "X-API-Key: gb_sk_…"
```

Self-host on your own machine: use `http://localhost:8000/api/mcp` (the default compose port;
`8001` if you remapped it). `grok mcp add` writes the entry to `~/.grok/config.toml`, which you
can also hand-edit.

**Team-wide, via a project file.** Grok Build also merges MCP config from a project `.mcp.json`
(and `.cursor/mcp.json`), so committing a template configures everyone who runs `grok` in the
repo. Never commit a real key — ship a `.mcp.json.example`:

```json
{
  "mcpServers": {
    "graphban": {
      "url": "https://<your-host>/api/mcp",
      "transport": "streamable-http",
      "headers": { "X-API-Key": "gb_sk_…" }
    }
  }
}
```

## 2. Verify

```bash
grok mcp list                 # graphban should be listed
grok mcp doctor graphban   # diagnoses config + connectivity
grok inspect                  # shows the MCP servers / skills / hooks Grok discovered
```

Then, in a `grok` session, the model should be able to call **`get_context`** — it returns the
key's project, scopes, and `readable_/writable_projects`. That round-trip is the handshake
working; a `401` means the header/key is wrong, a connectivity error means the URL isn't
reachable *from where `grok` runs*.

## 3. Prime it on the loop

Discovering the tools isn't the same as knowing the *loop*. Drop a `GROK.md` (or reuse the
repo's agent rules — Grok Build reads project instructions) that teaches the cycle, so a fresh
session doesn't rediscover it each time:

```markdown
# Working this repo via Graphban (MCP)

Call `get_context` FIRST — it names your project, scopes, and what you can read/write.

The loop:
1. `prd_coverage` / `get_backlog` / `suggest_next` — find what's specced-but-unbuilt or ready.
   `get_backlog` ranks ready-first and never hands out blocked work.
2. `claim_cluster` — atomically claim a non-colliding batch and reserve the files it touches,
   so two agents never take the same item or collide over one. `heartbeat(id)` keeps the lease.
3. Do the work in the repo.
4. `update_item(id, status="review")` — hand it on. Another agent calls `claim_review` and then
   `sign_off` (which auto-extracts lessons to memory) or `bounce(id, reason)`; nobody signs off
   their own work.
   `describe_code` as a byproduct so the code map stays fresh; `link_code` to bridge the item
   to the files it touched.
5. Back to coverage — the loop closes.

Rules of the road:
- Memory you write enters as a **candidate** (a human publishes it) — telemetry, not truth.
  Don't treat `search_memory` candidates as ground truth.
- Set **touchpoints** on items you create/update — the files/globs they affect. That's what
  powers collision-free clustering.
- Errors are typed: branch on `structuredContent.error.code`
  (`validation`/`not_found`/`conflict`/`unauthorized`/`internal`) and read the `hint` — don't
  parse prose. `unauthorized` won't fix on retry; `internal` is safe to retry once.
```

Because Grok Build also loads `.cursor/mcp.json` and project instructions, one set of agent
rules serves both editors — the planned Cursor rules (AL-147) apply to Grok Build unchanged.

## Related

- [mcp.md](mcp.md) — the full 30-tool reference + error taxonomy.
- [AL-201 spike](spikes/al-201-grok-build-worktrees.md) — running collision-free clusters on
  Grok Build's parallel worktrees, with touchpoint auto-capture.
