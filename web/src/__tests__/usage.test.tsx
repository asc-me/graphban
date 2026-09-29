import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { ProjectProvider } from "@/features/ProjectContext";
import { UsageView } from "@/features/usage/UsageView";
import type { UsageAggregate } from "@/lib/types";

const SAMPLE: UsageAggregate = {
  identity: {
    mode: "self-host",
    host: "localhost:8080",
    version: "0.2.0",
    git_sha: "abc1234",
    plan: "self-host",
    license: "self-host",
  },
  range_days: 30,
  retention_days: 7,
  coverage: "partial",
  kpis: [
    { id: "mcp_calls", label: "MCP calls", value: 42, delta: 3, sparkline: [{ start: "", end: "", value: 1 }] },
    { id: "agent_sessions", label: "Agent sessions", value: 2, delta: null, sparkline: [] },
    { id: "active_projects", label: "Active projects", value: 1, delta: null, sparkline: [] },
    { id: "seats", label: "Seats in use", value: null, delta: null, sparkline: [] },
    { id: "shards", label: "Memory shards", value: 10, delta: null, sparkline: [] },
  ],
  chart: {
    buckets: [{ start: "2026-09-01", end: "2026-09-01", value: 5 }],
    projects: [{ id: "core", tag: "GRPH", name: "Graphban", series: [{ start: "", end: "", value: 5 }], total: 5 }],
    coverage: "partial",
    note: "Bars cover the newest 7 days",
  },
  by_project: [{ id: "core", tag: "GRPH", name: "Graphban", calls: 42, agents: 2, shards: 10, done: 3 }],
  limits: [
    { id: "seats", label: "Seats", used: null, limit: null, declared: false },
    { id: "mcp_calls", label: "MCP calls / month", used: null, limit: null, declared: false },
    { id: "projects", label: "Projects", used: null, limit: null, declared: false },
    { id: "shards", label: "Memory shards", used: null, limit: null, declared: false },
  ],
  on_pace_note: "This deployment does not declare plan limits",
  busiest_keys: [{ id: "k1", name: "loop", owner: "alex", calls: 40, last_seen: "2026-09-29T12:00:00Z" }],
};

vi.mock("@/lib/api", () => ({
  setActiveProjectId: vi.fn(),
  api: {
    projects: vi.fn(async () => [
      { id: "core", name: "Graphban", tag: "GRPH", accent: "#c6f24e", description: "" },
    ]),
    config: vi.fn(async () => ({ hosted_mode: false })),
    usage: vi.fn(),
    usageCsv: vi.fn(),
  },
}));

import { api } from "@/lib/api";

function show() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={qc}>
      <MemoryRouter>
        <ProjectProvider>
          <UsageView />
        </ProjectProvider>
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

beforeEach(() => {
  vi.mocked(api.usage).mockReset();
});

describe("UsageView", () => {
  it("renders from a single aggregate response", async () => {
    vi.mocked(api.usage).mockResolvedValue(SAMPLE);
    show();
    expect(await screen.findByTestId("usage-view")).toBeInTheDocument();
    expect(screen.getByTestId("usage-identity")).toHaveTextContent("self-host");
    expect(screen.getAllByText("Graphban").length).toBeGreaterThan(0);
  });

  it("shows undeclared limit rows rather than a computed percentage", async () => {
    vi.mocked(api.usage).mockResolvedValue(SAMPLE);
    show();
    await screen.findByTestId("usage-view");
    expect(screen.getAllByText(/limit undeclared/i).length).toBeGreaterThan(0);
    expect(screen.queryByText(/100%/)).not.toBeInTheDocument();
  });

  it("names a failed fetch and does not show the empty-project copy", async () => {
    vi.mocked(api.usage).mockRejectedValue(new Error("boom"));
    show();
    expect(await screen.findByTestId("usage-error")).toBeInTheDocument();
    expect(screen.getByText(/request failed/i)).toBeInTheDocument();
    expect(screen.queryByText(/no projects you can read/i)).not.toBeInTheDocument();
  });
});
