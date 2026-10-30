import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import type { ReactNode } from "react";
import { MemoryRouter, Route, Routes, useLocation } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { LeftNav } from "@/components/shell/LeftNav";
import { CloudOrgLinkDialog } from "@/features/settings/CloudOrgLinkDialog";
import { ProjectProvider } from "@/features/ProjectContext";
import { ORG_SIGNUP_URL } from "@/lib/routes";
import type { SyncOrgOverview, SyncStatus } from "@/lib/types";

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

const linked: SyncStatus = {
  linked: true, source: "web", cloud_url: "https://cloud.graphban.dev", org: "acme",
  credential_set: true, linked_at: new Date().toISOString(), projects: [],
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

function wrapDialog(open: boolean, onOpenChange = vi.fn(), linked = false) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={qc}>
      <MemoryRouter>
        <ProjectProvider>
          <Routes>
            <Route path="*" element={<><CloudOrgLinkDialog open={open} onOpenChange={onOpenChange} linked={linked} /><Here /></>} />
          </Routes>
        </ProjectProvider>
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

describe("CloudOrgLinkDialog — the not-linked explainer (GRPH-1224)", () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  it("renders nothing when closed", () => {
    wrapDialog(false);
    expect(screen.queryByText("One org across every deployment")).not.toBeInTheDocument();
  });

  it("shows the kicker, title, five benefits and the three stays items", () => {
    wrapDialog(true);
    const dialog = screen.getByRole("dialog", { name: "One org across every deployment" });
    expect(within(dialog).getByText("ORGANIZATIONS · GRAPHBAN CLOUD")).toBeInTheDocument();
    const benefits = Array.from(dialog.querySelectorAll("[data-benefit]")).map((b) => b.querySelector("span.font-semibold")?.textContent);
    expect(benefits).toEqual([
      "People and teams in one place",
      "Roles and access (RBAC)",
      "Policy rules for every project",
      "Galaxy",
      "Feedback on your own domain",
    ]);
    expect(dialog.querySelectorAll("[data-benefit]")[4]).toHaveClass("col-span-full");
    expect(within(dialog).getByText("STAYS ON THIS BOX")).toBeInTheDocument();
    expect(Array.from(dialog.querySelectorAll("[data-stays]")).map((s) => s.textContent?.replace("✓", ""))).toEqual([
      "Code and repos", "Memory shards", "Item content",
    ]);
  });

  it("the old create-org flow is gone: no tiers, no sales, no review step, no cloud slug", () => {
    wrapDialog(true);
    for (const gone of [/Starter/, /Enterprise/, /Contact sales/, /Review/, /cloud\.graphban\.dev\//, /Continue/]) {
      expect(screen.queryByText(gone)).not.toBeInTheDocument();
    }
  });

  it.each([
    ["Link to an existing org", /border-line /],
    ["Create a cloud org", /bg-accent/],
  ])("%s is a Coming soon link to the waitlist in a new tab", (label, style) => {
    wrapDialog(true);
    const link = screen.getByRole("link", { name: `${label} — coming soon, join the waitlist` });
    expect(link).toHaveAttribute("href", ORG_SIGNUP_URL);
    expect(ORG_SIGNUP_URL).toBe("https://graphban.dev/platform#waitlist");
    expect(link).toHaveAttribute("target", "_blank");
    expect(link).toHaveAttribute("rel", "noopener noreferrer");
    expect(within(link).getByText("Coming soon")).toBeVisible();
    expect(link.className).toMatch(style);
  });

  it("neither org action links this box: clicking them never calls POST /api/sync/link", async () => {
    const user = userEvent.setup();
    wrapDialog(true);
    for (const name of [/Link to an existing org/, /Create a cloud org/]) {
      const link = screen.getByRole("link", { name });
      link.addEventListener("click", (e) => e.preventDefault());
      await user.click(link);
    }
    expect(mocks.syncLink).not.toHaveBeenCalled();
  });

  it("Not now closes the window", async () => {
    const user = userEvent.setup();
    const onOpenChange = vi.fn();
    wrapDialog(true, onOpenChange);
    await user.click(screen.getByRole("button", { name: "Not now" }));
    expect(onOpenChange).toHaveBeenCalledWith(false);
  });
});

describe("OrgRailItem — the Organization nav entry (GRPH-1224)", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    mocks.syncStatus.mockResolvedValue(unlinked);
    mocks.gitops.mockResolvedValue(gitopsView("linked_set"));
    mocks.syncOrg.mockResolvedValue(overview);
  });

  const entry = () => screen.findByRole("button", { name: /Organization/ });

  it("unlinked: building icon and CLOUD, and the click opens the explainer", async () => {
    const user = userEvent.setup();
    wrap(<LeftNav />);
    const btn = await entry();
    expect(btn.querySelector("svg.lucide-building2")).not.toBeNull();
    expect(btn).toHaveTextContent("CLOUD");
    expect(btn.querySelector("[data-org-dot]")).toBeNull();
    await user.click(btn);
    const dialog = await screen.findByRole("dialog", { name: "One org across every deployment" });
    expect(dialog.querySelectorAll("[data-benefit]")).toHaveLength(5);
    expect(dialog.querySelectorAll("[data-stays]")).toHaveLength(3);
    expect(screen.getByTestId("here")).toHaveTextContent("/tracker");
    // Unlinked never asks the cloud whether it is reachable.
    expect(mocks.gitops).not.toHaveBeenCalled();
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

  it.each([
    ["linked_set", "linked", "var(--color-st-done)"],
    ["linked_unreachable", "unreachable", "rgb(224, 179, 74)"],
  ])("gitops %s: %s dot, no CLOUD, never an external link, and the click opens the overview", async (control, state, colour) => {
    const user = userEvent.setup();
    mocks.syncStatus.mockResolvedValue(linked);
    mocks.gitops.mockResolvedValue(gitopsView(control));
    wrap(<LeftNav />);
    await waitFor(() => expect(document.querySelector(`[data-org-state="${state}"]`)).not.toBeNull());
    const btn = await entry();
    expect(btn.tagName).toBe("BUTTON");
    expect(btn.querySelector("svg.lucide-building2")).not.toBeNull();
    expect(btn).not.toHaveTextContent("CLOUD");
    expect(btn.querySelector<HTMLElement>("[data-org-dot]")!.style.background).toBe(colour);
    expect(screen.queryByRole("link", { name: /Organization/ })).not.toBeInTheDocument();
    expect(document.querySelector('a[href^="https://cloud.graphban.dev"]')).toBeNull();
    await user.click(btn);
    await screen.findByRole("dialog", { name: "Linked to Ascme Labs" });
    expect(screen.queryByText("One org across every deployment")).not.toBeInTheDocument();
    expect(screen.getByTestId("here")).toHaveTextContent("/tracker");
  });
});

describe("CloudOrgLinkDialog — the org overview on a linked box (GRPH-1225)", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    mocks.syncOrg.mockResolvedValue(overview);
  });

  const open = async () => {
    wrapDialog(true, vi.fn(), true);
    return screen.findByRole("dialog", { name: "Linked to Ascme Labs" });
  };

  it("linked: identity, the three chips, four counts and the lede", async () => {
    const dialog = await open();
    expect(within(dialog).getByText(/This deployment shares people, roles and org policy rules with Ascme Labs/)).toBeInTheDocument();
    const ident = dialog.querySelector<HTMLElement>("[data-org-identity]")!;
    expect(within(ident).getByText("AL")).toBeInTheDocument();
    expect(within(ident).getByText("Ascme Labs")).toBeInTheDocument();
    expect(within(ident).getByText("ascme-labs.graphban.dev")).toBeInTheDocument();
    // `role` is the linking credential's, so the chip never claims it is the viewer's.
    expect(Array.from(ident.querySelectorAll("[data-chip]")).map((c) => c.textContent)).toEqual([
      "team plan", "Linked as admin", "Linked",
    ]);
    expect(within(ident).queryByText(/YOU/i)).not.toBeInTheDocument();
    expect(ident.querySelector("[data-org-status]")).toHaveAttribute("data-org-status", "linked");
    const counts = Array.from(dialog.querySelectorAll("[data-stat]")).map((s) => [
      s.getAttribute("data-stat"), s.querySelector("[data-count]")?.textContent,
    ]);
    expect(counts).toEqual([["members", "18"], ["teams", "4"], ["policy_rules", "6"], ["deployments", "2"]]);
    expect(dialog.querySelector("[data-org-unreachable]")).toBeNull();
    expect(screen.queryByRole("button", { name: "Retry now" })).not.toBeInTheDocument();
  });

  it("Org policy rules opens Rules and closes the window", async () => {
    const user = userEvent.setup();
    const onOpenChange = vi.fn();
    wrapDialog(true, onOpenChange, true);
    const dialog = await screen.findByRole("dialog", { name: "Linked to Ascme Labs" });
    const rules = within(dialog).getByRole("link", { name: /Org policy rules/ });
    expect(rules).toHaveAttribute("href", "/rules");
    await user.click(rules);
    expect(screen.getByTestId("here")).toHaveTextContent("/rules");
    expect(onOpenChange).toHaveBeenCalledWith(false);
  });

  it("every count links through: members and teams to the org in Cloud, deployments to the list", async () => {
    const dialog = await open();
    expect(within(dialog).getByRole("link", { name: /Members/ })).toHaveAttribute("href", "https://ascme-labs.graphban.dev");
    expect(within(dialog).getByRole("link", { name: /Teams/ })).toHaveAttribute("href", "https://ascme-labs.graphban.dev");
    expect(within(dialog).getByRole("link", { name: /^2 Deployments/ })).toHaveAttribute("href", "#org-deployments");
  });

  it("deployment rows: name, THIS ONE on this box only, project count and a green recent sync", async () => {
    const dialog = await open();
    expect(within(dialog).getByText("DEPLOYMENTS IN THIS ORG")).toBeInTheDocument();
    const rows = Array.from(dialog.querySelectorAll<HTMLElement>("[data-deployment]"));
    expect(rows).toHaveLength(2);
    expect(within(rows[0]!).getByText("graphban.ascme.internal")).toBeInTheDocument();
    expect(within(rows[0]!).getByText("THIS ONE")).toBeInTheDocument();
    expect(within(rows[1]!).queryByText("THIS ONE")).not.toBeInTheDocument();
    expect(rows[0]!.querySelector("[data-projects]")).toHaveTextContent("5 projects");
    expect(rows[1]!.querySelector("[data-projects]")).toHaveTextContent("3 projects");
    expect(rows[0]!.querySelector("[data-sync]")).toHaveTextContent("synced 2m ago");
    expect(rows[0]!.querySelector("[data-sync]")).toHaveClass("text-st-done");
  });

  it("footer: Sync link settings goes to the deployment sync page; Open in Graphban Cloud is the org in a new tab", async () => {
    const user = userEvent.setup();
    const dialog = await open();
    const cloud = within(dialog).getByRole("link", { name: /Open in Graphban Cloud/ });
    expect(cloud).toHaveAttribute("href", "https://ascme-labs.graphban.dev");
    expect(cloud).toHaveAttribute("target", "_blank");
    await user.click(within(dialog).getByRole("link", { name: "Sync link settings" }));
    expect(screen.getByTestId("here")).toHaveTextContent("/settings/deployment/sync");
  });

  it("unreachable: every count is \u2014 (never 0), the banner names the time, and sync times turn amber", async () => {
    mocks.syncOrg.mockResolvedValue(unreachable);
    const dialog = await open();
    const ident = dialog.querySelector<HTMLElement>("[data-org-identity]")!;
    expect(ident.querySelector("[data-org-status]")).toHaveTextContent("Unreachable");
    expect(ident.querySelector("[data-org-status]")).toHaveClass("text-st-review");
    const counts = Array.from(dialog.querySelectorAll("[data-count]"));
    expect(counts.map((c) => c.textContent)).toEqual(["—", "—", "—", "—"]);
    for (const c of counts) expect(c).toHaveClass("text-faint");
    const banner = dialog.querySelector<HTMLElement>("[data-org-unreachable]")!;
    expect(banner).toHaveTextContent(
      `Can't reach ascme-labs.graphban.dev since ${hhmm("2026-10-30T09:42:00Z")}. Org policy rules show as unmeasured in Rules`,
    );
    expect(banner).toHaveTextContent("Local work carries on.");
    const rows = Array.from(dialog.querySelectorAll<HTMLElement>("[data-deployment]"));
    expect(rows[0]!.querySelector("[data-sync]")).toHaveTextContent(`last sync ${hhmm("2026-10-30T09:40:00Z")}`);
    expect(rows[0]!.querySelector("[data-sync]")).toHaveClass("text-st-review");
    expect(rows[1]!.querySelector("[data-sync]")).toHaveTextContent("—");
    for (const r of rows) expect(r.querySelector("[data-projects]")).toHaveTextContent("—");
  });

  it("unreachable never shows numbers even if stale counts came along", async () => {
    mocks.syncOrg.mockResolvedValue({ ...unreachable, counts: overview.counts });
    const dialog = await open();
    expect(Array.from(dialog.querySelectorAll("[data-count]")).map((c) => c.textContent)).toEqual(["—", "—", "—", "—"]);
  });

  it("Retry now sits beside Open in Graphban Cloud and refetches, showing Retrying…", async () => {
    const user = userEvent.setup();
    mocks.syncOrg.mockResolvedValue(unreachable);
    const dialog = await open();
    expect(within(dialog).getByRole("link", { name: /Open in Graphban Cloud/ })).toBeInTheDocument();
    expect(mocks.syncOrg).toHaveBeenCalledTimes(1);
    let finish!: (v: SyncOrgOverview) => void;
    mocks.syncOrg.mockImplementationOnce(() => new Promise((r) => { finish = r; }));
    await user.click(within(dialog).getByRole("button", { name: "Retry now" }));
    expect(await within(dialog).findByRole("button", { name: "Retrying…" })).toBeInTheDocument();
    expect(mocks.syncOrg).toHaveBeenCalledTimes(2);
    finish(overview);
    await waitFor(() => expect(dialog.querySelector("[data-org-status]")).toHaveTextContent("Linked"));
    expect(dialog.querySelector('[data-stat="members"] [data-count]')).toHaveTextContent("18");
  });

  it("Unlink asks for confirmation, then calls DELETE /api/sync/link and closes", async () => {
    const user = userEvent.setup();
    const onOpenChange = vi.fn();
    mocks.syncUnlink.mockResolvedValue(unlinked);
    wrapDialog(true, onOpenChange, true);
    const dialog = await screen.findByRole("dialog", { name: "Linked to Ascme Labs" });
    await user.click(within(dialog).getByRole("button", { name: "Unlink…" }));
    expect(mocks.syncUnlink).not.toHaveBeenCalled();
    const confirm = within(dialog).getByRole("alertdialog", { name: "Unlink this deployment" });
    expect(confirm).toHaveTextContent("Unlink this deployment from Ascme Labs?");
    await user.click(within(confirm).getByRole("button", { name: "Cancel" }));
    expect(mocks.syncUnlink).not.toHaveBeenCalled();
    await user.click(within(dialog).getByRole("button", { name: "Unlink…" }));
    await user.click(within(dialog).getByRole("button", { name: "Unlink" }));
    await waitFor(() => expect(mocks.syncUnlink).toHaveBeenCalledTimes(1));
    await waitFor(() => expect(onOpenChange).toHaveBeenCalledWith(false));
  });

  it("a refused unlink shows the refusal and its rule", async () => {
    const user = userEvent.setup();
    mocks.syncUnlink.mockRejectedValue(new Error(JSON.stringify({
      detail: "only an admin of this box can unlink it to a cloud org", rule_id: "sync.link_needs_box_admin",
    })));
    const dialog = await open();
    await user.click(within(dialog).getByRole("button", { name: "Unlink…" }));
    await user.click(within(dialog).getByRole("button", { name: "Unlink" }));
    const alert = await within(dialog).findByRole("alert");
    expect(alert).toHaveTextContent("only an admin of this box can unlink it");
    expect(within(alert).getByText("sync.link_needs_box_admin")).toBeInTheDocument();
    expect(within(alert).getByRole("link", { name: "see rule" })).toHaveAttribute("href", "/rules?rule=sync.link_needs_box_admin");
  });

  it("a non-admin sees no Unlink", async () => {
    mocks.syncOrg.mockResolvedValue({ ...overview, can_unlink: false });
    const dialog = await open();
    expect(within(dialog).getByRole("link", { name: "Sync link settings" })).toBeInTheDocument();
    expect(within(dialog).queryByRole("button", { name: /Unlink/ })).not.toBeInTheDocument();
  });

  it("the server saying unlinked falls back to the explainer", async () => {
    mocks.syncOrg.mockResolvedValue({ linked: false });
    wrapDialog(true, vi.fn(), true);
    expect(await screen.findByRole("dialog", { name: "One org across every deployment" })).toBeInTheDocument();
  });

  it("the unlinked explainer never asks for the org overview", () => {
    wrapDialog(true);
    expect(mocks.syncOrg).not.toHaveBeenCalled();
  });
});
