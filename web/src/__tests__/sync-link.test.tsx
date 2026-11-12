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
  syncLinkStart: vi.fn(),
  syncLinkPoll: vi.fn(),
  syncCloudProjects: vi.fn(),
  syncOrg: vi.fn(),
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
    api.syncOrg.mockResolvedValue({ linked: false });
    api.syncUnlink.mockResolvedValue(unlinked);
    api.syncSetGraph.mockResolvedValue({});
    api.syncPush.mockResolvedValue({ project_id: "core", pushed: 40, removed: 0 });
  });

  it("shows the Link to an org wizard, not a key form, when the instance is not linked", async () => {
    renderPanel();
    expect(await screen.findByText("not linked")).toBeInTheDocument();
    expect(screen.getByTestId("link-wizard")).toBeInTheDocument();
    expect(screen.getByLabelText("Console URL")).toBeInTheDocument();
    expect(screen.queryByPlaceholderText("paste link key…")).not.toBeInTheDocument();
    expect(screen.queryByText(/Link key/i)).not.toBeInTheDocument();
  });

  it("shows a non-admin the wizard read-only on an unlinked box (can_link false)", async () => {
    api.syncStatus.mockResolvedValue({ ...unlinked, can_link: false });
    renderPanel();
    expect(await screen.findByTestId("link-wizard")).toBeInTheDocument();
    expect(screen.getByText(/Only a box admin can link this deployment/)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Get a sign-in code" })).toBeDisabled();
    expect(screen.getByLabelText("Console URL")).toHaveAttribute("readonly");
    expect(api.syncLinkStart).not.toHaveBeenCalled();
  });

  it("gives a box admin a live wizard on an unlinked box (can_link true)", async () => {
    api.syncStatus.mockResolvedValue({ ...unlinked, can_link: true });
    renderPanel();
    expect(await screen.findByTestId("link-wizard")).toBeInTheDocument();
    expect(screen.queryByText(/Only a box admin can link/)).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Get a sign-in code" })).toBeEnabled();
  });

  it("shows a non-admin the Linked panel read-only (can_link false)", async () => {
    api.syncStatus.mockResolvedValue({
      ...linked, can_link: false, signed_in: true,
      projects: [{ ...linked.projects[0], mapped: true }],
    });
    renderPanel();
    const unlinkBtn = await screen.findByRole("button", { name: "Unlink" });
    expect(unlinkBtn).toBeDisabled();
    expect(screen.getByRole("button", { name: "Sign in again" })).toBeDisabled();
  });

  it("renders link details and gates the scoped controls until a project is picked", async () => {
    const user = userEvent.setup();
    api.syncStatus.mockResolvedValue(linked);
    renderPanel();

    expect(await screen.findByText("linked")).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Open the org →" })).toHaveAttribute("href", "https://cloud.agentldgr.dev");
    expect(screen.getByTestId("linked-panel")).toBeInTheDocument();
    expect(screen.queryByText(/Link key/i)).not.toBeInTheDocument();

    // Scope is empty → the graph-sync checkbox does nothing yet.
    expect(screen.getByText("No project selected")).toBeInTheDocument();

    // Selecting the project scopes the lower cards and enables the toggle.
    await user.click(screen.getByRole("button", { name: /Core/ }));
    expect(await screen.findByText("Controls below apply to this project only")).toBeInTheDocument();

    await user.click(screen.getByText(/Sync this project.s code graph to the cloud/));
    await waitFor(() => expect(api.syncSetGraph).toHaveBeenCalledWith("core", false));
  });

  it("says the code graph stays local when the privacy toggle is off", async () => {
    api.syncStatus.mockResolvedValue({
      ...linked,
      projects: [
        ...linked.projects,
        {
          project_id: "north", name: "North", writable: true, sync_graph: false,
          total_nodes: 10, synced_nodes: 0, pending: 0, last_synced_at: null, status: "paused",
        },
      ],
    });
    renderPanel();
    expect(await screen.findByText("graph kept local")).toBeInTheDocument();
    expect(screen.queryByText(/^kept local$/)).not.toBeInTheDocument();
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

  it("a successful sign-in drops the gitops cache and does not note an unlink restore", async () => {
    const user = userEvent.setup();
    api.syncLinkStart.mockResolvedValue({
      status: "started", user_code: "ABCD-1234", verification_url: "https://c/link", interval: 0.01,
    });
    api.syncLinkPoll.mockResolvedValue({ status: "approved", org: "acme", email: "a@b.c" });
    api.syncCloudProjects.mockResolvedValue({ status: "ok", projects: [], mapping: [] });
    const { qc } = renderPanel();
    qc.setQueryData(keys.gitops("core"), { fields: { base_branch: { value: "test" } } });
    await screen.findByText("not linked");

    await user.click(screen.getByRole("button", { name: "Get a sign-in code" }));
    await waitFor(() => expect(api.syncLinkPoll).toHaveBeenCalled());
    await waitFor(() => expect(qc.getQueryData(keys.gitops("core"))).toBeUndefined());
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
