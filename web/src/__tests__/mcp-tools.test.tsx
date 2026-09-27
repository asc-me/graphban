import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter, Outlet, Route, Routes } from "react-router-dom";
import { describe, expect, it, vi } from "vitest";

import { ProjectProvider } from "@/features/ProjectContext";
import { McpToolsView } from "@/features/mcp/McpToolsView";
import { settingsPath } from "@/lib/routes";
import type { McpToolInfo } from "@/lib/types";

const TOOLS: McpToolInfo[] = [
  {
    name: "get_context",
    description: "Orient yourself: the project this key writes to.",
    params: ["project_id"],
    param_details: [
      { name: "project_id", type: "string", description: "Overrides the key's project.", required: false },
    ],
    calls: 42,
    status: "live",
  },
  {
    name: "claim_next",
    description: "Claim ONE ready item and move it to in_progress.",
    params: ["agent_id"],
    param_details: [
      { name: "agent_id", type: "string", description: "Your agent id.", required: true },
    ],
    calls: 10,
    status: "live",
  },
  {
    name: "sign_off",
    description: "Take a reviewed item to done with evidence.",
    params: ["id", "agent_id"],
    param_details: [
      { name: "id", type: "string", description: "The item id.", required: true },
      { name: "agent_id", type: "string", description: "Your agent id.", required: false },
    ],
    calls: 5,
    status: "live",
  },
];

vi.mock("@/lib/queries", () => ({
  useMcpTools: () => ({
    data: { live: 3, tools: TOOLS },
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

  it("shows the detail panel with param_details when a tool is clicked", async () => {
    const user = userEvent.setup();
    renderView();

    await user.click(screen.getByText("get_context"));

    expect(screen.getByText("Parameters")).toBeInTheDocument();
    expect(screen.getByText("project_id")).toBeInTheDocument();
    expect(screen.getByText("Overrides the key's project.")).toBeInTheDocument();
  });

  it("shows 'No tool matches.' and 'Try what you want to do' on search miss", async () => {
    const user = userEvent.setup();
    renderView();

    await user.type(screen.getByLabelText("Search tools"), "xyzzy-no-match");

    expect(screen.getByText("No tool matches.")).toBeInTheDocument();
    expect(screen.getByText(/Try what you want to do/)).toBeInTheDocument();
  });

  it("renders all three role tabs in LoopByRole (worker, planner, reviewer)", () => {
    renderView();

    expect(screen.getByRole("button", { name: /worker/i })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /planner/i })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /reviewer/i })).toBeInTheDocument();
  });

  it("renders ConnectAgent with per-client snippet tabs", () => {
    renderView();

    expect(screen.getByRole("button", { name: /qwen code/i })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /claude code/i })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /^cursor$/i })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /codex/i })).toBeInTheDocument();
  });
});
