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
  linked: false, source: "", cloud_url: "not-a-url", org: "", credential_set: false, linked_at: null,
  projects: [],
};

const linked: SyncStatus = {
  linked: true, source: "web", cloud_url: "https://cloud.agentldgr.dev", org: "acme",
  credential_set: true, linked_at: new Date().toISOString(), projects: [],
};

const api = vi.hoisted(() => ({
  projects: vi.fn(async () => [project]),
  counts: vi.fn(async () => ({ items: 0, items_in_progress: 0, requests: 0, review: 0 })),
  adminWhoami: vi.fn(async () => ({ is_platform_admin: false })),
  syncStatus: vi.fn(async () => unlinked),
  keys: vi.fn(async () => []),
}));

vi.mock("@/lib/api", () => ({ api }));

function Here() {
  return <div data-testid="here">{useLocation().pathname}{useLocation().search}</div>;
}

function wrap(ui: ReactNode, path = "/tracker") {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={qc}>
      <MemoryRouter initialEntries={[path]}>
        <ProjectProvider>
          <Routes>
            <Route path="*" element={ui} />
          </Routes>
          <Here />
        </ProjectProvider>
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

describe("CloudOrgLinkDialog", () => {
  beforeEach(() => vi.clearAllMocks());

  it("renders three steps: Plan, Account, Review", () => {
    wrap(<CloudOrgLinkDialog open onOpenChange={() => {}} />);
    expect(screen.getByText("1. Plan")).toBeInTheDocument();
    expect(screen.getByText("2. Account")).toBeInTheDocument();
    expect(screen.getByText("3. Review")).toBeInTheDocument();
  });

  it("Plan step: billing toggle, four tiers, seats note", () => {
    wrap(<CloudOrgLinkDialog open onOpenChange={() => {}} />);
    expect(screen.getByRole("button", { name: "Monthly" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Annual" })).toBeInTheDocument();
    // "Free" appears as both the tier name and the price label.
    expect(screen.getAllByText("Free").length).toBeGreaterThanOrEqual(1);
    expect(screen.getByText("Starter")).toBeInTheDocument();
    expect(screen.getByText("Pro")).toBeInTheDocument();
    expect(screen.getByText("Enterprise")).toBeInTheDocument();
    expect(screen.getByText(/Seats are people/)).toBeInTheDocument();
  });

  it("Enterprise tier shows 'Contact sales' instead of a price", () => {
    wrap(<CloudOrgLinkDialog open onOpenChange={() => {}} />);
    expect(screen.getByText("Contact sales")).toBeInTheDocument();
  });

  it("navigates Plan → Account → Review", async () => {
    const user = userEvent.setup();
    wrap(<CloudOrgLinkDialog open onOpenChange={() => {}} />);

    await user.click(screen.getByRole("button", { name: "Next" }));
    expect(screen.getByText("Your name")).toBeInTheDocument();
    expect(screen.getByText("Work email")).toBeInTheDocument();
    expect(screen.getByText("Organization name")).toBeInTheDocument();

    await user.type(screen.getByPlaceholderText("Ada Lovelace"), "Ada");
    await user.type(screen.getByPlaceholderText("ada@company.com"), "ada@acme.com");
    await user.type(screen.getByPlaceholderText("Acme Corp"), "Acme");
    await user.click(screen.getByRole("button", { name: "Next" }));

    expect(screen.getByText("What gets linked")).toBeInTheDocument();
    expect(screen.getByText("Acme")).toBeInTheDocument();
  });

  it("Account step: work email validation rejects personal domains", async () => {
    const user = userEvent.setup();
    wrap(<CloudOrgLinkDialog open onOpenChange={() => {}} />);

    await user.click(screen.getByRole("button", { name: "Next" }));
    await user.type(screen.getByPlaceholderText("Ada Lovelace"), "Ada");
    const emailInput = screen.getByPlaceholderText("ada@company.com");
    await user.type(emailInput, "ada@gmail.com");
    await user.tab();
    expect(await screen.findByText("Use a work email address")).toBeInTheDocument();

    await user.type(screen.getByPlaceholderText("Acme Corp"), "Acme");
    expect(screen.getByRole("button", { name: "Next" })).toBeDisabled();
  });

  it("Account step: auto-derives slug from org name", async () => {
    const user = userEvent.setup();
    wrap(<CloudOrgLinkDialog open onOpenChange={() => {}} />);

    await user.click(screen.getByRole("button", { name: "Next" }));
    await user.type(screen.getByPlaceholderText("Ada Lovelace"), "Ada");
    await user.type(screen.getByPlaceholderText("ada@company.com"), "ada@acme.com");
    await user.type(screen.getByPlaceholderText("Acme Corp"), "Acme");
    expect(screen.getByText(/acme\.graphban\.dev/)).toBeInTheDocument();
  });

  it("Review step: disabled Create org button with tooltip", async () => {
    const user = userEvent.setup();
    wrap(<CloudOrgLinkDialog open onOpenChange={() => {}} />);

    await user.click(screen.getByRole("button", { name: "Next" }));
    await user.type(screen.getByPlaceholderText("Ada Lovelace"), "Ada");
    await user.type(screen.getByPlaceholderText("ada@company.com"), "ada@acme.com");
    await user.type(screen.getByPlaceholderText("Acme Corp"), "Acme");
    await user.click(screen.getByRole("button", { name: "Next" }));

    const btn = screen.getByRole("button", { name: "Create org" });
    expect(btn).toBeDisabled();
    expect(btn).toHaveAttribute("title", "Cloud org creation is not yet available from self-hosted");
  });

  it("Review step: what-gets-linked disclosure states vectors and source stay on box", async () => {
    const user = userEvent.setup();
    wrap(<CloudOrgLinkDialog open onOpenChange={() => {}} />);

    await user.click(screen.getByRole("button", { name: "Next" }));
    await user.type(screen.getByPlaceholderText("Ada Lovelace"), "Ada");
    await user.type(screen.getByPlaceholderText("ada@company.com"), "ada@acme.com");
    await user.type(screen.getByPlaceholderText("Acme Corp"), "Acme");
    await user.click(screen.getByRole("button", { name: "Next" }));

    expect(screen.getByText(/Raw vectors stay on this box/)).toBeInTheDocument();
    expect(screen.getByText(/Source code never leaves this box/)).toBeInTheDocument();
    expect(screen.getByText(/outbound only/)).toBeInTheDocument();
  });

  it("'Already have an account?' navigates to sync settings", async () => {
    const user = userEvent.setup();
    wrap(<CloudOrgLinkDialog open onOpenChange={() => {}} />);
    await user.click(screen.getByText("Already have an account?"));
    expect(screen.getByTestId("here")).toHaveTextContent("/settings/deployment/sync");
  });
});

describe("OrgRailItem (self-host nav)", () => {
  beforeEach(() => vi.clearAllMocks());

  it("opens the cloud-org dialog when cloud_url is empty (unlinked)", async () => {
    const user = userEvent.setup();
    api.syncStatus.mockResolvedValue(unlinked);
    wrap(<LeftNav />);

    // Wait for the resolved button (loading state renders a link, not a button).
    const orgBtn = await screen.findByRole("button", { name: /Organization/i });
    await user.click(orgBtn);

    expect(await screen.findByText("Connect to a cloud org")).toBeInTheDocument();
    expect(screen.getByText("1. Plan")).toBeInTheDocument();
  });

  it("shows a red dot badge when unlinked", async () => {
    api.syncStatus.mockResolvedValue(unlinked);
    wrap(<LeftNav />);
    // Wait for the resolved button with the badge.
    await screen.findByRole("button", { name: /Organization/i });
    expect(screen.getByLabelText("Not linked")).toBeInTheDocument();
  });

  it("navigates to sync page with ?reason=malformed when cloud_url is malformed", async () => {
    const user = userEvent.setup();
    api.syncStatus.mockResolvedValue(malformed);
    wrap(<LeftNav />);

    const orgBtn = await screen.findByRole("button", { name: /Organization/i });
    await user.click(orgBtn);

    expect(screen.getByTestId("here")).toHaveTextContent("/settings/deployment/sync?reason=malformed");
  });

  it("renders an external link when cloud_url is valid", async () => {
    api.syncStatus.mockResolvedValue(linked);
    wrap(<LeftNav />);

    // Poll until the external <a> replaces the loading-state NavLink.
    await waitFor(() => {
      const links = screen.getAllByRole("link", { name: /Organization/i });
      const external = links.find((l) => l.getAttribute("target") === "_blank");
      expect(external).toBeTruthy();
      expect(external).toHaveAttribute("href", "https://cloud.agentldgr.dev/");
    });
  });

  it("sabotage: OrgRailItem must call onOpenDialog — removing it breaks the unlinked flow", async () => {
    const sources = import.meta.glob("../components/shell/LeftNav.tsx", {
      query: "?raw",
      import: "default",
      eager: true,
    }) as Record<string, string>;
    const src = Object.values(sources)[0] ?? "";

    expect(src).toContain("onOpenDialog");
    expect(src).toContain("CloudOrgLinkDialog");
    expect(src).toContain("orgDialogOpen");
  });
});
