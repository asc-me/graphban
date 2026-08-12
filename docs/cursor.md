# Using Graphban with Cursor

[Cursor](https://cursor.com) is an MCP *client* and — since Cursor 3 — a **fleet
orchestrator**: it runs many agents in parallel and can pin each to a different model.
Connecting it to Graphban lets those agents drive the tracker through the same
operating loop Claude Code and [Grok Build](grok-build.md) use — and lets the cheap
sub-agent fleet in [`.cursor/agents/`](../.cursor/agents/README.md) (AL-213) build with
real project context instead of a cold, clean context window.

## Transport: a direct match (no bridge)

Graphban's MCP endpoint (`POST /api/mcp`) is **Streamable-HTTP compatible** — JSON-RPC
2.0, single-JSON responses, `X-API-Key` auth (see [mcp.md](mcp.md)). Cursor's remote MCP
servers connect by `url` + `headers`, so Cursor talks **straight to `/api/mcp`** — no
`mcp-remote` stdio bridge.

## 1. Connect (user scope — works today)

Mint a key in **Settings → API Keys** (scoped to your project — the plaintext shows once).
Cursor reads two config files; pick by who the config is for:

- **Global, per-developer (recommended):** `~/.cursor/mcp.json`. Holds *your* real key,
  never committed.
- **Project, shared:** `.cursor/mcp.json` at the repo root. Handy for a team, but
  **never commit a real key** — commit [`.cursor/mcp.json.example`](../.cursor/mcp.json.example)
  and have each dev copy it to `.cursor/mcp.json` and drop their key in. `.cursor/mcp.json`
  is git-ignored for exactly this reason.

```json
{
  "mcpServers": {
    "graphban": {
      "url": "https://<your-host>/api/mcp",
      "headers": { "X-API-Key": "gb_sk_…" }
    }
  }
}
```

Self-host on your own machine: use `http://localhost:8000/api/mcp` (the default compose
port; `8001` if you remapped it). **Settings → API Keys** generates this exact snippet
ready-to-paste the moment you create a key.

> **Cheap models, lean manifest.** Mint a **read-only** key for research/scout agents: the
> scope-gated `tools/list` (AL-78) hands them only the read tools, so a non-frontier subagent
> isn't paying for write-tool schemas it can't call.

## 2. Verify

Cursor **Settings → MCP** should list `graphban` with its tools discovered. Then, in a
chat/agent, the model should be able to call **`get_context`** — it returns the key's project,
scopes, and `readable_/writable_projects`. That round-trip is the handshake working:

- a `401` means the header/key is wrong;
- a connectivity error means the URL isn't reachable **from where the agent runs** — note
  that Cursor **cloud** agents run remotely, so `localhost` won't reach your machine (that's
  what the Team-scope path below solves).

## 3. Prime it on the loop — the sub-agent fleet

Discovering the tools isn't the same as knowing the *loop*. This repo ships a generated
sub-agent fleet in [`.cursor/agents/`](../.cursor/agents/README.md) (AL-213):

- **`al-planner`** (frontier, `model: inherit`) — picks ready work and fans out only
  **non-colliding** clusters (`next_cluster`) so parallel workers don't touch the same files.
- **`al-implementer`** / **`al-frontend`** (cheap `composer-2`) — claim → load context →
  build → run the both-DB test loop → move to review.
- **`al-scout`** / **`al-verifier`** (cheap, read-only) — research and verification.

They're pre-primed on the claim → context → build → test → review cycle, so a fresh Cursor
session doesn't rediscover it. Regenerate after changing the roster or `AGENTS.md`:

```bash
python scripts/gen_subagents.py          # rewrites .cursor/.claude/.codex agents
python scripts/gen_subagents.py --check  # CI-safe staleness check
```

A thinner, always-on primer (a `.cursor/rules` file that points at `AGENTS.md`) is the
planned AL-147 — complementary to the fleet.

## 4. Team scope — fleet-wide distribution (Cursor 3 v3.11)

Cursor v3.11 added **Team MCP distribution**: a team admin registers a server **once** under
**Dashboard → Integrations & MCP**, and it's automatically available to **cloud agents, the
Agents Window, the IDE, and the CLI** — no per-developer setup, no config drift. This is the
only way **cloud/background** agents get Graphban, because they use the *team*-configured
servers rather than your local session. Admins can **Add to Team Marketplace** for
discoverability; Enterprise admins allowlist servers under **Team Settings → MCP Configuration**.

**Prerequisite (not yet shippable):** Team registration needs a **reachable, authenticated
Graphban URL** an admin can add — the org-scoped hosted endpoint (the local MCP proxy +
org sync credential) tracked as **PRD-11 §D5 / AL-216**, which itself depends on the
local-first hybrid (**AL-134**). Until that lands, use the **user-scope** path in §1 — it
works today for local agents. Server-side org stamping and the tenant-isolation sweeps
(AL-76/AL-95) must extend to the ingest path before this endpoint is exposed to a fleet.

## 5. Automate the loop with hooks (Cursor 3 v3.11)

MCP exposes the tools; **hooks** supply the *when*. This repo ships
[`.cursor/hooks.json`](../.cursor/hooks.json) (repo scope — any plan) with three stdlib-Python
handlers in [`.cursor/hooks/`](../.cursor/hooks/README.md):

- **`sessionStart`** injects the operating-loop primer into context (plus a live
  `get_context` snapshot when `GRAPHBAN_MCP_URL`/`GRAPHBAN_API_KEY` are set — the
  older `AGENTLEDGER_*` names are still honoured) — the
  biggest lever for a cheap model starting cold.
- **`afterFileEdit`** warns when an edit lands outside the claimed item's touchpoints
  (best-effort — the hook fires post-edit and can't block).
- **`stop`** nudges the agent to move its item to review and extract lessons (best-effort:
  `stop` may not fire in cloud agents on v3.11).

See [`.cursor/hooks/README.md`](../.cursor/hooks/README.md) for the I/O contract, the claim
manifest, and the honest limits of each hook.

## Related

- [mcp.md](mcp.md) — the full tool reference, client table, and error taxonomy.
- [`.cursor/agents/`](../.cursor/agents/README.md) — the generated sub-agent fleet (AL-213).
- [`.cursor/hooks/`](../.cursor/hooks/README.md) — the lifecycle hooks (AL-214).
- [grok-build.md](grok-build.md) — the sibling connect guide (Grok Build).
