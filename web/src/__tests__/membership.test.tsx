import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { describe, expect, it, vi } from "vitest";

import { OrgUsers } from "@/features/orgadmin/OrgUsers";
import type { OrgMember } from "@/lib/types";

/**
 * PRD-21 D8, from the screen's side.
 *
 * Most of this asserts a refusal, mirroring the server: the owner is immutable and nobody
 * edits themselves. The other half is the confirmation, which has to name what is lost —
 * removing someone cascades their project access, and a dialog that said only "are you
 * sure?" would hide the half of the action with consequences.
 */
const member = (
  id: string,
  name: string,
  role: OrgMember["role"],
  access: OrgMember["access"] = [],
): OrgMember => ({
  user: { id, name, handle: name.toLowerCase(), email: `${name.toLowerCase()}@acme.dev`,
          avatar: "#c6f24e", initials: name.slice(0, 2).toUpperCase() },
  role,
  access,
  last_write_at: null,
});

const MEMBERS: OrgMember[] = [
  member("u1", "Alex", "owner"),
  member("u2", "Dana", "admin", [
    { project_id: "prj_core", tag: "CORE", name: "Core", level: "write" },
    { project_id: "prj_web", tag: "WEB", name: "Web", level: "read" },
  ]),
  member("u3", "Ops", "member"),
];

const { setRoleSpy, removeSpy } = vi.hoisted(() => ({
  setRoleSpy: vi.fn(async () => ({})),
  removeSpy: vi.fn(async () => ({ removed_role: "member", projects_revoked: [] })),
}));

vi.mock("@/features/auth/AuthContext", () => ({
  // Signed in as Dana, the admin — so "you cannot edit yourself" is exercisable.
  useAuth: () => ({ user: { id: "u2", name: "Dana" }, loading: false }),
}));

vi.mock("@/lib/api", () => ({
  api: {
    orgs: vi.fn(async () => [{ id: "org_1", name: "Acme", plan: "team", role: "admin" }]),
    orgMembers: vi.fn(async () => MEMBERS),
    invites: vi.fn(async () => []),
    orgBilling: vi.fn(async () => ({
      plan: "team",
      limits: { max_projects: 50, max_seats: 100, max_shards: 1e5, max_calls_per_month: 1e6 },
      usage: { projects: 2, seats: 3, shards: 0, calls_this_month: 0 },
    })),
    setMemberRole: setRoleSpy,
    removeMember: removeSpy,
  },
}));

function renderUsers() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={qc}>
      <MemoryRouter initialEntries={["/org/admin/users"]}>
        {/* OrgUsers declares its own nested <Routes>, so its index route only matches
            beneath a parent that consumes the path prefix. */}
        <Routes>
          <Route path="/org/admin/users/*" element={<OrgUsers />} />
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

const rowOf = (handle: string) => screen.getByTestId(`member-${handle}`);

describe("Membership mutations", () => {
  it("changes a member's role", async () => {
    renderUsers();
    const select = await screen.findByLabelText("Role for ops");
    await userEvent.selectOptions(select, "admin");
    expect(setRoleSpy).toHaveBeenCalledWith("org_1", "u3", "admin");
  });

  it("refuses to let the owner be demoted or removed", async () => {
    renderUsers();
    await screen.findByLabelText("Role for ops");
    const row = rowOf("alex");
    expect(within(row).getByLabelText("Role for alex")).toBeDisabled();
    expect(within(row).getByRole("button", { name: "Remove" })).toBeDisabled();
  });

  it("refuses to let you edit yourself", async () => {
    // Signed in as Dana. An admin who can demote themselves is a support ticket, and one
    // who can promote themselves is not an admin.
    renderUsers();
    await screen.findByLabelText("Role for ops");
    const row = rowOf("dana");
    expect(within(row).getByLabelText("Role for dana")).toBeDisabled();
    expect(within(row).getByRole("button", { name: "Remove" })).toBeDisabled();
  });

  it("names the project access a removal will revoke", async () => {
    // The seat is the visible half; the access is the half that mattered.
    renderUsers();
    await screen.findByLabelText("Role for ops");
    await userEvent.click(within(rowOf("ops")).getByRole("button", { name: "Remove" }));

    expect(screen.getByText(/Remove Ops from this organization\?/)).toBeInTheDocument();
    expect(screen.getByText(/no project access, so nothing else changes/)).toBeInTheDocument();
  });

  it("lists the projects by name when there are some", async () => {
    // Dana cannot be removed (it is us), so the copy is asserted through Ops, given access.
    const { api } = await import("@/lib/api");
    vi.mocked(api.orgMembers).mockResolvedValue([
      MEMBERS[0],
      MEMBERS[1],
      member("u3", "Ops", "member", [
        { project_id: "prj_core", tag: "CORE", name: "Core", level: "write" },
      ]),
    ]);
    renderUsers();
    await screen.findByLabelText("Role for ops");
    await userEvent.click(within(rowOf("ops")).getByRole("button", { name: "Remove" }));

    const dialog = screen.getByText(/Remove Ops from this organization\?/).closest("div")!;
    expect(within(dialog).getByText("CORE")).toBeInTheDocument();
    expect(within(dialog).getByText(/is revoked with it/)).toBeInTheDocument();
  });

  it("no longer advertises the actions as unbuilt", async () => {
    renderUsers();
    await screen.findByLabelText("Role for ops");
    expect(screen.queryByText(/ACTIONS · NOT BACKED/)).not.toBeInTheDocument();
    expect(screen.queryByText(/specified as D8 in PRD-21/)).not.toBeInTheDocument();
  });
});
