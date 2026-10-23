import { readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor, within } from "@testing-library/react";
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
              filed_key: "stack-filed:aaaaaaaaaaaa",
              filed_item: null,
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
          cta: {
            kind: "add_scanner",
            path: "backend/",
            filed_key: "stack-filed:bbbbbbbbbbbb",
            filed_item: null,
          },
        },
        stub: {
          label: "Partial implementation",
          state: "never_scanned",
          scanner: null,
          scanned_sha: null,
          scanned_at: null,
          findings: [],
        },
        deadcode: {
          label: "Dead code · unused",
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
        tests: {
          label: "Tests",
          state: "clean_at_sha",
          scanner: "pytest 8.3.3",
          scanned_sha: "abc1234",
          scanned_at: "2026-10-18T00:00:00Z",
          findings: [],
          note: "412 passed · 0 failed · 3 skipped",
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
        meta: "4 of 6 never run",
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
        deadcode: {
          label: "Dead code · unused",
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
        tests: {
          label: "Tests",
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
    createItem: vi.fn(async (_pid: string, body: { title?: string }) => ({
      id: "GRPH-77",
      title: body.title,
    })),
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
    vi.mocked(api.createItem).mockClear();
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

  it("a box this project runs but does not write carries no code-health footer", async () => {
    // Health is keyed by repo path, so on a project that only RUNS Graphban the `web`
    // component is that project's own web/ — it must not paint on Graphban's web box.
    const user = userEvent.setup();
    vi.mocked(api.stack).mockResolvedValue({
      ...topology,
      nodes: topology.nodes.map((n) => (n.id === "web" ? { ...n, ring: 2 } : n)),
    });
    wrap(<StackView />);
    await screen.findByTestId("stack-health-footer-api");
    expect(screen.queryByTestId("stack-health-footer-web")).not.toBeInTheDocument();
    await user.click(screen.getByTestId("stack-node-web"));
    expect(await screen.findByTestId("stack-health-na")).toHaveTextContent(/didn't write/i);
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

  it("FINDINGS takes the colour of the worst severity present, not a fixed orange", async () => {
    const withWorst = (worst: string) => ({
      ...health,
      components: health.components.map((c) =>
        c.id === "api"
          ? {
              ...c,
              rollup: {
                ...c.rollup,
                worst_severity: worst,
                severity_badges: [{ severity: worst, count: 1 }],
              },
            }
          : c,
      ),
    });
    vi.mocked(api.stackHealth).mockResolvedValue(withWorst("crit"));
    const { unmount } = wrap(<StackView />);
    let footer = await screen.findByTestId("stack-health-footer-api");
    expect(footer).toHaveAttribute("data-worst", "crit");
    expect(within(footer).getByText("FINDINGS").className).toContain("text-rose-300");
    unmount();

    vi.mocked(api.stackHealth).mockResolvedValue(withWorst("low"));
    wrap(<StackView />);
    footer = await screen.findByTestId("stack-health-footer-api");
    const label = within(footer).getByText("FINDINGS");
    expect(label.className).toContain("text-muted");
    expect(label.className).not.toContain("orange");
  });

  it("Defrag files a cleanup item tagged with the finding's key", async () => {
    const user = userEvent.setup();
    wrap(<StackView />);
    await user.click(await screen.findByTestId("stack-node-api"));
    const inspector = await screen.findByTestId("stack-inspector");
    await user.click(await within(inspector).findByTestId("stack-health-defrag"));
    expect(api.createItem).toHaveBeenCalledTimes(1);
    const [pid, body] = vi.mocked(api.createItem).mock.calls[0];
    expect(pid).toBe("core");
    expect(body.title).toContain("python-multipart");
    expect(body.tags).toContain("stack-filed:aaaaaaaaaaaa");
    expect(body.touchpoints).toEqual(["backend/pyproject.toml"]);
    // Filing refetches health so the server can say it is filed.
    await waitFor(() => expect(api.stackHealth).toHaveBeenCalledTimes(2));
  });

  it("an already-filed finding shows its item instead of offering Defrag again", async () => {
    const filed = structuredClone(health);
    filed.components[0].categories.security.findings[0].filed_item = "GRPH-55";
    vi.mocked(api.stackHealth).mockResolvedValue(filed);
    const user = userEvent.setup();
    wrap(<StackView />);
    await user.click(await screen.findByTestId("stack-node-api"));
    const inspector = await screen.findByTestId("stack-inspector");
    const finding = await within(inspector).findByTestId("stack-health-finding");
    expect(within(finding).queryByTestId("stack-health-defrag")).not.toBeInTheDocument();
    expect(within(finding).getByTestId("stack-health-filed")).toHaveTextContent("filed GRPH-55");
  });

  it("a never-scanned cell offers 'Add a scanner →' and files it", async () => {
    const user = userEvent.setup();
    wrap(<StackView />);
    await user.click(await screen.findByTestId("stack-node-api"));
    const inspector = await screen.findByTestId("stack-inspector");
    const cta = await within(inspector).findByTestId("stack-health-cta-refactor");
    expect(cta).toHaveTextContent("Add a scanner →");
    // A cell the server offered nothing for draws no button.
    expect(within(inspector).queryByTestId("stack-health-cta-work")).not.toBeInTheDocument();
    await user.click(cta);
    const [, body] = vi.mocked(api.createItem).mock.calls[0];
    expect(body.title).toBe("Add a Refactoring · complexity scanner for backend/");
    expect(body.tags).toContain("stack-filed:bbbbbbbbbbbb");
  });

  it("a stale cell offers 'Re-run at head →'", async () => {
    const stale = structuredClone(health);
    stale.components[0].categories.refactor = {
      ...stale.components[0].categories.refactor,
      state: "stale",
      scanner: "radon 6.0.1",
      scanned_sha: "0ld5ha0",
      cta: { kind: "rerun", path: "backend/", filed_key: "stack-filed:cccccccccccc", filed_item: null },
    };
    vi.mocked(api.stackHealth).mockResolvedValue(stale);
    const user = userEvent.setup();
    wrap(<StackView />);
    await user.click(await screen.findByTestId("stack-node-api"));
    const inspector = await screen.findByTestId("stack-inspector");
    const cta = await within(inspector).findByTestId("stack-health-cta-refactor");
    expect(cta).toHaveTextContent("Re-run at head →");
    await user.click(cta);
    const [, body] = vi.mocked(api.createItem).mock.calls[0];
    expect(body.title).toBe("Re-run radon 6.0.1 on backend/ at head");
  });

  it("inspector shows the Tests category with its passed/failed/skipped counts (GRPH-1061)", async () => {
    const user = userEvent.setup();
    wrap(<StackView />);
    await user.click(await screen.findByTestId("stack-node-api"));
    const inspector = await screen.findByTestId("stack-inspector");
    expect(await within(inspector).findByTestId("stack-health-cat-tests")).toHaveTextContent("CLEAN");
    expect(within(inspector).getByTestId("stack-health-categories")).toHaveTextContent(
      "412 passed · 0 failed · 3 skipped",
    );
  });

  it("a failing test is a finding with its test id and file, filterable by the Tests category", async () => {
    const failing = structuredClone(health);
    failing.components[0].categories.tests = {
      ...failing.components[0].categories.tests,
      state: "has_findings",
      note: "410 passed · 2 failed · 3 skipped",
      findings: [
        {
          severity: "high",
          summary: "tests/test_scans.py::test_zero_run",
          path: "backend/tests/test_scans.py",
          filed_key: "stack-filed:dddddddddddd",
          filed_item: null,
        },
      ],
    };
    vi.mocked(api.stackHealth).mockResolvedValue(failing);
    const user = userEvent.setup();
    wrap(<StackView />);
    await screen.findByTestId("stack-canvas");
    await user.click(screen.getByTestId("stack-report-toggle"));
    const report = await screen.findByTestId("stack-report");
    expect(within(report).getByTestId("stack-report-matrix")).toHaveTextContent("Tests");
    await user.click(within(report).getByTestId("stack-report-filter-cat-tests"));
    const row = within(report).getByTestId("stack-report-finding");
    expect(row).toHaveTextContent("tests/test_scans.py::test_zero_run");
    expect(row).toHaveTextContent("backend/tests/test_scans.py");
    // web has no tests result at all — its Tests cell reads "not run", never clean.
    const matrix = within(report).getByTestId("stack-report-matrix");
    const heads = Array.from(matrix.querySelectorAll("thead th")).map((th) => th.textContent);
    const col = heads.indexOf("Tests");
    expect(col).toBeGreaterThan(0);
    const webRow = Array.from(matrix.querySelectorAll("tbody tr")).find((tr) =>
      tr.textContent?.startsWith("web"),
    )!;
    expect(webRow.querySelectorAll("td")[col]).toHaveTextContent("not run");
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

  it("scan report lists every component's findings and the never-scanned matrix (GRPH-1049)", async () => {
    const user = userEvent.setup();
    wrap(<StackView />);
    await screen.findByTestId("stack-canvas");
    await user.click(screen.getByTestId("stack-report-toggle"));

    const report = await screen.findByTestId("stack-report");
    // The one reported finding shows, across all components (not per-node click).
    expect(within(report).getByTestId("stack-report-finding")).toHaveTextContent(
      "python-multipart",
    );
    // Coverage matrix is honest: web's never-scanned cells read "not run", not clean.
    expect(within(report).getByTestId("stack-report-matrix")).toHaveTextContent("not run");
    expect(within(report).getByTestId("stack-report-count")).toHaveTextContent("1 finding");
  });

  it("scan report filters by severity, and absence never reads as clean", async () => {
    const user = userEvent.setup();
    wrap(<StackView />);
    await screen.findByTestId("stack-canvas");
    await user.click(screen.getByTestId("stack-report-toggle"));
    const report = await screen.findByTestId("stack-report");
    // The only finding is HIGH; filtering to CRIT hides it but keeps the matrix honest.
    await user.click(within(report).getByTestId("stack-report-filter-sev-crit"));
    expect(within(report).getByTestId("stack-report-none")).toBeInTheDocument();
    expect(within(report).getByTestId("stack-report-matrix")).toHaveTextContent("1 finding");
    // Back to HIGH surfaces it again.
    await user.click(within(report).getByTestId("stack-report-filter-sev-crit"));
    await user.click(within(report).getByTestId("stack-report-filter-sev-high"));
    expect(within(report).getByTestId("stack-report-finding")).toHaveTextContent("python-multipart");
  });
});

describe("StackView dead-code category (GRPH-1060)", () => {
  beforeEach(() => {
    vi.mocked(api.stack).mockClear();
    vi.mocked(api.stack).mockResolvedValue(topology);
    vi.mocked(api.stackHealth).mockClear();
    vi.mocked(api.stackHealth).mockResolvedValue(health);
    vi.mocked(api.createItem).mockClear();
  });

  const withDeadCode = () => {
    const h = structuredClone(health);
    h.components[0].categories.security = {
      ...h.components[0].categories.security,
      state: "clean_at_sha",
      findings: [],
    };
    h.components[0].categories.deadcode = {
      label: "Dead code · unused",
      state: "has_findings",
      scanner: "vulture 2.14",
      scanned_sha: "abc1234",
      scanned_at: "2026-10-18T00:00:00Z",
      findings: [
        {
          severity: "low",
          summary: "unused function poll_once",
          path: "backend/app/sync/legacy.py",
          link_kind: "",
          link_ref: "",
          filed_key: "stack-filed:dddddddddddd",
          filed_item: null,
        },
      ],
    };
    return h;
  };

  it("a never-scanned dead-code cell reads as not run in the inspector, not clean", async () => {
    const user = userEvent.setup();
    wrap(<StackView />);
    await user.click(await screen.findByTestId("stack-node-web"));
    const inspector = await screen.findByTestId("stack-inspector");
    const cats = await within(inspector).findByTestId("stack-health-categories");
    const row = within(cats).getByText("Dead code · unused").closest("li");
    expect(row).not.toBeNull();
    expect(row).not.toHaveTextContent(/clean/i);
  });

  it("the scan report has a Dead code column and a category filter for it", async () => {
    const user = userEvent.setup();
    wrap(<StackView />);
    await screen.findByTestId("stack-canvas");
    await user.click(screen.getByTestId("stack-report-toggle"));
    const report = await screen.findByTestId("stack-report");
    const matrix = within(report).getByTestId("stack-report-matrix");
    const headers = within(matrix).getAllByRole("columnheader").map((th) => th.textContent);
    expect(headers).toContain("Dead code");
    // Ordered after Partial implementation, before Coverage.
    expect(headers.indexOf("Dead code")).toBe(headers.indexOf("Partial implementation") + 1);
    expect(within(report).getByTestId("stack-report-filter-cat-deadcode")).toBeInTheDocument();
  });

  it("filtering to dead code shows only dead-code findings", async () => {
    vi.mocked(api.stackHealth).mockResolvedValue(withDeadCode());
    const user = userEvent.setup();
    wrap(<StackView />);
    await screen.findByTestId("stack-canvas");
    await user.click(screen.getByTestId("stack-report-toggle"));
    const report = await screen.findByTestId("stack-report");
    await user.click(within(report).getByTestId("stack-report-filter-cat-deadcode"));
    const rows = within(report).getAllByTestId("stack-report-finding");
    expect(rows).toHaveLength(1);
    expect(rows[0]).toHaveTextContent("poll_once");
  });

  it("Defrag on a dead-code finding files one card, then shows it filed", async () => {
    const dead = withDeadCode();
    vi.mocked(api.stackHealth).mockResolvedValue(dead);
    const user = userEvent.setup();
    wrap(<StackView />);
    await user.click(await screen.findByTestId("stack-node-api"));
    const inspector = await screen.findByTestId("stack-inspector");
    const filed = structuredClone(dead);
    filed.components[0].categories.deadcode.findings[0].filed_item = "GRPH-77";
    vi.mocked(api.stackHealth).mockResolvedValue(filed);
    await user.click(await within(inspector).findByTestId("stack-health-defrag"));
    expect(api.createItem).toHaveBeenCalledTimes(1);
    const [, body] = vi.mocked(api.createItem).mock.calls[0];
    expect(body.title).toContain("poll_once");
    expect(body.tags).toContain("stack-filed:dddddddddddd");
    // The refetch says it is filed: no second Defrag to click.
    expect(await within(inspector).findByTestId("stack-health-filed")).toHaveTextContent(
      "filed GRPH-77",
    );
    expect(within(inspector).queryByTestId("stack-health-defrag")).not.toBeInTheDocument();
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
