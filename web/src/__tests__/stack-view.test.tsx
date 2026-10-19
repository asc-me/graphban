import { readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import type { ReactNode } from "react";
import { MemoryRouter } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { StackView } from "@/features/stack/StackView";
import { api } from "@/lib/api";
import type { StackTopology } from "@/lib/types";

const topology: StackTopology = {
  project_id: "core",
  generated_at: "2026-10-18T00:00:00Z",
  hosts: [
    { id: "compose", label: "Compose / Railway stack", nodes: ["web", "api", "db"] },
    { id: "agent_hosts", label: "Agent hosts / outposts", nodes: ["fleet"] },
    { id: "user_machines", label: "User machines", nodes: ["cli"] },
    { id: "external", label: "External services", nodes: ["email"] },
  ],
  nodes: [
    {
      id: "api", label: "api", title: "graphban-api", kind: "deployable", ring: 1,
      scope: "instance", host: "compose", liveness: "reachable",
      surfaces: ["REST", "MCP"], extensions: [],
      facts: [
        { key: "version", value: "2026.10.2", provenance: "instance_update.running" },
        { key: "mcp_tools", value: "57", provenance: "mcp_server.LIVE_TOOL_COUNT" },
      ],
    },
    {
      id: "web", label: "web", title: "web (React SPA)", kind: "deployable", ring: 1,
      scope: "instance", host: "compose", liveness: "unknown",
      surfaces: [], extensions: [],
      facts: [{ key: "bundle_sha", value: "unknown", provenance: "/version.txt" }],
    },
    {
      id: "db", label: "db", title: "SQLite", kind: "infra", ring: 2,
      scope: "instance", host: "compose", liveness: "reachable",
      surfaces: [], extensions: [],
      facts: [{ key: "db", value: "ok", provenance: "SELECT 1 (aggregator)" }],
    },
    {
      id: "fleet", label: "fleet", title: "gbfleet", kind: "deployable", ring: 1,
      scope: "instance", host: "agent_hosts", liveness: "not_configured",
      surfaces: [], extensions: [],
      facts: [{ key: "online", value: "0", provenance: "GET /api/fleet" }],
    },
    {
      id: "cli", label: "cli", title: "gb", kind: "deployable", ring: 1,
      scope: "instance", host: "user_machines", liveness: "unknown",
      surfaces: [], extensions: [], facts: [],
    },
    {
      id: "email", label: "email", title: "Email (SMTP)", kind: "external", ring: 3,
      scope: "instance", host: "external", liveness: "not_configured",
      surfaces: [], extensions: [],
      facts: [{ key: "smtp_host", value: "(unset)", provenance: "settings.smtp_host" }],
    },
  ],
  edges: [
    { from: "web", to: "api", protocol: "HTTP/JWT", label: "HTTP/JWT" },
    { from: "api", to: "db", protocol: "sqlite", label: "sqlite" },
    { from: "fleet", to: "api", protocol: "HTTP + MCP JSON-RPC", label: "HTTP + MCP" },
    { from: "cli", to: "api", protocol: "HTTP/JWT", label: "HTTP/JWT" },
  ],
  empties: [{ id: "fleet", message: "no fleet registered" }],
  catalog: { providers_total: 21, providers_wired: 0, unwired_count: 21 },
  identity: {
    api_version: "2026.10.2",
    api_git_sha: "abc1234",
    web_bundle_sha: null,
    schema_revision: null,
    db_engine: "sqlite",
    mcp_tools: 57,
  },
};

vi.mock("@/lib/api", () => ({
  api: {
    stack: vi.fn(async () => topology),
  },
}));

vi.mock("@/features/ProjectContext", () => ({
  useProjectCtx: () => ({
    activeId: "core",
    active: { id: "core", tag: "CORE", name: "Core" },
    projects: [],
    setActiveId: () => {},
  }),
}));

function wrap(ui: ReactNode) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={qc}>
      <MemoryRouter initialEntries={["/stack"]}>{ui}</MemoryRouter>
    </QueryClientProvider>,
  );
}

describe("StackView", () => {
  beforeEach(() => {
    vi.mocked(api.stack).mockClear();
    vi.mocked(api.stack).mockResolvedValue(topology);
  });

  it("renders host groups, empty states, and opens the inspector with provenance", async () => {
    const user = userEvent.setup();
    wrap(<StackView />);

    expect(await screen.findByTestId("stack-view")).toBeInTheDocument();
    expect(await screen.findByTestId("stack-identity")).toHaveTextContent("api 2026.10.2");
    expect(screen.getByTestId("stack-empties")).toHaveTextContent("no fleet registered");
    expect(screen.getByTestId("stack-empties")).toHaveTextContent("21 catalog provider");

    expect(screen.getByTestId("stack-host-compose")).toBeInTheDocument();
    expect(screen.getByTestId("stack-node-api")).toBeInTheDocument();
    expect(screen.getByTestId("stack-node-db")).toBeInTheDocument();

    // MCP is a surface on api, not a peer box.
    expect(screen.queryByTestId("stack-node-mcp")).not.toBeInTheDocument();

    await user.click(screen.getByTestId("stack-node-api"));
    const inspector = await screen.findByTestId("stack-inspector");
    expect(inspector).toHaveTextContent("graphban-api");
    expect(inspector).toHaveTextContent("instance_update.running");
    expect(within(inspector).getByText("MCP")).toBeInTheDocument();
    expect(inspector).toHaveTextContent("HTTP/JWT");
  });

  it("keeps unknown web liveness — never paints a missing sha as reachable", async () => {
    wrap(<StackView />);
    const web = await screen.findByTestId("stack-node-web");
    expect(web).toHaveTextContent("unknown");
    expect(web).not.toHaveTextContent("reachable");
  });

  it("draws every edge as a directional, liveness-coloured path (not a text list)", async () => {
    const { container } = wrap(<StackView />);
    await screen.findByTestId("stack-canvas");
    const paths = container.querySelectorAll("path[data-edge]");
    // One drawn path per edge in the topology.
    expect(paths.length).toBe(topology.edges.length);
    // Each is directional — carries the arrowhead marker.
    paths.forEach((p) => expect(p).toHaveAttribute("marker-end", "url(#stack-arrow)"));
    // Colour follows the unhealthiest endpoint: web(unknown)->api(reachable) = unknown.
    expect(container.querySelector('path[data-edge="web->api"]')).toHaveAttribute(
      "data-liveness",
      "unknown",
    );
    // api(reachable)->db(reachable) = reachable.
    expect(container.querySelector('path[data-edge="api->db"]')).toHaveAttribute(
      "data-liveness",
      "reachable",
    );
  });

  it("inspector connections are clickable and select the peer", async () => {
    const user = userEvent.setup();
    wrap(<StackView />);
    await user.click(await screen.findByTestId("stack-node-api"));
    const inspector = await screen.findByTestId("stack-inspector");
    // api's edge to db renders as a selectable connection row.
    await user.click(within(inspector).getByTestId("stack-conn-db"));
    // The inspector now shows the db node.
    expect(await screen.findByTestId("stack-inspector")).toHaveTextContent("SQLite");
  });

  it("selecting a host highlights its nodes and dims the rest", async () => {
    const user = userEvent.setup();
    wrap(<StackView />);
    const host = await screen.findByTestId("stack-host-compose");
    await user.click(within(host).getByRole("button"));
    // compose owns web/api/db — they light up; cli (user machines) dims.
    expect(screen.getByTestId("stack-node-api")).toHaveAttribute("data-host-lit");
    expect(screen.getByTestId("stack-node-cli")).toHaveClass("opacity-40");
  });
});

describe("LeftNav Build lists Stack", () => {
  it("includes Stack after Code graph in both BUILD lists", () => {
    const src = readFileSync(
      join(dirname(fileURLToPath(import.meta.url)), "../components/shell/LeftNav.tsx"),
      "utf8",
    );
    expect(src).toMatch(/label: "Stack"/);
    expect(src).toMatch(/to: "\/stack"/);
    expect(src).toMatch(/to: "stack"/); // hosted relative view
    // PRD: Stack sits between Code graph and MCP Tools / Links.
    const code = src.indexOf('label: "Code graph"');
    const stack = src.indexOf('label: "Stack"');
    expect(code).toBeGreaterThan(-1);
    expect(stack).toBeGreaterThan(code);
  });
});
