import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen } from "@testing-library/react";
import { MemoryRouter, Outlet, Route, Routes } from "react-router-dom";
import { describe, expect, it, vi } from "vitest";

import { ProjectProvider } from "@/features/ProjectContext";
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
  useProjects: () => ({ data: [{ id: "core", name: "Core", accent: "#a78bfa", tag: "core" }], isLoading: false }),
}));

vi.mock("@/lib/api", () => ({
  setActiveProjectId: vi.fn(),
  api: {
    projects: vi.fn(async () => [{ id: "core", name: "Core", accent: "#a78bfa", tag: "core" }]),
  },
}));

describe("MCP Tools", () => {
  it("sends someone looking for API keys to API keys, not a mint on this catalog", () => {
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    render(
      <QueryClientProvider client={qc}>
        <MemoryRouter initialEntries={["/mcp-tools"]}>
          <ProjectProvider>
            <Routes>
              <Route element={<Outlet context={""} />}>
                <Route path="/mcp-tools" element={<McpToolsView />} />
              </Route>
            </Routes>
          </ProjectProvider>
        </MemoryRouter>
      </QueryClientProvider>,
    );
    // Same question AI Providers asks. "Looking for a key?" could be an Anthropic secret.
    expect(screen.getByRole("link", { name: /looking for api keys\?/i }))
      .toHaveAttribute("href", settingsPath("project/api-keys"));
    expect(screen.queryByRole("link", { name: /looking for a key\?/i })).not.toBeInTheDocument();
  });
});
