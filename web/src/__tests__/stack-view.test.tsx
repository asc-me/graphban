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
import type { StackHealth, StackTopology } from "@/lib/types";

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

const health: StackHealth = {
  project_id: "core",
  head_sha: "abc1234",
  generated_at: "2026-10-18T00:00:00Z",
  scored: false,
  components: [
    {
      id: "api",
      path: "backend/",
      head_sha: "abc1234",
      rollup: {
        label: "FINDINGS",
        state: "has_findings",
        worst_severity: "high",
        severity_badges: [{ severity: "high", count: 1 }],
        meta: "@abc1234",
        finding_count: 1,
      },
      categories: {
        security: {
          label: "Security",
          state: "has_findings",
          scanner: "pip-audit 2.7.3",
          scanned_sha: "abc1234",
          scanned_at: "2026-10-18T00:00:00Z",
          reported_by: "GRPH-A3282",
          findings: [
            {
              severity: "high",
              summary: "python-multipart pinned below 0.0.18",
              path: "backend/pyproject.toml",
              link_kind: "item",
              link_ref: "GRPH-1041",
            },
          ],
        },
        refactor: {
          label: "Refactoring · complexity",
          state: "never_scanned",
          scanner: null,
          scanned_sha: null,
          scanned_at: null,
          findings: [],
          note: "Nothing has scanned this path.",
        },
        stub: {
          label: "Partial implementation",
          state: "never_scanned",
          scanner: null,
          scanned_sha: null,
          scanned_at: null,
          findings: [],
        },
        coverage: {
          label: "Coverage · graph staleness",
          state: "never_scanned",
          scanner: "code-graph health",
          scanned_sha: null,
          scanned_at: null,
          findings: [],
          note: "No describe_code pass has run.",
        },
        work: {
          label: "Open work",
          state: "clean_at_sha",
          scanner: "tracker",
          scanned_sha: null,
          scanned_at: null,
          findings: [],
          note: "No open defects.",
          live: true,
        },
      },
    },
    {
      id: "web",
      path: "web/",
      head_sha: "abc1234",
      rollup: {
        label: "NOT SCANNED",
        state: "never_scanned",
        worst_severity: null,
        severity_badges: [],
        meta: "3 of 5 never run",
        finding_count: 0,
      },
      categories: {
        security: {
          label: "Security",
          state: "never_scanned",
          scanner: null,
          scanned_sha: null,
          scanned_at: null,
          findings: [],
        },
        refactor: {
          label: "Refactoring · complexity",
          state: "never_scanned",
          scanner: null,
          scanned_sha: null,
          scanned_at: null,
          findings: [],
        },
        stub: {
          label: "Partial implementation",
          state: "never_scanned",
          scanner: null,
          scanned_sha: null,
          scanned_at: null,
          findings: [],
        },
        coverage: {
          label: "Coverage · graph staleness",
          state: "never_scanned",
          scanner: null,
          scanned_sha: null,
          scanned_at: null,
          findings: [],
        },
        work: {
          label: "Open work",
          state: "clean_at_sha",
          scanner: "tracker",
          scanned_sha: null,
          scanned_at: null,
          findings: [],
          live: true,
        },
      },
    },
  ],
};

vi.mock("@/lib/api", () => ({
  api: {
    stack: vi.fn(async () => topology),
    stackHealth: vi.fn(async () => health),
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
    vi.mocked(api.stackHealth).mockClear();
    vi.mocked(api.stackHealth).mockResolvedValue(health);
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

  it("code-health overlay defaults on and paints FINDINGS / NOT SCANNED footers", async () => {
    wrap(<StackView />);
    const toggle = await screen.findByTestId("stack-health-overlay");
    expect(toggle).toHaveAttribute("aria-pressed", "true");

    const apiFooter = await screen.findByTestId("stack-health-footer-api");
    expect(apiFooter).toHaveTextContent("FINDINGS");
    expect(screen.getByTestId("stack-node-api")).toHaveAttribute("data-health", "has_findings");

    const webFooter = await screen.findByTestId("stack-health-footer-web");
    expect(webFooter).toHaveTextContent("NOT SCANNED");
    expect(screen.getByTestId("stack-node-web")).toHaveAttribute("data-health", "never_scanned");

    // Overlay never invents a green default for unscanned deployables.
    expect(webFooter).not.toHaveTextContent("CLEAN");
  });

  it("toggling the overlay hides health footers", async () => {
    const user = userEvent.setup();
    wrap(<StackView />);
    await screen.findByTestId("stack-health-footer-api");
    await user.click(screen.getByTestId("stack-health-overlay"));
    expect(screen.getByTestId("stack-health-overlay")).toHaveAttribute("aria-pressed", "false");
    expect(screen.queryByTestId("stack-health-footer-api")).not.toBeInTheDocument();
    expect(screen.queryByTestId("stack-health-footer-web")).not.toBeInTheDocument();
  });

  it("inspector lists findings with severity and path when overlay is on", async () => {
    const user = userEvent.setup();
    wrap(<StackView />);
    await user.click(await screen.findByTestId("stack-node-api"));
    const inspector = await screen.findByTestId("stack-inspector");
    expect(await within(inspector).findByTestId("stack-health-summary")).toHaveTextContent(
      "FINDINGS",
    );
    expect(within(inspector).getByTestId("stack-health-categories")).toHaveTextContent(
      "Security",
    );
    const finding = within(inspector).getByTestId("stack-health-finding");
    expect(finding).toHaveTextContent("python-multipart");
    expect(finding).toHaveTextContent("backend/pyproject.toml");
  });

  it("inspector shows each scanner's provenance (reported_by)", async () => {
    const user = userEvent.setup();
    wrap(<StackView />);
    await user.click(await screen.findByTestId("stack-node-api"));
    const inspector = await screen.findByTestId("stack-inspector");
    // Design: the finding carries "who reported", distinct from a weak scanner's clean.
    const prov = await within(inspector).findByTestId("stack-health-reported-by");
    expect(prov).toHaveTextContent("reported by");
    expect(prov).toHaveTextContent("GRPH-A3282");
  });

  it("inspector renders a finding's cross-reference as a routed link", async () => {
    const user = userEvent.setup();
    wrap(<StackView />);
    await user.click(await screen.findByTestId("stack-node-api"));
    const inspector = await screen.findByTestId("stack-inspector");
    // Design (Stack.dc.html): the finding's link_ref is an anchor, not plain text.
    const link = await within(inspector).findByTestId("stack-health-finding-link");
    expect(link.tagName).toBe("A");
    expect(link).toHaveTextContent("GRPH-1041");
    // link_kind "item" routes to the tracker, deep-linking the item.
    expect(link).toHaveAttribute("href", "/tracker?select=GRPH-1041");
  });

  it("inspector marks infra boxes as code-health N/A", async () => {
    const user = userEvent.setup();
    wrap(<StackView />);
    await user.click(await screen.findByTestId("stack-node-db"));
    const inspector = await screen.findByTestId("stack-inspector");
    expect(await within(inspector).findByTestId("stack-health-na")).toHaveTextContent(
      "Infrastructure",
    );
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
