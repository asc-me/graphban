import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen } from "@testing-library/react";
import { MemoryRouter, Outlet, Route, Routes } from "react-router-dom";
import { describe, expect, it, vi } from "vitest";

import { ProjectProvider } from "@/features/ProjectContext";
import { McpToolsView } from "@/features/mcp/McpToolsView";
import { settingsPath } from "@/lib/routes";

const TOOLS = [
  {
    name: "get_context",
    description: "orient yourself",
    params: ["scope"],
    param_details: [
      { name: "scope", type: "string", description: "What to include", required: false },
    ],
    calls: 42,
    status: "live",
  },
  {
    name: "claim_next",
    description: "claim one ready item",
    params: ["agent_id"],
    param_details: [
      { name: "agent_id", type: "string", description: "Your agent id", required: true },
    ],
    calls: 10,
    status: "live",
  },
  {
    name: "register_agent",
    description: "register this process",
    params: [],
    param_details: [],
    calls: 5,
    status: "live",
  },
  {
    name: "heartbeat",
    description: "extend lease and presence",
    params: [],
    param_details: [],
    calls: 100,
    status: "live",
  },
  {
    name: "get_item_details",
    description: "full record for one item",
    params: [],
    param_details: [],
    calls: 30,
    status: "live",
  },
  {
    name: "update_item",
    description: "patch fields or advance status",
    params: [],
    param_details: [],
    calls: 20,
    status: "live",
  },
];

vi.mock("@/lib/queries", () => ({
  useMcpTools: () => ({
    data: { live: TOOLS.length, tools: TOOLS },
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

function renderView() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
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
}

describe("MCP Tools", () => {
  it("sends someone looking for API keys to API keys, not a mint on this catalog", () => {
    renderView();
    expect(screen.getByRole("link", { name: /looking for api keys\?/i }))
      .toHaveAttribute("href", settingsPath("project/api-keys"));
    expect(screen.queryByRole("link", { name: /looking for a key\?/i })).not.toBeInTheDocument();
  });

  it("opens the detail panel with param_details when a tool is clicked", () => {
    renderView();
    fireEvent.click(screen.getAllByText("get_context")[0]);
    expect(screen.getByText("Parameters")).toBeInTheDocument();
    expect(screen.getByText("scope")).toBeInTheDocument();
    expect(screen.getByText("What to include")).toBeInTheDocument();
  });

  it("shows search-miss copy when no tool matches the query", () => {
    renderView();
    fireEvent.change(screen.getByLabelText("Search tools"), {
      target: { value: "xyzzy-no-match" },
    });
    expect(screen.getByText("No tool matches.")).toBeInTheDocument();
    expect(screen.getByText(/try what you want to do/i)).toBeInTheDocument();
  });

  it("renders three role tabs in the loop-by-role section", () => {
    renderView();
    expect(screen.getByRole("button", { name: /worker/i })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /planner/i })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /reviewer/i })).toBeInTheDocument();
  });
});
