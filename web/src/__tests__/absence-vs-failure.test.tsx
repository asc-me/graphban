/**
 * PRD-47 S1 / A1 — a failed fetch must never render as an empty project.
 *
 * This is the repository's most-repeated defect (`absence-reads-as-clean`): a view whose
 * only branch is `isLoading || !data` falls through to its empty state when the request
 * fails, so a server that never answered tells you your project has nothing in it. Seven
 * views used `PlannerStates`; thirteen did not.
 *
 * Every case here asserts BOTH halves, because only the second one catches the defect:
 *
 *   1. the failure is named and offers a way out (Retry), and
 *   2. the surface's own "nothing here yet" copy is ABSENT.
 *
 * A test that only checked (1) would pass against a view that rendered the error banner
 * *and* the empty state — which is how you get a screen saying "no agents yet" underneath
 * "the request failed". The reviewer sabotage for this file is to delete an `isError`
 * branch from any view below and watch its own case fail on (2), not on (1).
 */
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen } from "@testing-library/react";
import * as React from "react";
import { MemoryRouter } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { ActivityView } from "@/features/activity/ActivityView";
import { DashboardView } from "@/features/dashboard/DashboardView";
import { OutpostsView } from "@/features/fleet/OutpostsView";
import { HarnessView } from "@/features/harness/HarnessView";
import { ProjectHome } from "@/features/projecthome/ProjectHome";
import { ProjectProvider } from "@/features/ProjectContext";
import { TriageView } from "@/features/triage/TriageView";

/**
 * Every endpoint these six views reach, all rejecting. Mocking the whole client rather
 * than a hook keeps the test honest about the thing under test: the view's branching, not
 * a stubbed `isError` flag.
 *
 * The factory is hoisted above every top-level binding, so `fail` is declared inside it.
 */
vi.mock("@/lib/api", () => {
  const fail = () => Promise.reject(new Error("boom"));
  const endpoints = [
    "dashboard", "events", "triageQueue", "fleet", "harness", "harnessProbeCandidates",
    "items", "shards", "codeMap", "prds", "orgs", "galaxy", "platform", "mcpTools",
    "links", "apiKeys",
  ];
  return {
    setActiveProjectId: vi.fn(),
    api: {
      // The project list must succeed — without a project there is nothing to fail at.
      projects: vi.fn(async () => [
        { id: "core", name: "Core", tag: "GRPH", accent: "#c6f24e", description: "" },
      ]),
      config: vi.fn(async () => ({ hosted_mode: false })),
      ...Object.fromEntries(endpoints.map((k) => [k, fail])),
    },
  };
});

function show(ui: React.ReactElement) {
  const qc = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  return render(
    <QueryClientProvider client={qc}>
      <MemoryRouter>
        <ProjectProvider>{ui}</ProjectProvider>
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

/** The one-liner every surface shows when a request failed. */
const FAILED_COPY = /the request failed/i;

beforeEach(() => {
  vi.clearAllMocks();
});

describe("a failed fetch is not an empty project", () => {
  it("Dashboard names the failure instead of drawing zeroed KPIs", async () => {
    show(<DashboardView />);
    expect(await screen.findByText(FAILED_COPY)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /retry/i })).toBeInTheDocument();
    // The KPI labels are the tell: a zeroed strip is a claim about the project.
    expect(screen.queryByText("Memory shards")).not.toBeInTheDocument();
    expect(screen.queryByText("In progress")).not.toBeInTheDocument();
  });

  it("Activity names the failure instead of 'No activity yet'", async () => {
    show(<ActivityView />);
    expect(await screen.findByText(FAILED_COPY)).toBeInTheDocument();
    expect(screen.queryByText(/no activity yet/i)).not.toBeInTheDocument();
    expect(screen.queryByText(/nothing in this selection/i)).not.toBeInTheDocument();
    // Both of these used to assert the absence of the word "EVENTS", which the S10
    // rebuild removed from the header entirely — so the assertion passed against a view
    // that rendered the empty state under the failure, which is the exact defect this
    // file exists to catch. These name things the success path DOES render: the header's
    // "N of M recorded" count (a fabricated zero on a failed read) and the lens strip.
    expect(screen.queryByText(/of .* recorded/i)).not.toBeInTheDocument();
    expect(screen.queryByRole("group", { name: "Activity lenses" })).not.toBeInTheDocument();
  });

  it("Triage names both failures instead of 'Queue is empty' and 'Nothing in flight'", async () => {
    show(<TriageView />);
    const failures = await screen.findAllByText(FAILED_COPY);
    // Two independent reads fail independently: the queue and the fleet.
    expect(failures.length).toBe(2);
    expect(screen.queryByText(/queue is empty/i)).not.toBeInTheDocument();
    expect(screen.queryByText(/nothing in flight/i)).not.toBeInTheDocument();
    expect(screen.queryByText(/no overlaps in flight/i)).not.toBeInTheDocument();
  });

  it("Harness says the catalog was not served, not that nothing is measured", async () => {
    show(<HarnessView />);
    expect(await screen.findByText(/the catalog has not been served/i)).toBeInTheDocument();
    expect(screen.queryByText(/nothing measured yet/i)).not.toBeInTheDocument();
  });

  it("Outposts names the failure instead of 'no host has registered'", async () => {
    show(<OutpostsView />);
    expect(await screen.findByText(FAILED_COPY)).toBeInTheDocument();
    expect(screen.queryByText(/no host has registered/i)).not.toBeInTheDocument();
  });

  it("Project Home shows unread counts as — and withholds the no-code-graph claim", async () => {
    show(<ProjectHome />);
    expect(await screen.findByText(FAILED_COPY)).toBeInTheDocument();
    // Six counts, none of them readable, so none of them may render as 0.
    expect(screen.getAllByText("—").length).toBeGreaterThanOrEqual(6);
    expect(screen.queryByText("0")).not.toBeInTheDocument();
    // The strongest claim on this page is that no deployment has pushed a graph. It is
    // only true once the code-map read answered.
    expect(screen.queryByText(/has pushed a code graph/i)).not.toBeInTheDocument();
  });
});
