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
import { CodeGraphView } from "@/features/code/CodeGraphView";
import { DashboardView } from "@/features/dashboard/DashboardView";
import { OutpostsView } from "@/features/fleet/OutpostsView";
import { HarnessView } from "@/features/harness/HarnessView";
import { ProjectHome } from "@/features/projecthome/ProjectHome";
import { ProjectProvider } from "@/features/ProjectContext";
import { TriageView } from "@/features/triage/TriageView";
import type { FleetPresence } from "@/lib/types";

/**
 * Set by the last case below to let ONE read succeed while the code-map read fails. `null`
 * keeps the default reject for every view above, so this widens the harness without changing
 * what the other six assert.
 */
const fleetRead = vi.hoisted(() => ({ current: null as FleetPresence | null }));

/**
 * Every endpoint these seven views reach, all rejecting. Mocking the whole client rather
 * than a hook keeps the test honest about the thing under test: the view's branching, not
 * a stubbed `isError` flag.
 *
 * The factory is hoisted above every top-level binding, so `fail` is declared inside it.
 */
vi.mock("@/lib/api", () => {
  const fail = () => Promise.reject(new Error("boom"));
  const endpoints = [
    "dashboard", "events", "triageQueue", "fleet", "fleetPresence", "harness",
    "harnessProbeCandidates", "items", "shards", "codeMap", "prds", "orgs", "galaxy",
    "platform", "mcpTools", "links", "apiKeys",
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
      // `fleetPresence` (not `fleet`) is what the code graph's legend reads. Overridden after
      // the spread so the last case can let THAT one read succeed; `null` keeps the reject.
      fleetPresence: () =>
        fleetRead.current
          ? Promise.resolve(fleetRead.current)
          : Promise.reject(new Error("boom")),
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

/**
 * One human holding one node. `FleetLegend` returns `null` for a presence with nobody in it,
 * so a case that wants to see the legend has to give it a holder — which is also why the
 * all-fail harness above cannot make the legend's gating observable.
 */
const ONE_HOLDER: FleetPresence = {
  served_at: "2026-10-03T00:00:00Z",
  heartbeat_interval_seconds: 50,
  held: [{
    area: "web/src/features/code/",
    item_id: "GRPH-1",
    expires_at: null,
    agent_id: "GRPH-A1",
    agent_label: "a @ host",
    active_role: "worker",
    state: "building",
    user_id: "u1",
    user_initials: "AL",
    user_color: "#c6f24e",
    node_paths: ["web/src/features/code/CodeGraphView.tsx"],
  }],
  off_map: [],
  truncated: false,
  total: 1,
};

beforeEach(() => {
  vi.clearAllMocks();
  fleetRead.current = null;
  // jsdom has no layout, so the code chat's mount-time autoscroll finds no `scrollTo` on its
  // node — the same stub `graph-accessible-name.test.tsx` and `tracker.test.tsx` install for
  // the same reason. Only the code-graph case reaches it; defining it when already present is
  // a no-op, so the six views above are unaffected.
  Element.prototype.scrollTo = Element.prototype.scrollTo ?? (() => {});
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

  it("Code graph names the failure instead of 'No code described yet'", async () => {
    // GRPH-944. This view's empty state is the strongest claim in the file: it does not just
    // say there is nothing here, it explains WHY — "Empty because nothing has been described,
    // not because this project has no structure" — and then tells you a coding agent fixes it
    // by calling `describe_code`. Every word of that is false when `/api/agent/code/map`
    // returned 500, and it sends the reader off to describe a codebase the server already has.
    // The guard is `const empty = !isLoading && !failed && nodes.length === 0` plus the
    // `failed ?` branch rendering `PlannerError`; this is the case that goes red if either
    // half is deleted. It shipped in the same commit as the other six and was the one view
    // left uncovered.
    show(<CodeGraphView />);
    expect(await screen.findByText(FAILED_COPY)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /retry/i })).toBeInTheDocument();
    expect(screen.queryByText(/no code described yet/i)).not.toBeInTheDocument();
    // The explanation and the invitation, not just the heading: a view could drop the title
    // and keep asserting that nothing was described.
    expect(
      screen.queryByText(/nothing has been described/i),
    ).not.toBeInTheDocument();
    expect(screen.queryByText(/describe_code/i)).not.toBeInTheDocument();
    // The gesture hint teaches Hover/Click/Drag on a graph that is not there. It is gated by
    // its OWN `!failed`, not by `empty` — on a failure `empty` is false, so every other term in
    // that condition passes and `!failed` is the only thing holding the hint back. Without this
    // assertion, deleting that term is invisible.
    expect(
      screen.queryByRole("button", { name: "Dismiss hint" }),
    ).not.toBeInTheDocument();
  });

  it("Code graph keeps the fleet it could read when the map read failed", async () => {
    // The other half of `empty`'s `!failed`, and the only place it is observable. The empty
    // state itself is guarded twice — `failed ?` renders `PlannerError` before `empty` is ever
    // consulted — so deleting `!failed` from `const empty = !isLoading && !failed && …` changes
    // nothing about the copy and the case above stays green. What it does change is this: the
    // legend is gated on `!empty` alone, so without `!failed` a map read that returned 500
    // would also erase a presence read that SUCCEEDED. That is the same defect wearing a
    // different surface — an absence presented as the answer — so it gets its own case rather
    // than being left to a guard no test can reach.
    //
    // The hint one branch down carries its own `!failed`, which is the tell that this split is
    // deliberate: a gesture hint is nonsense over a graph that did not load, while "who is
    // working here" stays true regardless.
    fleetRead.current = ONE_HOLDER;
    show(<CodeGraphView />);
    expect(await screen.findByText(FAILED_COPY)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /^fleet/i })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /1a · 1n/ })).toBeInTheDocument();
    expect(screen.queryByText(/no code described yet/i)).not.toBeInTheDocument();
  });
});
