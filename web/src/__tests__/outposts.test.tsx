import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { describe, expect, it, vi } from "vitest";

import { OutpostsView } from "@/features/fleet/OutpostsView";
import { groupOutposts, outpostHost } from "@/features/fleet/outposts";
import type { FleetAgent } from "@/lib/types";

const fleet = vi.hoisted(() => ({ data: null as unknown, isLoading: false }));
vi.mock("@/lib/queries", () => ({
  useFleet: () => fleet,
  useConfig: () => ({ data: { hosted_mode: false } }),
}));
vi.mock("@/features/ProjectContext", () => ({
  useProjectCtx: () => ({ active: { id: "core", name: "Core", tag: "CORE" },
                          activeId: "core", projects: [] }),
}));

function agent(partial: Partial<FleetAgent>): FleetAgent {
  return {
    id: "A1", key: "GRPH-A1", label: "", active_role: "worker", state: "working",
    capabilities: {}, credential: null, credential_posture: null, enrolment_id: null,
    enrolled: true, dismissed: false, worktree: "", branch: "", branch_orphaned: false,
    last_seen_at: null, holdings: [],
    ...partial,
  };
}

describe("groupOutposts", () => {
  it("groups by capabilities.host, then label @host, and keeps unspecified as its own group", () => {
    const posts = groupOutposts([
      agent({ id: "1", key: "A1", capabilities: { host: "box-a", vendor: "claude", model: "opus" } }),
      agent({ id: "2", key: "A2", label: "grok @ box-a:wt" }),
      agent({ id: "3", key: "A3", label: "lonely" }),
    ]);
    expect(posts.map((p) => p.host)).toEqual(["box-a", "unspecified"]);
    expect(posts[0].agents.map((a) => a.id)).toEqual(["1", "2"]);
    expect(posts[1].specified).toBe(false);
  });

  it("does not invent localhost when host is missing", () => {
    expect(outpostHost(agent({ label: "opus" }))).toBe("");
  });
});

describe("OutpostsView", () => {
  it("says nobody has registered rather than listing zero outposts", () => {
    fleet.data = { agents: [] };
    render(
      <QueryClientProvider client={new QueryClient()}>
        <MemoryRouter>
          <OutpostsView />
        </MemoryRouter>
      </QueryClientProvider>,
    );
    expect(screen.getByTestId("outposts-empty")).toHaveTextContent(/No host has registered/);
  });

  it("renders declared build info for a host", () => {
    fleet.data = {
      agents: [agent({
        label: "opus @ macbook:wt-2",
        capabilities: { host: "macbook", vendor: "claude", model: "opus", tier: "frontier", os: "darwin" },
        worktree: "~/wt-2", branch: "feat/x",
      })],
    };
    render(
      <QueryClientProvider client={new QueryClient()}>
        <MemoryRouter>
          <OutpostsView />
        </MemoryRouter>
      </QueryClientProvider>,
    );
    const card = screen.getByTestId("outpost-card");
    expect(card).toHaveTextContent("macbook");
    expect(card).toHaveTextContent("claude");
    expect(card).toHaveTextContent("opus");
    expect(card).toHaveTextContent("darwin");
    expect(card).toHaveTextContent("feat/x");
  });
});
