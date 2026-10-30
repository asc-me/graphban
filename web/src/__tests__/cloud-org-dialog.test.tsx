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
import type { SyncStatus } from "@/lib/types";

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

const mocks = vi.hoisted(() => ({
  syncStatus: vi.fn(),
  gitops: vi.fn(),
  syncLink: vi.fn(),
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
  ])("gitops %s: %s dot, no CLOUD, and never an external link", async (control, state, colour) => {
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
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    expect(screen.getByTestId("here")).toHaveTextContent("/settings/deployment/sync");
  });
});
