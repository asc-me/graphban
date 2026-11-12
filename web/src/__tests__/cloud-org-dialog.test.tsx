import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import type { ReactNode } from "react";
import { MemoryRouter, Route, Routes, useLocation } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { LeftNav } from "@/components/shell/LeftNav";
import { CloudOrgLinkDialog } from "@/features/settings/CloudOrgLinkDialog";
import { ProjectProvider } from "@/features/ProjectContext";
import { ORG_SIGNUP_URL, settingsPath } from "@/lib/routes";
import { requestOrgWindow } from "@/features/settings/CloudOrgLinkDialog";
import { LINK_FINISHED_KEY } from "@/features/organization/LinkToOrgWizard";
import type { SyncOrgOverview, SyncProjectState, SyncStatus } from "@/lib/types";

const project = {
  id: "core", tag: "CORE", name: "Core", accent: "#c6f24e", visibility: "private",
  description: "", share_global_memory: false, auto_extract: true, mcp_enabled: true,
  embed_model: "", credential_id: null, model_override: "",
  memory_auto_reject: true, memory_write_mode: "review", memory_llm_judge: false,
  agent_adjudication: false, allow_self_review: false,
};

const unlinked: SyncStatus = {
  linked: false, source: "", cloud_url: "", org: "", credential_set: false, linked_at: null,
  projects: [],
};

const malformed: SyncStatus = {
  linked: false, source: "web", cloud_url: "not-a-url", org: "", credential_set: false, linked_at: null,
  projects: [],
};

const proj = (id: string, name: string, extra: Partial<SyncProjectState> = {}): SyncProjectState => ({
  project_id: id, name, writable: true, sync_graph: true, total_nodes: 1, synced_nodes: 1,
  pending: 0, last_synced_at: null, status: "live", mapped: false, ...extra,
});

const PUSHED_AT = "2026-11-12T14:02:00Z";

/** Signed in, core → CORE pushed, two projects local only: the Linked window (resume → null). */
const linked: SyncStatus = {
  linked: true, source: "web", cloud_url: "https://cloud.graphban.dev", org: "Ascme Labs",
  credential_set: true, linked_at: new Date().toISOString(), signed_in: true,
  linked_email: "alex@acme.example", state: "ok", can_link: true,
  projects: [
    proj("core", "core", { mapped: true, cloud_project_id: "c-1", cloud_project_tag: "CORE", last_push_result: "ok", last_push_at: PUSHED_AT }),
    proj("infra", "infra"),
    proj("notes", "notes"),
  ],
};

const recent = new Date(Date.now() - 2 * 60_000).toISOString();

const overview: SyncOrgOverview = {
  linked: true, reachable: true, unreachable_since: null, fetched_at: recent,
  org: { name: "Ascme Labs", host: "ascme-labs", plan: "team" },
  role: "admin",
  counts: { members: 18, teams: 4, policy_rules: 6, deployments: 2 },
  deployments: [
    { name: "graphban.ascme.internal", projects: 5, here: true, last_sync_at: recent },
    { name: "graphban-eu.ascme.internal", projects: 3, here: false, last_sync_at: new Date(Date.now() - 6 * 60_000).toISOString() },
  ],
  can_unlink: true,
};

/** What the server sends while the cloud is down: identity kept, counts and figures null. */
const unreachable: SyncOrgOverview = {
  ...overview,
  reachable: false,
  unreachable_since: "2026-10-30T09:42:00Z",
  counts: null,
  deployments: [
    { name: "graphban.ascme.internal", projects: null, here: true, last_sync_at: "2026-10-30T09:40:00Z" },
    { name: "graphban-eu.ascme.internal", projects: null, here: false, last_sync_at: null },
  ],
};

const hhmm = (iso: string) => new Date(iso).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", hour12: false });

const mocks = vi.hoisted(() => ({
  syncStatus: vi.fn(),
  gitops: vi.fn(),
  syncLink: vi.fn(),
  syncOrg: vi.fn(),
  syncUnlink: vi.fn(),
  syncLinkStart: vi.fn(),
  syncLinkPoll: vi.fn(),
  syncCloudProjects: vi.fn(),
}));

/** Only `control.state` is read by the nav entry. */
const gitopsView = (state: string) => ({
  project_id: "core", org_id: null, fields: {}, control: { state, writable: false },
});

vi.mock("@/lib/api", () => ({
  api: {
    projects: vi.fn(async () => [project]),
    counts: vi.fn(async () => ({ items: 0, items_in_progress: 0, requests: 0, review: 0 })),
    adminWhoami: vi.fn(async () => ({ is_platform_admin: false })),
    config: vi.fn(async () => ({ hosted_mode: false, signup_mode: "closed" })),
    syncStatus: mocks.syncStatus,
    gitops: mocks.gitops,
    syncLink: mocks.syncLink,
    syncOrg: mocks.syncOrg,
    syncUnlink: mocks.syncUnlink,
    syncLinkStart: mocks.syncLinkStart,
    syncLinkPoll: mocks.syncLinkPoll,
    syncCloudProjects: mocks.syncCloudProjects,
    dashboard: vi.fn(async () => ({
      items_total: 0, items_by_status: {}, effort_total: 0, done_count: 0, in_progress_count: 0,
      blocked_count: 0, requests_total: 0, requests_by_type: {}, requests_by_status: {},
      shard_count: 0, prd_count: 0, mcp_calls: 0, recent_items: [],
    })),
    fleet: vi.fn(async () => ({
      agents: [], online: 0, total: 0, by_role: {}, posture: "single-agent", roles: [],
      presence_ttl_seconds: 150, heartbeat_interval_seconds: 50, review_queue: [],
    })),
    orgs: vi.fn(async () => []),
    mcpTools: vi.fn(async () => ({ live: 0, tools: [] })),
    platform: vi.fn(async () => null),
    members: vi.fn(async () => []),
    keys: vi.fn(async () => []),
  },
}));

function Here() {
  return <div data-testid="here">{useLocation().pathname}</div>;
}

function wrap(ui: ReactNode, path = "/tracker") {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={qc}>
      <MemoryRouter initialEntries={[path]}>
        <ProjectProvider>
          <Routes>
            <Route path="*" element={<>{ui}<Here /></>} />
          </Routes>
        </ProjectProvider>
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

function wrapDialog(open: boolean, onOpenChange = vi.fn()) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={qc}>
      <MemoryRouter>
        <ProjectProvider>
          <Routes>
            <Route path="*" element={<><CloudOrgLinkDialog open={open} onOpenChange={onOpenChange} /><Here /></>} />
          </Routes>
        </ProjectProvider>
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

const RAIL = ["Console", "Sign in", "Projects", "First push"];

beforeEach(() => {
  vi.clearAllMocks();
  localStorage.removeItem(LINK_FINISHED_KEY);
  mocks.syncStatus.mockResolvedValue({ ...unlinked, can_link: true });
  mocks.syncOrg.mockResolvedValue(overview);
  mocks.syncLinkPoll.mockResolvedValue({ status: "pending" });
  mocks.syncCloudProjects.mockResolvedValue({ status: "ok", projects: [], mapping: [] });
});

describe("Organization window, not linked: the Link to an org wizard is the body (GRPH-1564, L1)", () => {
  it("renders nothing when closed", () => {
    wrapDialog(false);
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    expect(mocks.syncStatus).not.toHaveBeenCalled();
  });

  it("header, ROLE · admin chip, the intro line, and the wizard's four steps", async () => {
    wrapDialog(true);
    const dialog = await screen.findByRole("dialog", { name: "Link to an org" });
    expect(await within(dialog).findByText("ROLE · admin")).toBeInTheDocument();
    expect(dialog).toHaveTextContent(
      "Link this deployment to an org on Graphban Cloud. The org's people, roles and rule decisions arrive here. Your code, agent memory and coding-agent credentials stay on this deployment.",
    );
    const rail = await within(dialog).findByRole("list", { name: "Link steps" });
    for (const step of RAIL) expect(rail).toHaveTextContent(step);
  });

  it("the explainer is gone: no benefits grid, no Coming soon, no waitlist link, no redirect to Settings", async () => {
    wrapDialog(true);
    const dialog = await screen.findByRole("dialog", { name: "Link to an org" });
    await within(dialog).findByRole("list", { name: "Link steps" });
    expect(dialog.querySelector("[data-benefit]")).toBeNull();
    expect(dialog.querySelector("[data-coming-soon]")).toBeNull();
    expect(dialog).not.toHaveTextContent(/Coming soon|One org across every deployment/);
    expect(dialog.querySelector(`a[href="${ORG_SIGNUP_URL}"]`)).toBeNull();
    // It stays in the window: nothing navigates to Settings.
    expect(screen.getByTestId("here")).not.toHaveTextContent("/settings");
    expect(mocks.syncLink).not.toHaveBeenCalled();
    // Not linked never asks the org overview.
    expect(mocks.syncOrg).not.toHaveBeenCalled();
  });

  it("a non-admin box sees ROLE · member and a read-only wizard", async () => {
    mocks.syncStatus.mockResolvedValue({ ...unlinked, can_link: false });
    wrapDialog(true);
    const dialog = await screen.findByRole("dialog", { name: "Link to an org" });
    expect(await within(dialog).findByText("ROLE · member")).toBeInTheDocument();
  });

  it("a failed status read is not an unlinked box: no wizard, a Retry", async () => {
    mocks.syncStatus.mockRejectedValue(new Error("boom"));
    wrapDialog(true);
    const dialog = await screen.findByRole("dialog");
    expect(await within(dialog).findByRole("alert")).toHaveTextContent("this is not an unlinked deployment");
    expect(within(dialog).queryByRole("list", { name: "Link steps" })).toBeNull();
    expect(within(dialog).getByRole("button", { name: "Retry now" })).toBeInTheDocument();
  });

  it("resumes at the first unfinished step: signed in, nothing chosen → ③ Projects", async () => {
    mocks.syncStatus.mockResolvedValue({ ...linked, projects: [proj("core", "core")] });
    wrapDialog(true);
    const dialog = await screen.findByRole("dialog", { name: "Link to an org" });
    expect(await within(dialog).findByRole("combobox", { name: "Cloud project for core" })).toBeInTheDocument();
  });
});

describe("OrgRailItem — the Organization nav entry", () => {
  beforeEach(() => {
    mocks.syncStatus.mockResolvedValue(unlinked);
    mocks.gitops.mockResolvedValue(gitopsView("linked_set"));
  });

  const entry = () => screen.findByRole("button", { name: /Organization/ });

  it("unlinked: building icon and CLOUD, and the click opens the wizard in the window", async () => {
    const user = userEvent.setup();
    wrap(<LeftNav />);
    const btn = await entry();
    expect(btn.querySelector("svg.lucide-building2")).not.toBeNull();
    expect(btn).toHaveTextContent("CLOUD");
    expect(btn.querySelector("[data-org-dot]")).toBeNull();
    await user.click(btn);
    const dialog = await screen.findByRole("dialog", { name: "Link to an org" });
    expect(await within(dialog).findByRole("list", { name: "Link steps" })).toBeInTheDocument();
    expect(screen.getByTestId("here")).toHaveTextContent("/tracker");
    // Unlinked never asks the cloud whether it is reachable.
    expect(mocks.gitops).not.toHaveBeenCalled();
  });

  it("requestOrgWindow (the tour's Link to an org →) opens the window", async () => {
    wrap(<LeftNav />);
    await entry();
    act(() => requestOrgWindow());
    expect(await screen.findByRole("dialog", { name: "Link to an org" })).toBeInTheDocument();
  });

  it("navigates to sync settings with ?reason=malformed for bad cloud_url", async () => {
    const user = userEvent.setup();
    mocks.syncStatus.mockResolvedValue(malformed);
    wrap(<LeftNav />);
    const btn = await entry();
    expect(btn).not.toHaveTextContent("CLOUD");
    await user.click(btn);
    await waitFor(() => {
      expect(screen.getByTestId("here")).toHaveTextContent("/settings/deployment/sync");
    });
  });

  it("linked: the click opens the overview, never an external link", async () => {
    const user = userEvent.setup();
    mocks.syncStatus.mockResolvedValue(linked);
    wrap(<LeftNav />);
    await waitFor(() => expect(document.querySelector('[data-org-state="linked"]')).not.toBeNull());
    const btn = await entry();
    expect(btn.tagName).toBe("BUTTON");
    expect(btn).not.toHaveTextContent("CLOUD");
    await user.click(btn);
    await screen.findByRole("dialog", { name: "Linked to Ascme Labs" });
    expect(screen.getByTestId("here")).toHaveTextContent("/tracker");
  });
});

describe("Organization window, linked: the P67 overview (GRPH-1564, L3/L4)", () => {
  beforeEach(() => {
    mocks.syncStatus.mockResolvedValue(linked);
  });

  const open = async (onOpenChange = vi.fn()) => {
    wrapDialog(true, onOpenChange);
    return screen.findByRole("dialog", { name: "Linked to Ascme Labs" });
  };

  it("names the org and the signed-in email, the synced project with its tag and time, and the local count", async () => {
    const dialog = await open();
    expect(dialog.querySelector("[data-linked-as]")).toHaveTextContent(
      "This deployment is linked to Ascme Labs as alex@acme.example.",
    );
    expect(within(dialog).getByText("Ascme Labs", { selector: "strong" })).toBeInTheDocument();
    const rows = dialog.querySelectorAll("[data-synced-project]");
    expect(rows).toHaveLength(1);
    expect(rows[0]).toHaveTextContent(`core→☁ CORE· ☁ SYNCED ${hhmm(PUSHED_AT)}`);
    expect(within(dialog).getByText("2 projects are local only.")).toBeInTheDocument();
  });

  it("Choose projects and Sign in again open the wizard at ③ and ②", async () => {
    const user = userEvent.setup();
    const dialog = await open();
    await user.click(within(dialog).getByRole("button", { name: "Choose projects" }));
    expect(await within(dialog).findByRole("combobox", { name: "Cloud project for core" })).toBeInTheDocument();
  });

  it("SENDS / RECEIVES and DEPLOYMENTS IN <ORG>", async () => {
    const dialog = await open();
    expect(within(dialog).getByRole("list", { name: "↑ SENDS TO Ascme Labs" })).toHaveTextContent("Project names");
    expect(within(dialog).getByRole("list", { name: "↓ RECEIVES FROM Ascme Labs" })).toHaveTextContent("Org people");
    const deps = within(dialog).getByRole("list", { name: "DEPLOYMENTS IN ASCME LABS" });
    await waitFor(() => expect(deps.querySelectorAll("[data-deployment]")).toHaveLength(2));
    expect(within(deps).getByText("THIS ONE")).toBeInTheDocument();
  });

  it("no amber anywhere, and no unreachable banner while the link answers ok", async () => {
    const dialog = await open();
    await waitFor(() => expect(dialog.querySelectorAll("[data-deployment]")).toHaveLength(2));
    expect(dialog.querySelector("[data-org-unreachable]")).toBeNull();
    expect(dialog.innerHTML).not.toMatch(/224,\s*179,\s*74|st-review|amber/);
  });

  it("the link's own unknown answer is ⌁ UNREACHABLE in dashed blue, never amber", async () => {
    mocks.syncStatus.mockResolvedValue({ ...linked, state: "unknown" });
    const dialog = await open();
    const banner = dialog.querySelector<HTMLElement>("[data-org-unreachable]")!;
    expect(banner.textContent).toMatch(new RegExp(`^⌁ UNREACHABLE\\s*Org rules last synced ${hhmm(PUSHED_AT)};`));
    expect(banner.className).toContain("border-dashed");
    expect(banner.className).toContain("rgba(124,162,255");
    expect(dialog.innerHTML).not.toMatch(/224,\s*179,\s*74|st-review|amber/);
  });

  it("the org read saying unreachable while Settings' status says ok: no banner, deployments grey — both read /sync/status", async () => {
    mocks.syncOrg.mockResolvedValue(unreachable);
    const dialog = await open();
    await waitFor(() => expect(dialog.querySelectorAll("[data-deployment]")).toHaveLength(2));
    expect(dialog.querySelector("[data-org-unreachable]")).toBeNull();
    expect(dialog).not.toHaveTextContent(/Can.t reach/);
    expect(dialog.querySelector("[data-linked-as]")).toHaveTextContent("linked to Ascme Labs");
    for (const n of dialog.querySelectorAll("[data-projects]")) expect(n).toHaveTextContent("—");
    for (const n of dialog.querySelectorAll("[data-sync]")) expect(n.className).toContain("text-faint");
  });

  it("the org read failing outright still shows the linked overview", async () => {
    mocks.syncOrg.mockRejectedValue(new Error("down"));
    const dialog = await open();
    expect(await within(dialog).findByText(/Couldn't read the org's deployments/)).toBeInTheDocument();
    expect(dialog.querySelector("[data-org-unreachable]")).toBeNull();
  });

  it("Unlink confirms with Settings' copy, then calls DELETE /api/sync/link and closes", async () => {
    const user = userEvent.setup();
    const onOpenChange = vi.fn();
    mocks.syncUnlink.mockResolvedValue({ ok: true });
    const dialog = await open(onOpenChange);
    await user.click(within(dialog).getByRole("button", { name: "Unlink" }));
    const confirm = within(dialog).getByRole("alertdialog", { name: "Unlink this deployment" });
    expect(confirm).toHaveTextContent("Stops all sync. Every project becomes local only. Cloud items remain.");
    expect(mocks.syncUnlink).not.toHaveBeenCalled();
    await user.click(within(confirm).getByRole("button", { name: "Unlink this deployment" }));
    await waitFor(() => expect(mocks.syncUnlink).toHaveBeenCalled());
    await waitFor(() => expect(onOpenChange).toHaveBeenCalledWith(false));
  });

  it("a refused unlink shows the refusal and keeps the window", async () => {
    const user = userEvent.setup();
    const onOpenChange = vi.fn();
    mocks.syncUnlink.mockRejectedValue(new Error("not a box admin"));
    const dialog = await open(onOpenChange);
    await user.click(within(dialog).getByRole("button", { name: "Unlink" }));
    await user.click(within(dialog).getByRole("button", { name: "Unlink this deployment" }));
    expect(await within(dialog).findByRole("alert")).toHaveTextContent("not a box admin");
    expect(onOpenChange).not.toHaveBeenCalledWith(false);
  });

  it("a non-admin box cannot unlink", async () => {
    mocks.syncStatus.mockResolvedValue({ ...linked, can_link: false });
    const dialog = await open();
    expect(within(dialog).getByRole("button", { name: "Unlink" })).toBeDisabled();
  });

  it("Sync link settings goes to the deployment sync page; Open in Graphban Cloud opens the org", async () => {
    const user = userEvent.setup();
    const dialog = await open();
    const cloud = await within(dialog).findByRole("link", { name: /Open in Graphban Cloud/ });
    expect(cloud).toHaveAttribute("href", "https://ascme-labs.graphban.dev");
    await user.click(within(dialog).getByRole("link", { name: "Sync link settings" }));
    expect(screen.getByTestId("here")).toHaveTextContent(settingsPath("deployment/sync"));
  });
});
