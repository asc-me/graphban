import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter, Outlet, Route, Routes } from "react-router-dom";
import { describe, expect, it, vi } from "vitest";

import { ProjectProvider } from "@/features/ProjectContext";
import { GROUPS, enrich, rolesOf, tierOf } from "@/features/mcp/catalog";
import { McpToolsView } from "@/features/mcp/McpToolsView";
import { settingsPath } from "@/lib/routes";
import type { McpToolInfo } from "@/lib/types";

const TOOLS: McpToolInfo[] = [
  { name: "get_context", description: "orient", params: ["project"], param_details: [], calls: 10, status: "live" },
  { name: "register_agent", description: "join", params: ["label"], param_details: [
    { name: "label", type: "string", description: "<model> @ <host>", required: true },
  ], calls: 4, status: "live" },
  { name: "claim_cluster", description: "take work", params: [], param_details: [], calls: 2, status: "live" },
  { name: "create_prd", description: "draft a spec", params: ["title"], param_details: [
    { name: "title", type: "string", description: "", required: true },
  ], calls: 1, status: "live" },
];

vi.mock("@/lib/queries", () => ({
  useMcpTools: () => ({
    data: { live: TOOLS.length, tools: TOOLS },
    isLoading: false,
    isError: false,
    refetch: vi.fn(),
  }),
  useProjects: () => ({
    data: [{ id: "core", name: "Core", accent: "#a78bfa", tag: "core" }],
    isLoading: false,
  }),
  useConfig: () => ({ data: { hosted_mode: false } }),
}));

vi.mock("@/lib/api", () => ({
  setActiveProjectId: vi.fn(),
  api: {
    projects: vi.fn(async () => [{ id: "core", name: "Core", accent: "#a78bfa", tag: "core" }]),
  },
}));

function show() {
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
    show();
    expect(screen.getByRole("link", { name: /looking for api keys\?/i }))
      .toHaveAttribute("href", settingsPath("project/api-keys"));
    expect(screen.queryByRole("link", { name: /looking for a key\?/i })).not.toBeInTheDocument();
  });

  it("links seats to Fleet, not a mint on this page", () => {
    show();
    expect(screen.getByRole("link", { name: /seats on fleet/i })).toHaveAttribute("href", "/fleet");
  });

  it("walks connect-an-agent as three steps over verified client snippets", () => {
    show();
    const connect = screen.getByTestId("mcp-connect");
    expect(connect).toHaveTextContent("Mint a key");
    expect(connect).toHaveTextContent("Add the server");
    expect(connect).toHaveTextContent("First call");
    expect(connect).toHaveTextContent("POST /api/mcp");
    // Verified CLIENTS command, not a localhost:8000 JSON stanza.
    expect(connect).toHaveTextContent(/claude mcp add/);
    expect(connect).not.toHaveTextContent("localhost:8000");
    expect(connect).not.toHaveTextContent("Qwen Code");
  });

  it("teaches the loop as all-in-one / worker / planner, not a reviewer role", async () => {
    const user = userEvent.setup();
    show();
    const loop = screen.getByTestId("mcp-loop");
    expect(loop).toHaveTextContent("all-in-one");
    expect(loop).toHaveTextContent("worker");
    expect(loop).toHaveTextContent("planner");
    expect(loop).not.toHaveTextContent("reviewer");
    expect(loop).toHaveTextContent(/Avoid claim_next/);
    await user.click(screen.getByRole("button", { name: "planner" }));
    expect(screen.getByTestId("mcp-avoid")).toHaveTextContent(/tools_off_limits/);
  });

  it("keeps a detail panel open with when / example / roles, and the search-miss copy", async () => {
    const user = userEvent.setup();
    show();
    const detail = screen.getByTestId("mcp-detail");
    expect(detail).toBeInTheDocument();
    // Default selection is claim_cluster; `when` is the overlay body, not the list `short`.
    expect(detail).toHaveTextContent(/The one way to take work/);
    expect(detail).toHaveTextContent(/Roles/);
    await user.click(
      within(screen.getByTestId("mcp-reference")).getByRole("button", { name: /register_agent/i }),
    );
    expect(screen.getByTestId("mcp-detail")).toHaveTextContent(/Refused when/);
    expect(screen.getByTestId("mcp-detail")).toHaveTextContent(/Usually followed by/);

    await user.type(screen.getByRole("textbox", { name: "Search tools" }), "zzzznope");
    expect(screen.getByTestId("mcp-none")).toHaveTextContent(
      'No tool matches. Try what you want to do, e.g. "review" or "files".',
    );
  });
});

describe("MCP catalog overlay", () => {
  it("reads tier from the same map as tool_tiers.py, defaulting unlisted to core", () => {
    expect(tierOf("create_prd")).toBe("prd");
    expect(tierOf("describe_code")).toBe("codegraph");
    expect(tierOf("record_scan")).toBe("codegraph");
    expect(tierOf("mint_enrolment")).toBe("fleet");
    expect(tierOf("get_context")).toBe("core");
    expect(tierOf("a_tool_that_does_not_exist")).toBe("core");
    const names = GROUPS.flatMap((g) => g.names);
    expect(names).toContain("propose_allocation");
    expect(names).not.toContain("allocate_cluster");
    expect(rolesOf("claim_cluster")).toEqual(["all-in-one", "worker"]);
    expect(rolesOf("create_prd")).toEqual(["planner"]);
  });

  it("does not invent returns / refused / next for a tool the overlay does not name", () => {
    const t = enrich({
      name: "search_items", description: "Find work in the stream.", params: ["query"],
      param_details: [{ name: "query", type: "string", description: "substring", required: false }],
      calls: 0, status: "live",
    });
    expect(t.short).toBe("Find work in the stream.");
    expect(t.example).toBe("search_items()");
    expect(t.returns).toBe("");
    expect(t.refused).toBe("");
    expect(t.next).toEqual([]);
    expect(rolesOf("claim_cluster")).toEqual(["all-in-one", "worker"]);
    expect(rolesOf("create_prd")).toEqual(["planner"]);
  });
});
