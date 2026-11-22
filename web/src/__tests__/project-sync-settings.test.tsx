import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { LINK_FINISHED_KEY } from "@/features/organization/LinkToOrgWizard";
import { SyncLinkPanel } from "@/features/settings/SyncLinkPanel";
import type { SyncStatus } from "@/lib/types";

const api = vi.hoisted(() => ({
  syncStatus: vi.fn(),
  syncOrg: vi.fn(),
  syncUnlink: vi.fn(),
  syncCloudProjects: vi.fn(),
  syncSaveProjectMap: vi.fn(),
  syncPushAll: vi.fn(),
  putProjectSync: vi.fn(),
  retryParked: vi.fn(),
  syncBudget: vi.fn(),
  putSyncBudget: vi.fn(),
  syncLinkStart: vi.fn(),
  syncLinkPoll: vi.fn(),
  syncSetGraph: vi.fn(),
  projects: vi.fn(),
}));
vi.mock("@/lib/api", () => ({ api }));

const proj = (extra: Record<string, unknown> = {}) => ({
  project_id: "core", name: "Core", writable: true, sync_graph: true,
  total_nodes: 1, synced_nodes: 1, pending: 0, last_synced_at: null, status: "live" as const,
  mapped: true, cloud_project_id: "c-1", cloud_project_tag: "CORE", ...extra,
});

const status = (pushed: boolean): SyncStatus => ({
  linked: true, source: "web", cloud_url: "https://c.dev", org: "Acme",
  credential_set: true, linked_at: new Date().toISOString(), signed_in: true,
  linked_email: "ann@acme.dev", state: "ok", can_link: true,
  projects: [proj(pushed ? { last_push_result: "ok" } : { last_push_result: null })],
});

const local = {
  project_id: "core",
  sync_level: "sync" as const,
  cloud_permission: "comments" as const,
  backfill: { total: 4, confirmed: 1, parked: 0, state: "running" as const },
  queued: 2,
  parked: { count: 1, reasons: ["contract"] },
  backlog_bytes: 80,
  backlog_full: false as boolean | null,
  held_by: "contract" as const,
  last_push_at: null,
  last_push_result: "unknown" as const,
};

function renderPanel() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={qc}>
      <MemoryRouter>
        <SyncLinkPanel />
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

beforeEach(() => {
  vi.clearAllMocks();
  api.syncOrg.mockResolvedValue({ linked: true, can_unlink: true });
  api.syncUnlink.mockResolvedValue({});
  api.projects.mockResolvedValue([{ id: "core", tag: "CORE", name: "Core" }]);
  api.syncBudget.mockResolvedValue({ bytes_per_min: 262144, requests_per_min: 30 });
  api.putProjectSync.mockResolvedValue({ project: local, backfill: null });
  api.retryParked.mockResolvedValue({ retried: 1 });
  api.syncCloudProjects.mockResolvedValue({
    status: "ok", projects: [], mapping: [], local: [local],
  });
});

describe("Settings → Sync project sync", () => {
  it("Finish in background closes the wizard and the panel shows backfill progress", async () => {
    const user = userEvent.setup();
    api.syncStatus.mockResolvedValue(status(false));
    renderPanel();
    expect(await screen.findByRole("button", { name: "Finish in background" })).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Finish in background" }));
    const card = await screen.findByTestId("project-sync");
    expect(screen.queryByTestId("link-wizard")).not.toBeInTheDocument();
    expect(await within(card).findByText(/1 of 4/)).toBeInTheDocument();
    expect(card).toHaveTextContent("unknown");
    expect(card).toHaveTextContent("held by contract");
    expect(card).not.toHaveTextContent(/synced/i);
  });

  it("changing the permission calls PUT", async () => {
    const user = userEvent.setup();
    api.syncStatus.mockResolvedValue(status(true));
    renderPanel();
    const card = await screen.findByTestId("project-sync");
    const permission = await within(card).findByLabelText("Cloud permission for Core");
    await user.selectOptions(permission, "proposals");
    await waitFor(() => expect(api.putProjectSync).toHaveBeenCalledWith("core", { cloud_permission: "proposals" }));
  });

  it("Retry now calls the parked retry", async () => {
    const user = userEvent.setup();
    api.syncStatus.mockResolvedValue(status(true));
    renderPanel();
    const card = await screen.findByTestId("project-sync");
    await user.click(await within(card).findByRole("button", { name: "Retry now" }));
    await waitFor(() => expect(api.retryParked).toHaveBeenCalledWith("core"));
  });

  it("shows Backlog full next to the size when the outbox is at the cap", async () => {
    api.syncStatus.mockResolvedValue(status(true));
    api.syncCloudProjects.mockResolvedValue({
      status: "ok", projects: [], mapping: [],
      local: [{ ...local, backlog_bytes: 104857600, backlog_full: true }],
    });
    renderPanel();
    const card = await screen.findByTestId("project-sync");
    expect(await within(card).findByText(/104,857,600 B/)).toBeInTheDocument();
    expect(card).toHaveTextContent("Backlog full");
  });

  it("does not say Backlog full when the outbox was measured under the cap", async () => {
    api.syncStatus.mockResolvedValue(status(true));
    api.syncCloudProjects.mockResolvedValue({
      status: "ok", projects: [], mapping: [],
      local: [{ ...local, backlog_full: false }],
    });
    renderPanel();
    const card = await screen.findByTestId("project-sync");
    expect(await within(card).findByText(/80 B/)).toBeInTheDocument();
    expect(card).not.toHaveTextContent("Backlog full");
  });

  it("an unmeasured backlog is not shown as fine", async () => {
    api.syncStatus.mockResolvedValue(status(true));
    api.syncCloudProjects.mockResolvedValue({
      status: "ok", projects: [], mapping: [],
      local: [{ ...local, backlog_full: null }],
    });
    renderPanel();
    const card = await screen.findByTestId("project-sync");
    expect(await within(card).findByText(/backlog unknown/)).toBeInTheDocument();
    expect(card).not.toHaveTextContent("Backlog full");
    expect(card).not.toHaveTextContent("80 B");
  });

  it("a missing local list is not read as no projects", async () => {
    api.syncStatus.mockResolvedValue(status(true));
    api.syncCloudProjects.mockResolvedValue({ status: "ok", projects: [], mapping: [] });
    renderPanel();
    expect(await screen.findByText("Project sync was not read.")).toBeInTheDocument();
    expect(screen.queryByText("No local projects.")).not.toBeInTheDocument();
  });

  describe("a local-only project in Settings", () => {
    // A finished link with nothing mapped opens on the panel, not the wizard's ③.
    beforeEach(() => localStorage.setItem(LINK_FINISHED_KEY, "1"));
    afterEach(() => localStorage.removeItem(LINK_FINISHED_KEY));
    const localOnly = {
      ...local, sync_level: "local" as const, cloud_permission: null,
      backfill: { state: "off" as const },
      queued: 0, parked: { count: 0, reasons: [] }, held_by: null, last_push_result: null,
    };
    const unmapped = (): SyncStatus => ({
      ...status(true),
      projects: [proj({ mapped: false, cloud_project_id: null, cloud_project_tag: null })],
    });

    it("does not show a full-sync backfill for a local-only project", async () => {
      api.syncStatus.mockResolvedValue(unmapped());
      api.syncCloudProjects.mockResolvedValue({
        status: "ok", projects: [], mapping: [], local: [localOnly],
      });
      renderPanel();
      const card = await screen.findByTestId("project-sync");
      expect(await within(card).findByText(/queued 0/)).toBeInTheDocument();
      expect(card).not.toHaveTextContent(/\d+ of \d+/);
    });

    it("Sync to org creates in org when no cloud project matches the tag", async () => {
      const user = userEvent.setup();
      api.syncStatus.mockResolvedValue(unmapped());
      api.syncCloudProjects.mockResolvedValue({
        status: "ok", projects: [{ id: "c-9", tag: "OTHER", name: "Other" }], mapping: [], local: [localOnly],
      });
      renderPanel();
      const card = await screen.findByTestId("project-sync");
      const box = await within(card).findByLabelText("Cloud project for Core");
      expect((box as HTMLSelectElement).value).toBe("create");
      // Digests cannot create a cloud project.
      expect(within(card).getByRole("radio", { name: "Digests only" })).toBeDisabled();
      await user.click(within(card).getByRole("radio", { name: "Sync to org" }));
      await waitFor(() =>
        expect(api.putProjectSync).toHaveBeenCalledWith("core", { sync_level: "sync", create_in_org: true }));
    });

    it("Digests only sends the picked cloud project", async () => {
      const user = userEvent.setup();
      api.syncStatus.mockResolvedValue(unmapped());
      api.syncCloudProjects.mockResolvedValue({
        status: "ok", projects: [{ id: "c-9", tag: "OTHER", name: "Other" }], mapping: [], local: [localOnly],
      });
      renderPanel();
      const card = await screen.findByTestId("project-sync");
      await user.selectOptions(await within(card).findByLabelText("Cloud project for Core"), "OTHER");
      await user.click(within(card).getByRole("radio", { name: "Digests only" }));
      await waitFor(() =>
        expect(api.putProjectSync).toHaveBeenCalledWith("core", {
          sync_level: "digests", cloud_project_id: "c-9", cloud_project_tag: "OTHER",
        }));
    });

    it("a refused create names the owner instead of swallowing it", async () => {
      const user = userEvent.setup();
      api.syncStatus.mockResolvedValue(unmapped());
      api.syncCloudProjects.mockResolvedValue({ status: "ok", projects: [], mapping: [], local: [localOnly] });
      const err = new Error(JSON.stringify({ detail: "tag_taken", owner_label: "Bea" }));
      (err as Error & { status: number }).status = 409;
      api.putProjectSync.mockRejectedValue(err);
      renderPanel();
      const card = await screen.findByTestId("project-sync");
      await user.click(await within(card).findByRole("radio", { name: "Sync to org" }));
      expect(await within(card).findByRole("alert")).toHaveTextContent("Bea already uses this tag.");
      expect(within(card).getByRole("radio", { name: "Local only" })).toBeChecked();
    });
  });
});
