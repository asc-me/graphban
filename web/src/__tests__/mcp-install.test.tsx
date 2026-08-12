import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it } from "vitest";

import { McpInstall } from "@/features/settings/McpInstall";

/**
 * The connect snippets are copied verbatim into a config file or a terminal, so a wrong one
 * fails at the worst moment — an agent that will not start, with a snippet the product handed
 * over and vouched for.
 *
 * These pin the SHAPE each tool documents, not the exact bytes: a schema change should fail
 * here, a reworded note should not. Formats are verified against each vendor's official MCP
 * docs; Grok's is `~/.grok/config.toml` per docs.x.ai, which replaced an `mcp-remote` stdio
 * bridge written when that schema was undocumented.
 */
async function snippetFor(client: string) {
  const user = userEvent.setup();
  const { container, unmount } = render(<McpInstall apiKey="gb_sk_secret" />);
  await user.click(screen.getByRole("button", { name: client }));
  const text = container.querySelector("pre")?.textContent ?? "";
  unmount();
  return text;
}

describe("MCP install snippets", () => {
  it("Grok CLI gets TOML at ~/.grok/config.toml, not a stdio bridge", async () => {
    const snippet = await snippetFor("Grok CLI");

    expect(snippet).toContain("[mcp_servers.graphban]");
    expect(snippet).toContain('headers = { "X-API-Key" = "gb_sk_secret" }');
    // The old bridge spawned an extra process and pointed at a path that does not exist.
    expect(snippet).not.toContain("mcp-remote");
    expect(snippet).not.toContain("npx");
    expect(screen.queryByText(/\.grok\/settings\.json/)).not.toBeInTheDocument();
  });

  it("every client carries the key and none is confused for another", async () => {
    const clients = ["Claude Code", "Cursor", "Codex", "opencode", "Grok CLI"];
    const seen = new Map<string, string>();
    for (const c of clients) {
      const snippet = await snippetFor(c);
      expect(snippet).toContain("gb_sk_secret");
      seen.set(c, snippet);
    }

    // Five clients, five distinct formats. A shared stub is exactly how the Fleet view came
    // to hand `claude mcp add` to three tools that have no such command.
    expect(new Set(seen.values()).size).toBe(clients.length);
    expect(seen.get("Claude Code")).toContain("claude mcp add");
    expect(seen.get("Codex")).toContain("http_headers");
    expect(seen.get("Cursor")).toContain('"mcpServers"');
    expect(seen.get("opencode")).toContain("opencode.ai/config.json");
  });

  it("a placeholder key says so rather than looking ready to paste", async () => {
    render(<McpInstall apiKey="<YOUR_API_KEY>" />);
    expect(screen.getByText(/Replace/)).toBeInTheDocument();
  });
});
