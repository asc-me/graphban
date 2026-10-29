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
import { OutpostsView } from "@/features/fleet/OutpostsView";
import { HarnessView } from "@/features/harness/HarnessView";
import { ProjectHome } from "@/features/projecthome/ProjectHome";
import { ProjectProvider } from "@/features/ProjectContext";
import { PrdListView } from "@/features/prds/PrdListView";
import { RequestsView } from "@/features/requests/RequestsView";
import { RoadmapView } from "@/features/roadmap/RoadmapView";
import { TrackerView } from "@/features/tracker/TrackerView";
import { TriageView } from "@/features/triage/TriageView";

/**
 * Every endpoint these six views reach, all rejecting. Mocking the whole client rather
 * than a hook keeps the test honest about the thing under test: the view's branching, not
 * a stubbed `isError` flag.
 *
 * The factory is hoisted above every top-level binding, so `fail` is declared inside it.
 */
const FAILING_ENDPOINTS = [
  "dashboard", "counts", "events", "projectTriage", "fleet", "harness", "harnessProbeCandidates",
  "items", "shards", "codeMap", "prds", "orgs", "galaxy", "platform", "mcpTools",
  "links", "apiKeys", "roadmap", "requests",
];

vi.mock("@/lib/api", () => {
  const fail = () => Promise.reject(new Error("boom"));
  const endpoints = [
    "dashboard", "counts", "events", "projectTriage", "fleet", "harness", "harnessProbeCandidates",
    "items", "shards", "codeMap", "prds", "orgs", "galaxy", "platform", "mcpTools",
    "links", "apiKeys", "roadmap", "requests",
  ];
  return {
    setActiveProjectId: vi.fn(),
    api: {
      // The project list must succeed — without a project there is nothing to fail at.
      projects: vi.fn(async () => [
        { id: "core", name: "Core", tag: "GRPH", accent: "#c6f24e", description: "" },
      ]),
      config: vi.fn(async () => ({ hosted_mode: false })),
      ...Object.fromEntries(endpoints.map((k) => [k, vi.fn(fail)])),
    },
  };
});

/** Enough of a good read for the counts strip to show numbers rather than the empty project. */
const homeDash = {
  items_total: 4,
  items_by_status: { backlog: 1, next: 1, in_progress: 1, review: 1, done: 0, blocked: 0 },
  in_progress_count: 1, blocked_count: 0, requests_total: 1,
  requests_by_type: { bug: 1, feature: 0, enhancement: 0, feedback: 0 },
  shard_count: 2, prd_count: 1, mcp_calls: 0,
  recent_items: [{ id: "GRPH-1", title: "Ship the home", status: "in_progress" as const, date: "Oct 22" }],
};
const homeCounts = {
  items: 4, items_in_progress: 1, requests: 1, review: 2, triage_new: 1, lessons_published: 3,
};

function show(ui: React.ReactElement) {
  const qc = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  return Object.assign(
    render(
      <QueryClientProvider client={qc}>
        <MemoryRouter>
          <ProjectProvider>{ui}</ProjectProvider>
        </MemoryRouter>
      </QueryClientProvider>,
    ),
    { qc },
  );
}

/** The one-liner every surface shows when a request failed. */
const FAILED_COPY = /the request failed/i;
const STALE_BANNER = /stale — last good counts/i;

beforeEach(async () => {
  vi.clearAllMocks();
  const { api } = await import("@/lib/api");
  for (const key of FAILING_ENDPOINTS) {
    const fn = api[key as keyof typeof api] as ReturnType<typeof vi.fn>;
    fn.mockReset();
    fn.mockRejectedValue(new Error("boom"));
  }
});

describe("a failed fetch is not an empty project", () => {
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

  it("Triage names the failure instead of 'Triage clear'", async () => {
    show(<TriageView />);
    expect(await screen.findByText(FAILED_COPY)).toBeInTheDocument();
    expect(screen.queryByText(/triage clear/i)).not.toBeInTheDocument();
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
    expect(screen.getByRole("button", { name: /retry/i })).toBeInTheDocument();
    // Six counts, none of them readable, so none of them may render as 0.
    // A zero, or the empty-project title, would be the claim that the read happened.
    expect(screen.getAllByText("—").length).toBeGreaterThanOrEqual(6);
    expect(screen.queryByText("0")).not.toBeInTheDocument();
    expect(screen.queryByText("Nothing to summarise yet")).not.toBeInTheDocument();
    expect(screen.queryByText("not pushed")).not.toBeInTheDocument();
    // The strongest claim on this page is that no deployment has pushed a graph. It is
    // only true once the code-map read answered.
    expect(screen.queryByText(/has pushed a code graph/i)).not.toBeInTheDocument();
    // No good read preceded this failure, so the stale banner must not appear.
    expect(screen.queryByText(STALE_BANNER)).not.toBeInTheDocument();
  });

  it("Project Home does not banner a read that succeeded", async () => {
    const { api } = await import("@/lib/api");
    vi.mocked(api.dashboard).mockResolvedValue(homeDash as never);
    vi.mocked(api.counts).mockResolvedValue(homeCounts as never);
    vi.mocked(api.codeMap).mockResolvedValue({ node_count: 40 } as never);
    vi.mocked(api.fleet).mockResolvedValue({ online: 1 } as never);
    show(<ProjectHome />);
    expect(await screen.findByText("Items", { selector: "span" })).toBeInTheDocument();
    expect(screen.getByText("Items", { selector: "span" }).parentElement).toHaveTextContent("4");
    expect(screen.queryByText(STALE_BANNER)).not.toBeInTheDocument();
    expect(screen.queryByText(FAILED_COPY)).not.toBeInTheDocument();
  });

  it("Project Home keeps the last good counts and names them stale when a refresh fails", async () => {
    const { api } = await import("@/lib/api");
    vi.mocked(api.dashboard).mockResolvedValue(homeDash as never);
    vi.mocked(api.counts).mockResolvedValue(homeCounts as never);
    vi.mocked(api.codeMap).mockResolvedValue({ node_count: 40 } as never);
    vi.mocked(api.fleet).mockResolvedValue({ online: 1 } as never);
    const { qc } = show(<ProjectHome />);
    expect(await screen.findByText("Items", { selector: "span" })).toBeInTheDocument();
    expect(screen.getByText("Items", { selector: "span" }).parentElement).toHaveTextContent("4");

    vi.mocked(api.dashboard).mockRejectedValue(new Error("boom"));
    await qc.refetchQueries({ queryKey: ["dashboard", "core"] }).catch(() => undefined);

    expect(await screen.findByText(STALE_BANNER)).toBeInTheDocument();
    expect(screen.getByText("Items", { selector: "span" }).parentElement).toHaveTextContent("4");
    expect(screen.queryByText("—")).not.toBeInTheDocument();
  });
});

describe("Plan pages: a failed fetch shows the error card and no zero counts", () => {
  const cases: [string, React.ReactElement, RegExp, RegExp][] = [
    ["Tracker", <TrackerView />, /items unavailable/i, /no items yet/i],
    ["PRDs", <PrdListView />, /prds unavailable/i, /no prds yet/i],
    ["Requests", <RequestsView />, /requests unavailable/i, /no requests yet/i],
    ["Roadmap", <RoadmapView />, /roadmap unavailable/i, /no milestones/i],
  ];
  it.each(cases)("%s", async (_name, ui, title, emptyCopy) => {
    show(ui);
    expect(await screen.findByText(title)).toBeInTheDocument();
    expect(screen.getByText(/nothing here means empty/i)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Retry" })).toBeInTheDocument();
    expect(screen.queryByText(emptyCopy)).not.toBeInTheDocument();
    // No view tabs, no chip row, so no zero count anywhere.
    expect(screen.queryByRole("tablist", { name: "Views" })).not.toBeInTheDocument();
    expect(screen.queryByText("0")).not.toBeInTheDocument();
  });
});
