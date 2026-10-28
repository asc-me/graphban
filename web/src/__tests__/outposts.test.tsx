import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { describe, expect, it, vi } from "vitest";

import { OutpostsView } from "@/features/fleet/OutpostsView";
import { groupOutposts, hostHint, outpostAgent, outpostHost } from "@/features/fleet/outposts";
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
  function show() {
    return render(
      <QueryClientProvider client={new QueryClient()}>
        <MemoryRouter>
          <OutpostsView />
        </MemoryRouter>
      </QueryClientProvider>,
    );
  }

  it("says nobody has registered rather than listing zero outposts", () => {
    fleet.data = { agents: [] };
    render(
      <QueryClientProvider client={new QueryClient()}>
        <MemoryRouter>
          <OutpostsView />
        </MemoryRouter>
      </QueryClientProvider>,
    );
    expect(screen.getByTestId("outposts-empty")).toHaveTextContent(/No host has checked in/);
    expect(screen.getByTestId("outposts-empty")).toHaveTextContent(/not a list of zero machines/i);
    expect(screen.getByRole("link", { name: "Fleet catalog" })).toHaveAttribute("href", "/fleet?tab=harness");
  });

  // ---- PRD-47 S7 (GRPH-958) ------------------------------------------------------------------
  //
  // The slice is mostly COPY, and copy is what nothing was holding. This work was recovered from
  // an abandoned wave branch where the child never committed it; the version of GRPH-955
  // recovered the same way was bounced precisely because "removing any of these leaves the suite
  // green". These four assertions are the difference between shipping the slice and shipping a
  // file that happens to contain some sentences today.

  it("defines what a host IS, rather than leaving the grouping to be guessed", () => {
    fleet.data = { agents: [agent({ id: "1", key: "A1", capabilities: { host: "box-a" } })] };
    show();

    // The footnote is the whole reason `unspecified` is a named group and not a silent
    // localhost: without it a reader has no way to know the grouping was a decision.
    const body = document.body;
    expect(body).toHaveTextContent(/registered an agent with a host capability/i);
    expect(body).toHaveTextContent(/"Unspecified" is a real group, never silently localhost/i);
  });

  it("says tool versions are only what the host last declared", () => {
    fleet.data = { agents: [agent({ id: "1", key: "A1", capabilities: { host: "box-a" } })] };
    show();

    // Not measured, not fetched — declared. A version shown without that caveat reads as
    // something the server checked.
    expect(document.body).toHaveTextContent(/what the host last declared/i);
  });

  it("names the three tools and which machines need which", async () => {
    fleet.data = { agents: [] };
    show();
    await userEvent.setup().click(screen.getByRole("button", { name: /Set up a host/i }));

    expect(document.body).toHaveTextContent(/Three tools, one machine/i);
    // gbagent ships with gbfleet — the sentence exists so nobody installs a third thing.
    expect(document.body).toHaveTextContent(/gbfleet/);
    expect(document.body).toHaveTextContent(/gbagent/);
    expect(document.body).toHaveTextContent(/ships with gbfleet/);
  });

  it("offers the set-up steps a machine with no outpost actually needs", async () => {
    fleet.data = { agents: [] };
    show();
    await userEvent.setup().click(screen.getByRole("tab", { name: /Set up a host/ }));

    expect(document.body).toHaveTextContent(/Set up this machine/i);
    expect(document.body).toHaveTextContent("uv tool install graphban-cli");
    expect(document.body).toHaveTextContent("gban login --server");
    expect(document.body).toHaveTextContent("uv tool install graphban-fleet");
    expect(document.body).not.toHaveTextContent("npm install");
    expect(document.body).not.toHaveTextContent("gban adapter install");
    expect(document.body).not.toHaveTextContent("gban host set");
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
    expect(card).toHaveTextContent("gbfleet");
  });

  it("derives via from enrolment: a seat is gbfleet, a key is gban", () => {
    expect(outpostAgent(agent({ enrolled: true })).via).toBe("gbfleet");
    expect(outpostAgent(agent({ enrolled: false })).via).toBe("gban");
  });

  it("hostHint is null when every agent on a specified host is up", () => {
    const [post] = groupOutposts([
      agent({ id: "1", key: "A1", state: "working", capabilities: { host: "box-a" } }),
    ]);
    expect(hostHint(post)).toBeNull();
  });

  it("shows a fix hint for an offline host and hides it when everyone is up", () => {
    fleet.data = {
      agents: [agent({
        id: "1", key: "A1", state: "offline",
        capabilities: { host: "box-a" }, branch: "wave-6/grph-862",
      })],
    };
    const { unmount } = show();
    expect(screen.getByTestId("fix-hint")).toHaveTextContent(/Offline while holding/);
    expect(screen.getByRole("link", { name: /Open on Fleet/ })).toHaveAttribute("href", "/fleet");
    unmount();

    fleet.data = {
      agents: [agent({ id: "1", key: "A1", state: "working", capabilities: { host: "box-a" } })],
    };
    show();
    expect(screen.queryByTestId("fix-hint")).not.toBeInTheDocument();
  });
});
