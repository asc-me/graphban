import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { describe, expect, it, vi } from "vitest";

import { McpToolsView } from "@/features/mcp/McpToolsView";
import { settingsPath } from "@/lib/routes";

vi.mock("@/lib/queries", () => ({
  useMcpTools: () => ({
    data: {
      live: 1,
      tools: [{ name: "get_context", description: "orient", params: [], calls: 0, status: "live" }],
    },
    isLoading: false,
    isError: false,
  }),
}));

describe("MCP Tools", () => {
  it("sends someone looking for a key to API keys, not a mint on this catalog", () => {
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    render(
      <QueryClientProvider client={qc}>
        <MemoryRouter>
          <McpToolsView />
        </MemoryRouter>
      </QueryClientProvider>,
    );
    expect(screen.getByRole("link", { name: /looking for a key\?/i }))
      .toHaveAttribute("href", settingsPath("project/api-keys"));
  });
});
