import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { GITOPS_PRELINK_KEY } from "@/features/settings/GitopsPanel";
import { SyncLinkPanel } from "@/features/settings/SyncLinkPanel";
import { keys } from "@/lib/queries";
import type { SyncStatus } from "@/lib/types";

const unlinked: SyncStatus = {
  linked: false, source: "", cloud_url: "", org: "", credential_set: false, linked_at: null,
  projects: [
    { project_id: "core", name: "Core", writable: true, sync_graph: true, total_nodes: 1240, synced_nodes: 1200, pending: 40, last_synced_at: null, status: "stale" },
  ],
};

const linked: SyncStatus = {
  linked: true, source: "web", cloud_url: "https://cloud.agentldgr.dev", org: "acme",
  credential_set: true, linked_at: new Date().toISOString(),
  projects: [
    { project_id: "core", name: "Core", writable: true, sync_graph: true, total_nodes: 1240, synced_nodes: 1240, pending: 0, last_synced_at: new Date().toISOString(), status: "live" },
  ],
};

const api = vi.hoisted(() => ({
  syncStatus: vi.fn(),
  syncLink: vi.fn(),
  syncUnlink: vi.fn(),
  syncSetGraph: vi.fn(),
  syncPush: vi.fn(),
  syncPurge: vi.fn(),
  syncExport: vi.fn(),
  syncImport: vi.fn(),
}));

vi.mock("@/lib/api", () => ({ api }));

function renderPanel() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return {
    qc,
    ...render(
      <QueryClientProvider client={qc}>
        <MemoryRouter>
          <SyncLinkPanel />
        </MemoryRouter>
      </QueryClientProvider>,
    ),
  };
}

describe("SyncLinkPanel", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    sessionStorage.removeItem(GITOPS_PRELINK_KEY);
    api.syncStatus.mockResolvedValue(unlinked);
    api.syncLink.mockResolvedValue(linked);
    api.syncUnlink.mockResolvedValue(unlinked);
    api.syncSetGraph.mockResolvedValue({});
    api.syncPush.mockResolvedValue({ project_id: "core", pushed: 40, removed: 0 });
  });

  it("shows the link form when the instance is not linked", async () => {
    renderPanel();
    expect(await screen.findByText("not linked")).toBeInTheDocument();
    expect(screen.getByPlaceholderText("cloud.graphban.dev")).toBeInTheDocument();
    expect(screen.getByPlaceholderText("paste key…")).toBeInTheDocument();
    expect(screen.getByText(/Mint the link key there/)).toBeInTheDocument();
    // Same object the intro names. This field used to say "Sync API key" while
    // the paragraph said "link key".
    expect(screen.getByText("Link key")).toBeInTheDocument();
    expect(screen.queryByText(/Sync API key/i)).not.toBeInTheDocument();
  });

  it("submits the link form with URL, key, and org", async () => {
    const user = userEvent.setup();
    renderPanel();
    await screen.findByText("not linked");

    await user.type(screen.getByPlaceholderText("cloud.graphban.dev"), "cloud.agentldgr.dev");
    await user.type(screen.getByPlaceholderText("paste key…"), "gb_sk_secret");
    await user.type(screen.getByPlaceholderText("acme"), "acme");
    await user.click(screen.getByRole("button", { name: "Link instance" }));

    await waitFor(() =>
      expect(api.syncLink).toHaveBeenCalledWith("cloud.agentldgr.dev", "gb_sk_secret", "acme"),
    );
  });

  it("renders link details and gates the scoped controls until a project is picked", async () => {
    const user = userEvent.setup();
    api.syncStatus.mockResolvedValue(linked);
    renderPanel();

    expect(await screen.findByText("linked")).toBeInTheDocument();
    expect(screen.getByText("https://cloud.agentldgr.dev")).toBeInTheDocument();
    // Status used to label this row "Credential" — the same paste the form
    // now calls a link key.
    expect(screen.getByText("Link key")).toBeInTheDocument();
    expect(screen.queryByText(/^Credential$/i)).not.toBeInTheDocument();

    // Scope is empty → the graph-sync checkbox does nothing yet.
    expect(screen.getByText("No project selected")).toBeInTheDocument();

    // Selecting the project scopes the lower cards and enables the toggle.
    await user.click(screen.getByRole("button", { name: /Core/ }));
    expect(await screen.findByText("Controls below apply to this project only")).toBeInTheDocument();

    await user.click(screen.getByText(/Sync this project.s code graph to the cloud/));
    await waitFor(() => expect(api.syncSetGraph).toHaveBeenCalledWith("core", false));
  });

  it("unlink drops the gitops cache and notes a pre-link restore", async () => {
    const user = userEvent.setup();
    api.syncStatus.mockResolvedValue(linked);
    const { qc } = renderPanel();
    qc.setQueryData(keys.gitops("core"), { control: { state: "local" } });
    expect(qc.getQueryData(keys.gitops("core"))).toBeTruthy();

    await user.click(await screen.findByRole("button", { name: "Unlink" }));
    await waitFor(() => expect(api.syncUnlink).toHaveBeenCalled());
    expect(qc.getQueryData(keys.gitops("core"))).toBeUndefined();
    expect(sessionStorage.getItem(GITOPS_PRELINK_KEY)).toBe("1");
  });

  it("link drops the gitops cache and does not note an unlink restore", async () => {
    const user = userEvent.setup();
    const { qc } = renderPanel();
    qc.setQueryData(keys.gitops("core"), { fields: { base_branch: { value: "test" } } });
    await screen.findByText("not linked");

    await user.type(screen.getByPlaceholderText("cloud.graphban.dev"), "cloud.agentldgr.dev");
    await user.type(screen.getByPlaceholderText("paste key…"), "gb_sk_secret");
    await user.type(screen.getByPlaceholderText("acme"), "acme");
    await user.click(screen.getByRole("button", { name: "Link instance" }));

    await waitFor(() => expect(api.syncLink).toHaveBeenCalled());
    expect(qc.getQueryData(keys.gitops("core"))).toBeUndefined();
    expect(sessionStorage.getItem(GITOPS_PRELINK_KEY)).toBeNull();
  });

  it("graph-sync toggle does not drop gitops", async () => {
    const user = userEvent.setup();
    api.syncStatus.mockResolvedValue(linked);
    const { qc } = renderPanel();
    qc.setQueryData(keys.gitops("core"), { keep: true });
    await user.click(await screen.findByRole("button", { name: /Core/ }));
    await user.click(screen.getByText(/Sync this project.s code graph to the cloud/));
    await waitFor(() => expect(api.syncSetGraph).toHaveBeenCalled());
    expect(qc.getQueryData(keys.gitops("core"))).toEqual({ keep: true });
  });
});
