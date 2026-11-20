import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";

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

  it("a missing local list is not read as no projects", async () => {
    api.syncStatus.mockResolvedValue(status(true));
    api.syncCloudProjects.mockResolvedValue({ status: "ok", projects: [], mapping: [] });
    renderPanel();
    expect(await screen.findByText("Project sync was not read.")).toBeInTheDocument();
    expect(screen.queryByText("No local projects.")).not.toBeInTheDocument();
  });
});
