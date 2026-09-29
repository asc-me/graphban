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
    expect(screen.getByPlaceholderText("paste link key…")).toBeInTheDocument();
    expect(screen.queryByPlaceholderText("paste key…")).not.toBeInTheDocument();
    expect(screen.getByText(/Mint the link key there/)).toBeInTheDocument();
    expect(screen.getByText(/paste the cloud URL and link key/i)).toBeInTheDocument();
    // Same object the intro names. This field used to say "Sync API key" while
    // the paragraph said "link key".
    expect(screen.getByText("Link key")).toBeInTheDocument();
    expect(screen.queryByText(/Sync API key/i)).not.toBeInTheDocument();
    // After pairing, Graphban identity is an API key / link key; the Anthropic
    // secret is a provider key. This footer used to say "provider API keys".
    expect(screen.getByText(/the link key is stored/i)).toBeInTheDocument();
    expect(screen.getByText(/provider keys/i)).toBeInTheDocument();
    expect(screen.queryByText(/provider API keys/i)).not.toBeInTheDocument();
  });

  it("submits the link form with URL, key, and org", async () => {
    const user = userEvent.setup();
    renderPanel();
    await screen.findByText("not linked");

    await user.type(screen.getByPlaceholderText("cloud.graphban.dev"), "cloud.agentldgr.dev");
    await user.type(screen.getByPlaceholderText("paste link key…"), "gb_sk_secret");
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
    await user.type(screen.getByPlaceholderText("paste link key…"), "gb_sk_secret");
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

  // ── PRD-47 S17: a read that failed is not a read still running ──────────────────────────

  it("says loading while the status read is in flight, and offers no retry yet", () => {
    // Never settles. Pending is its own answer, not the failure below and not the panel.
    api.syncStatus.mockReturnValue(new Promise(() => {}));
    renderPanel();

    expect(screen.getByText(/Loading sync status/)).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /retry/i })).not.toBeInTheDocument();
    expect(screen.queryByText(/the request failed/i)).not.toBeInTheDocument();
  });

  it("names a failed status read instead of loading forever", async () => {
    const user = userEvent.setup();
    api.syncStatus.mockRejectedValue(new Error("boom"));
    renderPanel();

    // Half one: the failure is named and there is a way out.
    expect(await screen.findByText(/the request failed/i)).toBeInTheDocument();
    const retry = screen.getByRole("button", { name: /retry/i });

    // Half two: neither of the two things this failure must not look like. "Loading"
    // forever is a promise nobody intends to keep, and "not linked" would be a claim
    // about the instance that nothing read.
    expect(screen.queryByText(/Loading sync status/)).not.toBeInTheDocument();
    expect(screen.queryByText("not linked")).not.toBeInTheDocument();
    expect(screen.queryByText(/Mint the link key there/)).not.toBeInTheDocument();

    // The retry re-reads; the panel it was standing in for arrives.
    api.syncStatus.mockResolvedValue(unlinked);
    await user.click(retry);
    expect(await screen.findByText("not linked")).toBeInTheDocument();
  });
});
