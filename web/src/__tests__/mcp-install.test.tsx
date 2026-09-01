import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it } from "vitest";

import { CLIENTS, McpInstall, type KeyScope } from "@/features/settings/McpInstall";

/**
 * The connect snippets are copied verbatim into a config file or a terminal, so a wrong one
 * fails at the worst moment — an agent that will not start, with a snippet the product handed
 * over and vouched for.
 *
 * These pin the SHAPE each tool documents, not the exact bytes: a schema change should fail
 * here, a reworded note should not.
 *
 * **A terminal command is the preferred form and the default — but it is not universal, and
 * that asymmetry is the thing to protect.** Cursor ships no MCP CLI, opencode's `mcp`
 * subcommands cover auth/list/logout only, and `codex mcp add` takes stdio servers
 * exclusively. None of the three can add a remote server with a header from a terminal, so
 * handing them one produces a command that does not exist — which is exactly the bug the
 * Fleet view shipped when it reused a single `claude mcp add` stub for every client.
 *
 * Note that *having* an `mcp add` is not the same as being able to declare this server with
 * it: Codex's takes stdio only, and `hermes mcp add` installs catalog entries. Both read like
 * a command exists until you try the one case we need.
 */
const CONFIG_ONLY = ["Cursor", "Codex", "opencode", "Hermes"];

async function open(client: string, form?: "Command" | "Config file", keyScope?: KeyScope) {
  const user = userEvent.setup();
  const view = render(<McpInstall apiKey="gb_sk_secret" keyScope={keyScope} />);
  await user.click(screen.getByRole("button", { name: client }));
  if (form) await user.click(screen.getByRole("button", { name: form }));
  return { ...view, user, snippet: () => view.container.querySelector("pre")?.textContent ?? "" };
}

async function snippetFor(client: string, form?: "Command" | "Config file", keyScope?: KeyScope) {
  const { snippet, unmount } = await open(client, form, keyScope);
  const text = snippet();
  unmount();
  return text;
}

describe("MCP install snippets", () => {
  it("defaults to the terminal command wherever the client has one", async () => {
    for (const [client, command] of [
      ["Claude Code", "claude mcp add"],
      ["Grok CLI", "grok mcp add"],
      ["OpenClaw", "openclaw mcp set"],
    ] as const) {
      expect(await snippetFor(client)).toContain(command);
    }
  });

  it("sets the harness scope flag from the key's scope", async () => {
    // A project key registers beside the repo; a global key in the user config. Both tools
    // name the values user|project even though they DEFAULT differently — grok defaults to
    // user, so `--scope project` is the flag earning its keep there. The `/graphban/` tail
    // pins placement: flags after the positionals could be swallowed by `<args...>`.
    for (const client of ["Claude Code", "Grok CLI"] as const) {
      expect(await snippetFor(client, "Command", "project")).toMatch(/--scope project\s+graphban/);
      expect(await snippetFor(client, "Command", "global")).toMatch(/--scope user\s+graphban/);
    }
  });

  it("invents no scope flag for a client whose support is unverified", async () => {
    // OpenClaw's `mcp set` has no documented --scope. It writes one user-level file either
    // way, so the honest snippet is the one without a flag that may not exist.
    expect(await snippetFor("OpenClaw", "Command", "global")).not.toMatch(/--scope/);
    expect(await snippetFor("OpenClaw", "Command", "project")).not.toMatch(/--scope/);
  });

  it("omits the flag when nobody passed a scope, and says what to add", async () => {
    const { snippet, unmount } = await open("Claude Code", "Command");
    expect(snippet()).not.toContain("--scope");
    expect(screen.getByText(/Add --scope project for one repo, --scope user/)).toBeInTheDocument();
    unmount();
  });

  it("the note names the flag the snippet already set", async () => {
    // The scope now arrives automatic; the note's job changed from instructing to explaining.
    const claude = await open("Claude Code", "Command", "global");
    expect(screen.getByText(/--scope user registers it in every project/)).toBeInTheDocument();
    claude.unmount();

    const grok = await open("Grok CLI", "Command", "project");
    expect(screen.getByText(/would otherwise default to user/)).toBeInTheDocument();
    grok.unmount();
  });

  it("never offers a command to a client that has none", async () => {
    // THE regression. A command form for these would be a command that does not exist, and
    // the person pasting it has no way to know that before their shell says so.
    for (const client of CONFIG_ONLY) {
      const { snippet, unmount } = await open(client);
      expect(snippet()).not.toMatch(/mcp add|mcp set/);
      expect(screen.queryByRole("button", { name: "Command" })).not.toBeInTheDocument();
      unmount();
    }
  });

  it("says why the command form is missing rather than just omitting it", async () => {
    // An absent tab with no explanation reads as something we forgot. Each of these is a fact
    // about the vendor, taken from their docs.
    const { unmount } = await open("Codex");
    expect(screen.getByText(/stdio servers only/)).toBeInTheDocument();
    unmount();

    const cursor = await open("Cursor");
    expect(screen.getByText(/no MCP CLI/)).toBeInTheDocument();
    cursor.unmount();

    const hermes = await open("Hermes");
    expect(screen.getByText(/catalog entries/)).toBeInTheDocument();
    hermes.unmount();
  });

  it("no client can be config-only without saying why", () => {
    // The invariant over the data, not over the clients that happen to exist today. Hermes
    // shipped for three weeks with a missing Command tab and no explanation, and the
    // per-client test above would not have caught the next one either.
    for (const c of CLIENTS) {
      expect(c.command ?? c.config, `${c.label} offers no way to connect at all`).toBeTruthy();
      if (!c.command) {
        expect(c.noCommand, `${c.label} has no command form and does not explain why`).toBeTruthy();
      }
    }
  });

  it("offers the config stanza as the other option where both exist", async () => {
    const snippet = await snippetFor("Grok CLI", "Config file");

    expect(snippet).toContain("[mcp_servers.graphban]");
    expect(snippet).toContain('headers = { "X-API-Key" = "gb_sk_secret" }');
    // The old bridge spawned an extra process and pointed at a path that does not exist.
    expect(snippet).not.toContain("mcp-remote");
    expect(snippet).not.toContain("npx");
    expect(screen.queryByText(/\.grok\/settings\.json/)).not.toBeInTheDocument();
  });

  it("falls back to the config form without losing the choice on the way back", async () => {
    // Selecting a config-only client must not silently rewrite your preference — otherwise a
    // glance at Cursor demotes Claude Code to its JSON for the rest of the session.
    // `"type": "http"` is Claude Code's config and nothing else's — `"mcpServers"` alone would
    // pass against Cursor's stanza and prove nothing.
    const { user, snippet } = await open("Claude Code", "Config file");
    expect(snippet()).toContain('"type": "http"');

    await user.click(screen.getByRole("button", { name: "Cursor" }));
    expect(snippet()).toContain('"mcpServers"');
    expect(snippet()).not.toContain('"type": "http"');

    await user.click(screen.getByRole("button", { name: "Claude Code" }));
    expect(snippet(), "the config choice survived the detour").toContain('"type": "http"');
  });

  it("every client carries the key and none is confused for another", async () => {
    const clients = ["Claude Code", "Cursor", "Codex", "opencode", "Hermes", "OpenClaw", "Grok CLI"];
    const seen = new Map<string, string>();
    for (const c of clients) {
      const snippet = await snippetFor(c);
      expect(snippet).toContain("gb_sk_secret");
      seen.set(c, snippet);
    }

    expect(new Set(seen.values()).size).toBe(clients.length);
    expect(seen.get("Codex")).toContain("http_headers");
    expect(seen.get("Cursor")).toContain('"mcpServers"');
    expect(seen.get("opencode")).toContain("opencode.ai/config.json");
  });

  it("the two OpenClaw forms describe the same server", async () => {
    // One is the other wrapped in a command. If they drift, one of the two routes is wrong and
    // whichever the user picked decides whether their agent connects.
    const command = await snippetFor("OpenClaw", "Command");
    const config = await snippetFor("OpenClaw", "Config file");

    const inner = JSON.parse(command.slice(command.indexOf("'") + 1, command.lastIndexOf("'")));
    expect(JSON.parse(config).mcp.servers.graphban).toEqual(inner);
  });

  it("a placeholder key says so rather than looking ready to paste", async () => {
    render(<McpInstall apiKey="<YOUR_API_KEY>" />);
    expect(screen.getByText(/Replace/)).toBeInTheDocument();
  });
});
