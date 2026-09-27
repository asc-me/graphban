import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import type { ReactNode } from "react";
import { MemoryRouter, Route, Routes, useLocation } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { LeftNav } from "@/components/shell/LeftNav";
import { CloudOrgLinkDialog } from "@/features/settings/CloudOrgLinkDialog";
import { ProjectProvider } from "@/features/ProjectContext";
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
}));

vi.mock("@/lib/api", () => ({
  api: {
    projects: vi.fn(async () => [project]),
    counts: vi.fn(async () => ({ items: 0, items_in_progress: 0, requests: 0, review: 0 })),
    adminWhoami: vi.fn(async () => ({ is_platform_admin: false })),
    config: vi.fn(async () => ({ hosted_mode: false, signup_mode: "closed" })),
    syncStatus: mocks.syncStatus,
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

describe("CloudOrgLinkDialog", () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  it("renders nothing when closed", () => {
    wrapDialog(false);
    expect(screen.queryByText("Link to a cloud org")).not.toBeInTheDocument();
  });

  it("starts on the Plan step with billing toggle and four tiers", () => {
    wrapDialog(true);
    expect(screen.getByText("Link to a cloud org")).toBeInTheDocument();
    expect(screen.getByText("Plan")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Monthly" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Annual" })).toBeInTheDocument();
    expect(screen.getByText("Starter")).toBeInTheDocument();
    expect(screen.getByText("Pro")).toBeInTheDocument();
    expect(screen.getByText("Team")).toBeInTheDocument();
    expect(screen.getByText("Enterprise")).toBeInTheDocument();
    expect(screen.getByText(/Seats are people/)).toBeInTheDocument();
  });

  it("switches billing and updates price display", async () => {
    const user = userEvent.setup();
    wrapDialog(true);
    expect(screen.getByText("$29/mo")).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Annual" }));
    expect(screen.getByText("$24/mo")).toBeInTheDocument();
  });

  it("navigates Plan → Account → Review", async () => {
    const user = userEvent.setup();
    wrapDialog(true);
    await user.click(screen.getByRole("button", { name: "Continue" }));
    expect(screen.getByText("Your name")).toBeInTheDocument();
    expect(screen.getByText("Work email")).toBeInTheDocument();
    expect(screen.getByText("Organization name")).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Back" }));
    expect(screen.getByText(/Seats are people/)).toBeInTheDocument();
  });

  it("validates work email inline", async () => {
    const user = userEvent.setup();
    wrapDialog(true);
    await user.click(screen.getByRole("button", { name: "Continue" }));
    const emailInput = screen.getByPlaceholderText("jane@acme.com");
    await user.type(emailInput, "bad-email");
    await user.tab();
    expect(screen.getByText("Enter a valid email address")).toBeInTheDocument();
    await user.clear(emailInput);
    await user.type(emailInput, "jane@gmail.com");
    await user.tab();
    expect(screen.getByText("Use a work email")).toBeInTheDocument();
    await user.clear(emailInput);
    await user.type(emailInput, "jane@acme.com");
    await user.tab();
    expect(screen.queryByText("Use a work email")).not.toBeInTheDocument();
  });

  it("derives slug from org name and shows URL preview", async () => {
    const user = userEvent.setup();
    wrapDialog(true);
    await user.click(screen.getByRole("button", { name: "Continue" }));
    const orgInput = screen.getByPlaceholderText("Acme Corp");
    await user.type(orgInput, "Acme Corp");
    expect(screen.getByText("cloud.graphban.dev/acme-corp")).toBeInTheDocument();
  });

  it("disables Create org button with tooltip on Review step", async () => {
    const user = userEvent.setup();
    wrapDialog(true);
    await user.click(screen.getByRole("button", { name: "Continue" }));
    await user.type(screen.getByPlaceholderText("Jane Doe"), "Jane");
    await user.type(screen.getByPlaceholderText("jane@acme.com"), "jane@acme.com");
    await user.type(screen.getByPlaceholderText("Acme Corp"), "Acme");
    await user.click(screen.getByRole("button", { name: "Continue" }));
    const btn = screen.getByRole("button", { name: "Create org" });
    expect(btn).toBeDisabled();
    expect(btn).toHaveAttribute("title", "Cloud org creation is not yet available from self-hosted");
  });

  it("shows what-gets-linked disclosure on Review step", async () => {
    const user = userEvent.setup();
    wrapDialog(true);
    await user.click(screen.getByRole("button", { name: "Continue" }));
    await user.type(screen.getByPlaceholderText("Jane Doe"), "Jane");
    await user.type(screen.getByPlaceholderText("jane@acme.com"), "jane@acme.com");
    await user.type(screen.getByPlaceholderText("Acme Corp"), "Acme");
    await user.click(screen.getByRole("button", { name: "Continue" }));
    expect(screen.getByText("What gets linked")).toBeInTheDocument();
    expect(screen.getByText(/Code stays on this box/)).toBeInTheDocument();
    expect(screen.getByText(/Memory shards stay on this box/)).toBeInTheDocument();
    expect(screen.getByText(/Item content stays on this box/)).toBeInTheDocument();
    expect(screen.getByText(/Connection is outbound only/)).toBeInTheDocument();
    expect(screen.getByText(/Item keys, statuses and titles/)).toBeInTheDocument();
    expect(screen.queryByText(/bidirectionally/i)).not.toBeInTheDocument();
  });

  it("Enterprise tier routes to sales from the plan step", async () => {
    const user = userEvent.setup();
    wrapDialog(true);
    await user.click(screen.getByText("Enterprise"));
    const salesLink = screen.getByRole("link", { name: "Contact sales" });
    expect(salesLink).toHaveAttribute("href", expect.stringContaining("mailto:"));
    expect(screen.queryByRole("button", { name: "Continue" })).not.toBeInTheDocument();
  });

  it("tiers are sized by seats, not agents", () => {
    wrapDialog(true);
    expect(screen.getByText("Up to 3 seats")).toBeInTheDocument();
    expect(screen.getByText("Up to 10 seats")).toBeInTheDocument();
    expect(screen.getByText("Up to 25 seats")).toBeInTheDocument();
    expect(screen.queryByText(/Up to \d+ agents/i)).not.toBeInTheDocument();
  });

  it("disables Continue with GitHub button with tooltip", async () => {
    const user = userEvent.setup();
    wrapDialog(true);
    await user.click(screen.getByRole("button", { name: "Continue" }));
    const btn = screen.getByRole("button", { name: "Continue with GitHub" });
    expect(btn).toBeDisabled();
    expect(btn).toHaveAttribute("title", "Cloud org creation is not yet available from self-hosted");
  });

  it("'Already have an account?' navigates to sync settings", async () => {
    const user = userEvent.setup();
    wrapDialog(true);
    await user.click(screen.getByRole("button", { name: "Continue" }));
    await user.type(screen.getByPlaceholderText("Jane Doe"), "Jane");
    await user.type(screen.getByPlaceholderText("jane@acme.com"), "jane@acme.com");
    await user.type(screen.getByPlaceholderText("Acme Corp"), "Acme");
    await user.click(screen.getByRole("button", { name: "Continue" }));
    await user.click(screen.getByText("Already have an account?"));
    expect(screen.getByTestId("here")).toHaveTextContent("/settings/deployment/sync");
  });
});

describe("OrgRailItem dialog integration", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    mocks.syncStatus.mockResolvedValue(unlinked);
  });

  it("opens the dialog when unlinked (no cloud_url)", async () => {
    const user = userEvent.setup();
    wrap(<LeftNav />);
    const btn = await screen.findByRole("button", { name: /Organization/ });
    await user.click(btn);
    expect(await screen.findByText("Link to a cloud org")).toBeInTheDocument();
  });

  it("shows red dot badge when unlinked", async () => {
    wrap(<LeftNav />);
    await screen.findByRole("button", { name: /Organization/ });
    expect(screen.getByLabelText("Not linked")).toBeInTheDocument();
  });

  it("navigates to sync settings with ?reason=malformed for bad cloud_url", async () => {
    const user = userEvent.setup();
    mocks.syncStatus.mockResolvedValue(malformed);
    wrap(<LeftNav />);
    const btn = await screen.findByRole("button", { name: /Organization/ });
    await user.click(btn);
    await waitFor(() => {
      expect(screen.getByTestId("here")).toHaveTextContent("/settings/deployment/sync");
    });
  });

  it("renders external link when linked", async () => {
    mocks.syncStatus.mockReset();
    mocks.syncStatus.mockResolvedValue(linked);
    wrap(<LeftNav />);
    await waitFor(() => {
      const link = screen.getByRole("link", { name: /Organization/ });
      expect(link).toHaveAttribute("href", "https://cloud.graphban.dev/");
    });
  });

  it("CALL sabotage: clicking unlinked OrgRailItem opens dialog, not settings nav", async () => {
    const user = userEvent.setup();
    wrap(<LeftNav />);
    const btn = await screen.findByRole("button", { name: /Organization/ });
    await user.click(btn);
    expect(await screen.findByText("Link to a cloud org")).toBeInTheDocument();
    expect(screen.getByTestId("here")).toHaveTextContent("/tracker");
  });
});
