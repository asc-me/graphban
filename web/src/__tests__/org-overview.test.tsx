import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { describe, expect, it, vi } from "vitest";

import type { OrgOverview } from "@/lib/types";

/**
 * PRD-21 D2 — the org overview.
 *
 * The assertions here are about *absence being legible*. A project that has never synced
 * and a project whose box stopped pushing are different facts; an org with no projects and
 * an org whose projects are all empty are different facts. Rendering either pair the same
 * way is the failure this product exists to notice, so each is asserted separately.
 */
const base: OrgOverview = {
  org_id: "org_acme",
  plan: "team",
  projects: [],
  totals: { projects: 0, open_items: 0, claims: 0, nodes: 0, never_synced: 0 },
  usage: { projects: 0, seats: 1, shards: 0, calls_this_month: 0 },
  limits: { max_projects: 50, max_seats: 100, max_shards: 100000, max_calls_per_month: 1000000 },
};

describe("the org overview", () => {
  it("tells a brand-new org the one thing worth telling it", async () => {
    vi.resetModules();
    vi.doMock("@/lib/queries", () => ({
      useOrgs: () => ({ data: [{ id: "org_acme", name: "Acme" }] }),
      useOrgOverview: () => ({ data: base, isLoading: false }),
    }));
    const { OrgOverviewView: V } = await import("@/features/orgadmin/OrgOverviewView");
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    render(
      <QueryClientProvider client={client}>
        <MemoryRouter><V /></MemoryRouter>
      </QueryClientProvider>,
    );
    // Not a table of zeroes — a next action.
    expect(screen.getByText(/Link your first deployment/i)).toBeTruthy();
    expect(screen.queryByText(/Graph nodes/i)).toBeNull();
  });

  it("shows a never-synced project rather than filtering it out, and says which kind of empty", async () => {
    vi.resetModules();
    const data: OrgOverview = {
      ...base,
      projects: [
        {
          id: "p1", tag: "GRPH", name: "Graphban", accent: "#c6f24e",
          items: { backlog: 3, next: 0, in_progress: 1, review: 0, done: 9, blocked: 0 },
          open_items: 4, claims: [], nodes: 0, last_push_at: null, sync: "never",
        },
      ],
      totals: { projects: 1, open_items: 4, claims: 0, nodes: 0, never_synced: 1 },
    };
    vi.doMock("@/lib/queries", () => ({
      useOrgs: () => ({ data: [{ id: "org_acme", name: "Acme" }] }),
      useOrgOverview: () => ({ data, isLoading: false }),
    }));
    const { OrgOverviewView: V } = await import("@/features/orgadmin/OrgOverviewView");
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    render(
      <QueryClientProvider client={client}>
        <MemoryRouter><V /></MemoryRouter>
      </QueryClientProvider>,
    );

    expect(screen.getByText("Graphban")).toBeTruthy();
    // Two places say it, and both should: the row's pill and the org-level count. The
    // pill is what stops the row reading as healthy; the count is what stops the org
    // reading as fully wired.
    expect(screen.getAllByText(/never synced/i).length).toBe(2);
    // Its work is real even with no graph: a never-synced project is not an empty one,
    // and reading "0 open" here would be the wrong answer rather than a missing one.
    expect(screen.getByText("4 open")).toBeTruthy();
    expect(screen.getByText(/1 never synced/i)).toBeTruthy();
  });
});
