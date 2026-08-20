import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { OrgTeams } from "@/features/orgadmin/OrgTeams";
import type { Team } from "@/lib/types";

/**
 * PRD-21 D5, screen 8.
 *
 * Two things this screen exists to say, and both are about a gap between what an admin
 * intends and what actually happens:
 *
 * 1. **"Granted" is not "somebody gained access."** A team with no members grants nothing,
 *    and a grant row that showed only the project would let that pass unnoticed.
 * 2. **A member with direct access keeps it through a revoke.** Not saying so makes a
 *    revoke look like it did more than it did.
 */
const user = (id: string, handle: string) => ({
  id, handle, name: handle[0].toUpperCase() + handle.slice(1),
  email: `${handle}@acme.dev`, avatar: "#c6f24e", initials: handle.slice(0, 2).toUpperCase(),
});

const team = (over: Partial<Team> = {}): Team => ({
  id: "tm_1", org_id: "org_1", name: "Platform", description: "",
  members: [user("u2", "dana"), user("u3", "ops")],
  grants: [{
    project_id: "prj_core", tag: "CORE", name: "Core", access: "read",
    derived_user_ids: ["u3"], direct_user_ids: ["u2"],
  }],
  ...over,
});

const { revokeSpy, grantSpy } = vi.hoisted(() => ({
  revokeSpy: vi.fn(async () => ({ affected: 2, kept_access: ["u2"] })),
  grantSpy: vi.fn(async () => ({})),
}));

vi.mock("@/lib/api", () => ({
  api: {
    orgs: vi.fn(async () => [{ id: "org_1", name: "Acme", plan: "team", role: "owner" }]),
    teams: vi.fn(async () => [team()]),
    orgMembers: vi.fn(async () => []),
    projects: vi.fn(async () => [
      { id: "prj_core", tag: "CORE", name: "Core", accent: "#c6f24e", visibility: "private",
        description: "", share_global_memory: false, auto_extract: true, mcp_enabled: true,
        embed_model: "", memory_auto_reject: true, memory_write_mode: "review",
        memory_llm_judge: false, agent_adjudication: false, allow_self_review: false },
    ]),
    revokeTeamGrant: revokeSpy,
    setTeamGrant: grantSpy,
    createTeam: vi.fn(async () => ({})),
    deleteTeam: vi.fn(async () => ({})),
    addTeamMember: vi.fn(async () => ({})),
    removeTeamMember: vi.fn(async () => ({})),
  },
}));

function renderTeams() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={qc}>
      <MemoryRouter initialEntries={["/org/admin/teams"]}>
        <OrgTeams />
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

describe("Teams", () => {
  beforeEach(async () => {
    const { api } = await import("@/lib/api");
    vi.mocked(api.teams).mockResolvedValue([team()]);
  });

  it("explains that a grant is written, not resolved later", async () => {
    renderTeams();
    expect(await screen.findByText(/written as real project access the moment you make it/))
      .toBeInTheDocument();
  });

  it("says how many people a grant actually reaches", async () => {
    // "Granted" and "somebody gained access" are different facts.
    renderTeams();
    expect(await screen.findByText(/grants access to 1 person/)).toBeInTheDocument();
  });

  it("says a grant reaches nobody when the team is empty", async () => {
    const { api } = await import("@/lib/api");
    vi.mocked(api.teams).mockResolvedValue([
      team({ members: [], grants: [{
        project_id: "prj_core", tag: "CORE", name: "Core", access: "read",
        derived_user_ids: [], direct_user_ids: [],
      }] }),
    ]);
    renderTeams();
    expect(await screen.findByText(/reaches nobody/)).toBeInTheDocument();
  });

  it("marks a member whose access is direct, and says a revoke will not remove it", async () => {
    renderTeams();
    // @dana appears twice on purpose — in the member list, and again in the grant's
    // direct-access note. The note is the one under test.
    const note = (await screen.findByText(/will not remove/)).closest("div")!;
    expect(within(note).getByText("@dana")).toBeInTheDocument();
    expect(within(note).getByText("direct")).toBeInTheDocument();
  });

  it("names who keeps access in the revoke confirmation", async () => {
    // A revoke that looked total, while one member kept theirs, would be a confident
    // wrong impression created by omission.
    renderTeams();
    await userEvent.click(await screen.findByRole("button", { name: "Revoke" }));
    const dialog = screen.getByText(/Revoke Platform's access to CORE\?/).closest("div")!;
    expect(within(dialog).getByText(/1 person loses/)).toBeInTheDocument();
    expect(within(dialog).getByText(/@dana keeps theirs/)).toBeInTheDocument();
  });

  it("says an empty team grants nothing rather than showing a bare zero", async () => {
    const { api } = await import("@/lib/api");
    vi.mocked(api.teams).mockResolvedValue([team({ grants: [] })]);
    renderTeams();
    expect(await screen.findByText(/being in a team is not itself access/)).toBeInTheDocument();
  });
});
