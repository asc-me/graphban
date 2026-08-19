import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { afterAll, beforeAll, describe, expect, it, vi } from "vitest";

import { AdminView } from "@/features/admin/AdminView";
import { OperatorHome } from "@/features/admin/OperatorHome";
import { OperatorLicensing } from "@/features/admin/OperatorLicensing";
import { OperatorOrgs } from "@/features/admin/OperatorOrgs";
import { OperatorUsers } from "@/features/admin/OperatorUsers";
import { relTime, shortDate } from "@/features/admin/parts";
import type { AdminInvite, AdminOrg, AdminUser } from "@/lib/types";

/**
 * PRD-21 screens 19–22 — the operator console.
 *
 * The assertions that matter here are the ones about *absence*: an org at its cap, a
 * user with no recorded write, an invite accepted but not yet spent on an org. Each is
 * a state whose reassuring reading ("fine", "idle", "done") is the wrong one, and each
 * is why this plane exists in the shape it does.
 */

const acme: AdminOrg = {
  id: "org_acme", name: "acme", plan: "team", created_at: "2026-02-14T00:00:00Z",
  owner_email: "dana@acme.dev", owner_name: "Dana Okafor", owner_handle: "dana",
  usage: { projects: 7, seats: 4, shards: 5715, calls_this_month: 28140 },
  limits: { max_projects: 50, max_seats: 100, max_shards: 100000, max_calls_per_month: 1000000 },
  members: [
    { handle: "dana", name: "Dana Okafor", role: "owner", joined_at: "2026-02-14T00:00:00Z" },
    { handle: "bo", name: "Bo Lindqvist", role: "admin", joined_at: "2026-03-01T00:00:00Z" },
  ],
};

// At its MCP cap: agent calls are being refused right now, which is a red state and not
// a "high usage" one.
const northbeam: AdminOrg = {
  id: "org_nb", name: "northbeam", plan: "pro", created_at: "2026-04-02T00:00:00Z",
  owner_email: "rui@northbeam.io", owner_name: "Rui Ferreira", owner_handle: "rui",
  usage: { projects: 4, seats: 3, shards: 9400, calls_this_month: 100000 },
  limits: { max_projects: 10, max_seats: 15, max_shards: 10000, max_calls_per_month: 100000 },
  members: [{ handle: "rui", name: "Rui Ferreira", role: "owner", joined_at: null }],
};

const users: AdminUser[] = [
  {
    id: "u1", name: "Dana Okafor", handle: "dana", email: "dana@acme.dev", created_at: null,
    org_count: 1, orgs: [{ id: "org_acme", name: "acme", role: "owner" }],
    last_write_at: new Date(Date.now() - 120_000).toISOString(),
  },
  {
    id: "u2", name: "Wren Castillo", handle: "wren", email: "wren@quietfox.dev", created_at: null,
    org_count: 0, orgs: [], last_write_at: null,
  },
];

const invites: AdminInvite[] = [
  {
    id: "inv_1", kind: "platform", org_id: null, email: "founder@relay.sh", role: "member",
    plan: "pro", status: "pending", created_at: "", expires_at: null,
    accept_url: "https://app.example/invite/tok1", invited_by_handle: "root",
    redeemed_org_id: "", redeemed_org_name: "", expired: false,
  },
  {
    id: "inv_2", kind: "platform", org_id: null, email: "juno@quietfox.dev", role: "member",
    plan: "free", status: "accepted", created_at: "", expires_at: null,
    accept_url: "https://app.example/invite/tok2", invited_by_handle: "root",
    redeemed_org_id: "org_qf", redeemed_org_name: "quiet-fox", expired: false,
  },
  // Redeemed, but the account has not founded anything — a different fact from both
  // "pending" and "quiet-fox", and it must not borrow either's rendering.
  {
    id: "inv_3", kind: "platform", org_id: null, email: "ceo@harbor.dev", role: "member",
    plan: null, status: "accepted", created_at: "", expires_at: null,
    accept_url: "https://app.example/invite/tok3", invited_by_handle: "root",
    redeemed_org_id: "", redeemed_org_name: "", expired: false,
  },
];

const { setPlanSpy, createInviteSpy } = vi.hoisted(() => ({
  setPlanSpy: vi.fn(async () => ({})),
  createInviteSpy: vi.fn(async () => ({})),
}));

vi.mock("@/lib/api", () => ({
  setActiveProjectId: vi.fn(),
  api: {
    adminWhoami: vi.fn(async () => ({
      is_platform_admin: true, email: "root@example.com",
      signup_mode: "invite_only", invite_expiry_days: 14,
    })),
    adminOrgs: vi.fn(async () => [acme, northbeam]),
    adminUsers: vi.fn(async () => users),
    adminInvites: vi.fn(async (history = false) =>
      history ? invites : invites.filter((i) => i.status === "pending"),
    ),
    adminActivity: vi.fn(async () => [
      {
        ts: new Date(Date.now() - 600_000).toISOString(), action: "set_org_plan",
        actor_label: "root", target_type: "org", target_id: "org_acme", meta: { plan: "team" },
      },
    ]),
    adminOrgRequests: vi.fn(async () => []),
    adminCreateInvite: createInviteSpy,
    adminRevokeInvite: vi.fn(async () => undefined),
    adminDecideOrgRequest: vi.fn(async () => ({})),
    setOrgPlan: setPlanSpy,
  },
}));

function renderAt(path: string) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={qc}>
      <MemoryRouter initialEntries={[path]}>
        <Routes>
          <Route path="/admin" element={<AdminView />}>
            <Route index element={<OperatorHome />} />
            <Route path="orgs" element={<OperatorOrgs />} />
            <Route path="users" element={<OperatorUsers />} />
            <Route path="licensing" element={<OperatorLicensing />} />
          </Route>
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

describe("Operator console — home", () => {
  it("sums the tenants and names the org that is at a cap", async () => {
    renderAt("/admin");

    expect(await screen.findByRole("heading", { name: "Platform" })).toBeInTheDocument();
    const seatTile = screen.getByText("SEATS TAKEN").closest("div")!.parentElement!;
    expect(seatTile).toHaveTextContent("7"); // 4 + 3, summed from the orgs' own counters
    // A seat is a member OR a pending invite. acme has 4 seats and 2 members, northbeam
    // 3 and 1 — so 4 of the 7 are reserved by invites nobody has accepted.
    expect(seatTile).toHaveTextContent("3 accepted · 4 reserved by pending invites");

    // At-cap replaces the throughput tile rather than sitting quietly beside it.
    expect(screen.getByText("ORGS AT A CAP")).toBeInTheDocument();
    expect(screen.queryByText("MCP CALLS / MO")).not.toBeInTheDocument();
    expect(screen.getByText(/northbeam.*at a cap/)).toBeInTheDocument();
    expect(screen.getByText(/mcp \/ mo/i)).toBeInTheDocument();
  });

  it("counts the person who belongs to no org at all", async () => {
    renderAt("/admin");
    expect(await screen.findByText(/1 in no org at all/)).toBeInTheDocument();
  });

  it("names the org a plan change landed on, not its id", async () => {
    renderAt("/admin");
    expect(await screen.findByText("acme assigned the team plan")).toBeInTheDocument();
  });

  it("says an empty ledger means no operator acted, not that the platform is quiet", async () => {
    const { api } = await import("@/lib/api");
    vi.mocked(api.adminActivity).mockResolvedValueOnce([]);
    renderAt("/admin");

    expect(await screen.findByText(/No operator has done anything/)).toBeInTheDocument();
    expect(screen.getByText(/tenant activity never appears here/)).toBeInTheDocument();
  });
});

describe("Operator console — orgs", () => {
  it("opens a tenant and assigns a plan, the only write on the screen", async () => {
    const user = userEvent.setup();
    renderAt("/admin/orgs");

    const row = (await screen.findByText("northbeam")).closest("div")!.parentElement!;
    await user.click(within(row).getByRole("button", { name: "OPEN" }));

    // Members are listed, and nothing in the drawer can change one. Two mentions of
    // @rui — the row's owner column and the drawer's member list — is the point: the
    // seat count and the member list are two reads of one fact and must agree.
    expect(screen.getByText(/MEMBERS · ALL 1/)).toBeInTheDocument();
    // northbeam: 3 seats, 1 member. The drawer says where the other two went instead of
    // leaving a member list that silently disagrees with the seat bar beside it.
    expect(screen.getByText(/The seat counter reads 3, not 1/)).toBeInTheDocument();
    expect(within(row).getAllByText("@rui")).toHaveLength(2);
    expect(screen.getByText(/listed but not editable/)).toBeInTheDocument();

    // The at-cap note explains that a bigger plan is the only lever.
    expect(screen.getByText(/A larger plan is the only lever/)).toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: "TEAM" }));
    expect(setPlanSpy).toHaveBeenCalledWith("org_nb", "team");
  });
});

describe("Operator console — users", () => {
  it("names the orgs a user is in rather than counting them", async () => {
    renderAt("/admin/users");
    const row = (await screen.findByText("Dana Okafor")).closest("div")!.parentElement!;
    expect(within(row).getByText("acme")).toBeInTheDocument();
    expect(within(row).getByText("owner")).toBeInTheDocument();
  });

  it("distinguishes 'no writes on record' from inactivity", async () => {
    renderAt("/admin/users");
    expect(await screen.findByText("no writes")).toBeInTheDocument();
    expect(screen.getByText("no org memberships")).toBeInTheDocument();
  });

  it("says nobody matched rather than rendering an empty table", async () => {
    const user = userEvent.setup();
    renderAt("/admin/users");
    await user.type(await screen.findByLabelText("Search users"), "zzz");
    expect(screen.getByText(/No user matches/)).toBeInTheDocument();
    expect(screen.getByText(/would still appear here/)).toBeInTheDocument();
  });
});

describe("Operator console — licensing", () => {
  it("reports the signup mode and expiry as deployment policy, not as controls", async () => {
    renderAt("/admin/licensing");
    expect(await screen.findByText("invite only")).toBeInTheDocument();
    expect(screen.getByText(/requires a platform invite/)).toBeInTheDocument();
    expect(screen.getByText(/reported here rather than editable/)).toBeInTheDocument();
  });

  it("hides redeemed invites until history is asked for, then keeps them", async () => {
    const user = userEvent.setup();
    renderAt("/admin/licensing");

    expect(await screen.findByText("founder@relay.sh")).toBeInTheDocument();
    expect(screen.queryByText("juno@quietfox.dev")).not.toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: "SHOW HISTORY" }));
    expect(await screen.findByText("juno@quietfox.dev")).toBeInTheDocument();
    expect(screen.getByText("quiet-fox")).toBeInTheDocument();
    expect(screen.getByText(/Redeemed invites are kept, not purged/)).toBeInTheDocument();
  });

  it("separates 'redeemed' from 'redeemed into an org'", async () => {
    const user = userEvent.setup();
    renderAt("/admin/licensing");
    await user.click(await screen.findByRole("button", { name: "SHOW HISTORY" }));

    const row = (await screen.findByText("ceo@harbor.dev")).closest("div")!.parentElement!;
    expect(within(row).getByText("accepted, no org founded")).toBeInTheDocument();
  });

  it("mints an invite with the chosen plan", async () => {
    const user = userEvent.setup();
    renderAt("/admin/licensing");

    await user.click(await screen.findByRole("button", { name: /Mint platform invite/ }));
    await user.type(screen.getByLabelText("Recipient email"), "new@company.dev");
    await user.click(screen.getByRole("button", { name: "PRO" }));
    await user.click(screen.getByRole("button", { name: "Mint invite" }));

    expect(createInviteSpy).toHaveBeenCalledWith({ email: "new@company.dev", plan: "pro" });
  });

  it("refuses to mint until the address is one", async () => {
    const user = userEvent.setup();
    renderAt("/admin/licensing");

    await user.click(await screen.findByRole("button", { name: /Mint platform invite/ }));
    await user.type(screen.getByLabelText("Recipient email"), "not-an-email");
    expect(screen.getByText("not a valid address")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Mint invite" })).toBeDisabled();
  });
});

describe("Operator console — timestamps", () => {
  // Pinned west of UTC on purpose. In a UTC runner a naive string and its Z-suffixed
  // twin parse identically, so the assertion below would pass whether or not the fix
  // is present — the exact shape of vacuous test this repo keeps catching.
  beforeAll(() => {
    vi.stubEnv("TZ", "America/Los_Angeles");
  });
  afterAll(() => {
    vi.unstubAllEnvs();
  });

  it("reads a zone-less server timestamp as UTC, not as local time", () => {
    // SQLite returns naive datetimes; JS parses those as local, which west of UTC puts
    // them in the future and collapses every age to "0s ago". A live hosted instance
    // showed exactly that.
    const naive = new Date(Date.now() - 3 * 3600_000).toISOString().replace("Z", "");
    expect(relTime(`${naive}Z`)).toBe("3h ago");
    expect(relTime(naive)).toBe("3h ago");
  });

  it("keeps a date on the day the server meant", () => {
    expect(shortDate("2026-08-19T23:30:00.000000")).toBe("2026-08-19");
  });
});

describe("Operator console — the plane itself", () => {
  it("discloses nothing to a caller the API refuses", async () => {
    const { api } = await import("@/lib/api");
    vi.mocked(api.adminWhoami).mockRejectedValueOnce(new Error("404"));
    renderAt("/admin");

    expect(await screen.findByText("This area is not available.")).toBeInTheDocument();
    expect(screen.queryByText("OPERATOR")).not.toBeInTheDocument();
  });

  it("states the plane's boundary in its own chrome", async () => {
    renderAt("/admin");
    expect(await screen.findByText("CROSS-TENANT · METADATA ONLY")).toBeInTheDocument();
    expect(screen.getByText(/no impersonation, no data edits/)).toBeInTheDocument();
  });
});
