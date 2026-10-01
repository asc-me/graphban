import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, within } from "@testing-library/react";
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
  model_usage: {
    rows: [
      // Fully reported and priced.
      { vendor: "anthropic", model: "claude-sonnet-4", spawns: 4, tokens: 120000, tokens_reported: 4, cost_usd: 1.35 },
      // Local compute: a REAL zero, not an unknown one.
      { vendor: "gbagent", model: "qwen3-8b", spawns: 3, tokens: 50000, tokens_reported: 3, cost_usd: 0 },
      // Partial: 2 of 10 reported, so the sum rests on a fraction of the spawns.
      { vendor: "alibaba", model: "qwen3.8-max", spawns: 10, tokens: 8000, tokens_reported: 2, cost_usd: 0.02 },
      // Nothing reported and nothing priced.
      { vendor: "cursor", model: "undeclared", spawns: 5, tokens: null, tokens_reported: 0, cost_usd: null },
    ],
    spawns: 22,
    tokens_reported: 9,
    note: "9 of 22 attempts in this window reported tokens; the rest are shown as not reported rather than as zero.",
  },
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
  // `HarnessLink` prefers the last-used project tag; keep that hint from leaking between tests.
  localStorage.removeItem("gb_last_project_tag");
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

describe("UsageView · Model usage", () => {
  it("renders harness, model, spawns and tokens per attempt record", async () => {
    vi.mocked(api.usage).mockResolvedValue(SAMPLE);
    show();
    await screen.findByTestId("usage-view");

    const panel = screen.getByTestId("usage-models");
    expect(screen.getByText("Model usage")).toBeInTheDocument();
    expect(within(panel).getByText("Harness · Model")).toBeInTheDocument();
    expect(within(panel).getByText("Spawns")).toBeInTheDocument();
    expect(within(panel).getByText("Tokens")).toBeInTheDocument();

    const row = screen.getByTestId("usage-model-row-anthropic");
    expect(within(row).getByText("anthropic")).toBeInTheDocument();
    expect(within(row).getByText("claude-sonnet-4")).toBeInTheDocument();
    expect(within(row).getByText("4")).toBeInTheDocument();
    expect(within(row).getByText("120,000")).toBeInTheDocument();
  });

  it("labels cost an estimate and shows $0 for local models rather than blank", async () => {
    vi.mocked(api.usage).mockResolvedValue(SAMPLE);
    show();
    await screen.findByTestId("usage-view");

    expect(screen.getByText("Est. cost")).toBeInTheDocument();
    // D3: the design's footnote verbatim — it is what makes the column an estimate.
    expect(
      screen.getByText(
        "Cost is estimated from list prices of the tokens agents reported. Local models (gbagent) show $0.",
      ),
    ).toBeInTheDocument();

    const local = screen.getByTestId("usage-model-row-gbagent");
    expect(within(local).getByText("$0.00")).toBeInTheDocument();
    // A priced cloud model still gets a number, so $0 above is a claim and not a fallback.
    expect(within(screen.getByTestId("usage-model-row-anthropic")).getByText("$1.35")).toBeInTheDocument();
  });

  it("reads a model with no reported tokens as not reported, never as zero", async () => {
    vi.mocked(api.usage).mockResolvedValue(SAMPLE);
    show();
    await screen.findByTestId("usage-view");

    const row = screen.getByTestId("usage-model-row-cursor");
    expect(within(row).getByText("not reported")).toBeInTheDocument();
    expect(
      within(row).getByText(/5 spawns in this window, none carried a token count/),
    ).toBeInTheDocument();
    expect(within(row).getByText("not priced")).toBeInTheDocument();
    // The whole point: no zero anywhere in a row nobody measured.
    expect(within(row).queryByText("0")).not.toBeInTheDocument();
    expect(within(row).queryByText("$0.00")).not.toBeInTheDocument();

    // And the panel says how much of the window the numbers rest on.
    expect(screen.getByTestId("usage-models-note")).toHaveTextContent("9 of 22");
  });

  it("shows the count a partial token sum rests on", async () => {
    vi.mocked(api.usage).mockResolvedValue(SAMPLE);
    show();
    await screen.findByTestId("usage-view");

    const partial = screen.getByTestId("usage-model-row-alibaba");
    expect(within(partial).getByText("2/10 reported")).toBeInTheDocument();
    // A fully-reported row does not carry the qualifier.
    expect(
      within(screen.getByTestId("usage-model-row-anthropic")).queryByText(/reported$/),
    ).not.toBeInTheDocument();
  });

  it("uses PlannerEmpty for a window with no attempts, not a bare panel", async () => {
    vi.mocked(api.usage).mockResolvedValue({
      ...SAMPLE,
      model_usage: { rows: [], spawns: 0, tokens_reported: 0, note: null },
    });
    show();
    await screen.findByTestId("usage-view");

    expect(screen.getByText("No attempts in this window")).toBeInTheDocument();
    expect(screen.getByText(/not a deployment whose agents cost nothing/)).toBeInTheDocument();
    // No table behind the copy — an empty panel is the absence-reads-as-clean failure.
    expect(screen.queryByTestId("usage-models")).not.toBeInTheDocument();
  });

  it("draws no model panel at all when the fetch fails", async () => {
    vi.mocked(api.usage).mockRejectedValue(new Error("boom"));
    show();
    expect(await screen.findByTestId("usage-error")).toBeInTheDocument();
    expect(screen.getByText(/request failed/i)).toBeInTheDocument();
    // A1's second half: the empty-state title must not render on failure.
    expect(screen.queryByText("No attempts in this window")).not.toBeInTheDocument();
    expect(screen.queryByTestId("usage-model-usage")).not.toBeInTheDocument();
  });

  it("links to Harness on a project the caller can read", async () => {
    vi.mocked(api.usage).mockResolvedValue(SAMPLE);
    show();
    await screen.findByTestId("usage-view");

    const link = screen.getByTestId("usage-models-harness-link");
    expect(link).toHaveTextContent("Performance on Harness");
    expect(link).toHaveAttribute("href", "/p/GRPH/harness");
  });

  it("omits the Harness link when there is no readable project to link to", async () => {
    vi.mocked(api.usage).mockResolvedValue({ ...SAMPLE, by_project: [] });
    show();
    await screen.findByTestId("usage-view");

    expect(screen.queryByTestId("usage-models-harness-link")).not.toBeInTheDocument();
  });
});
