import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter, Route, Routes, useLocation } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { ProjectProvider } from "@/features/ProjectContext";
import { PrdListView } from "@/features/prds/PrdListView";
import type { SyncStorageStatus } from "@/lib/api";
import type { PrdSummary } from "@/lib/types";

const now = new Date().toISOString();
const prd = (id: string, status: PrdSummary["status"]): PrdSummary => ({
  id, title: `Spec ${id}`, status, version: "v1", linked: [], updated_at: now, closed_at: null,
});
const served = [prd("P-1", "approved"), prd("P-2", "draft"), prd("P-3", "draft")];

const conflictReport = {
  folder: "/drive/core", prds_dir: "/drive/core/PRDs", exported: [], imported: [], updated_db: [],
  updated_file: [], conflicts: ["P-1"], export_status: "ok" as const, in_sync: 1,
  conflict_details: [{ prd_id: "P-1", status: "approved", ledger_hash: "aaa", file_hash: "bbb",
    file_name: "P-1 — Spec.md", byte_length: 10, path: "PRDs/P-1 — Spec.md" }],
  read_failures: [{ file_name: "P-3 — Spec.md", path: "PRDs/P-3 — Spec.md",
    error: "not valid UTF-8 text (corrupt or binary file)", prd_id: "P-3" }],
};

function status(over: Partial<SyncStorageStatus> = {}): SyncStorageStatus {
  return {
    folder: { path: "/drive", configured: true, ok: true, writable: true, error: "", project_path: "/drive/core" },
    blob_dir: { path: "/data/sync/blobs", configured: false, ok: true, writable: true, error: "" },
    index: { available: true, reason: "", chunks: 4, mode: "vector" },
    rows: [
      { prd_id: "P-1", title: "Spec P-1", status: "approved", sync_state: "conflict", file_name: "P-1 — Spec.md", error: "" },
      { prd_id: "P-2", title: "Spec P-2", status: "draft", sync_state: "in_sync", file_name: "P-2 — Spec.md", error: "" },
      { prd_id: "P-3", title: "Spec P-3", status: "draft", sync_state: "read_failed", file_name: "P-3 — Spec.md", error: "not valid UTF-8" },
    ],
    report: conflictReport,
    last_run: { at: null, export_status: null, error: "" },
    ...over,
  };
}

const syncStorage = vi.fn(async () => status());
const saveSyncStorage = vi.fn(async (_p: string, _b: unknown) => status());
const runSyncStorage = vi.fn(async () => status());

vi.mock("@/lib/api", () => ({
  setActiveProjectId: vi.fn(),
  api: {
    projects: vi.fn(async () => [
      { id: "core", name: "Core", accent: "#a78bfa", visibility: "private", description: "", share_global_memory: false, auto_extract: true, mcp_enabled: true, embed_model: "" },
    ]),
    me: vi.fn(async () => ({ name: "Alex Cain" })),
    prds: vi.fn(async () => served),
    syncStorage: (...a: unknown[]) => syncStorage(...(a as [])),
    saveSyncStorage: (...a: unknown[]) => saveSyncStorage(...(a as [string, unknown])),
    runSyncStorage: (...a: unknown[]) => runSyncStorage(...(a as [])),
  },
}));

let lastState: unknown = null;
function ConflictProbe() {
  const loc = useLocation();
  lastState = loc.state;
  return <div data-testid="conflict-page">{loc.pathname}</div>;
}

function renderList() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={qc}>
      <MemoryRouter initialEntries={["/prds"]}>
        <ProjectProvider>
          <Routes>
            <Route path="/prds" element={<PrdListView />} />
            <Route path="/prds/conflict/:id" element={<ConflictProbe />} />
          </Routes>
        </ProjectProvider>
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

const panel = () => screen.getByRole("region", { name: "Sync storage" });

beforeEach(() => {
  syncStorage.mockReset().mockImplementation(async () => status());
  saveSyncStorage.mockReset().mockImplementation(async () => status());
  runSyncStorage.mockReset().mockImplementation(async () => status());
  lastState = null;
});

describe("Sync storage panel on Specs (GRPH-1320)", () => {
  it("shows the three facts and says a folder, not an account", async () => {
    renderList();
    await screen.findByText("/drive");
    const p = panel();
    expect(within(p).getByTestId("fact-folder")).toHaveTextContent("not an account");
    expect(within(p).getByTestId("fact-folder")).toHaveTextContent("Google Drive Desktop folder");
    expect(within(p).getByTestId("fact-folder")).toHaveTextContent("/drive/core/PRDs");
    expect(within(p).getByTestId("fact-blobs")).toHaveTextContent("/data/sync/blobs");
    expect(within(p).getByTestId("fact-blobs")).toHaveTextContent("(default)");
    expect(within(p).getByTestId("fact-blobs")).toHaveTextContent("sha256");
    expect(within(p).getByTestId("fact-index")).toHaveTextContent("available");
    expect(p).toHaveTextContent("An approved PRD whose file does not match the ledger stays a conflict");
    expect(p).toHaveTextContent("A failed export does not roll the ledger back");
    expect(within(p).queryByText(/connect/i)).toBeNull();
  });

  it("shows row sync states by word, read failure apart from conflict", async () => {
    renderList();
    expect(await screen.findByTestId("row-sync-P-1")).toHaveTextContent("conflict");
    expect(screen.getByTestId("row-sync-P-2")).toHaveTextContent("in sync");
    expect(screen.getByTestId("row-sync-P-3")).toHaveTextContent("read failed");
    const failures = within(panel()).getByTestId("read-failures");
    expect(failures).toHaveTextContent("PRDs/P-3 — Spec.md");
    expect(within(panel()).getByTestId("sync-conflicts")).not.toHaveTextContent("P-3");
  });

  it("an unreadable folder is a failure on that fact and keeps the documents listed", async () => {
    syncStorage.mockImplementation(async () =>
      status({
        folder: { path: "/Volumes/GoogleDrive", configured: true, ok: false, writable: false, error: "does not exist on this box" },
        rows: served.map((p) => ({ prd_id: p.id, title: p.title, status: p.status, sync_state: "unknown" as const, file_name: "", error: "" })),
        report: null,
      }),
    );
    renderList();
    expect(await within(await screen.findByTestId("fact-folder")).findByText(/cannot read: does not exist/)).toBeInTheDocument();
    for (const p of served) expect(screen.getByText(p.title)).toBeInTheDocument();
    expect(screen.getByTestId("fact-blobs")).not.toHaveTextContent("cannot read");
    expect(screen.getByTestId("row-sync-P-1")).toHaveTextContent("sync unknown");
    expect(within(panel()).getByRole("button", { name: "Open sync report" })).toBeDisabled();
  });

  it("an unavailable index says why and does not hide the ledger rows", async () => {
    syncStorage.mockImplementation(async () =>
      status({ index: { available: false, reason: "the index cannot be read: OperationalError", chunks: null, mode: null } }),
    );
    renderList();
    expect(await screen.findByText(/unavailable: the index cannot be read/)).toBeInTheDocument();
    expect(screen.getByText("Spec P-1")).toBeInTheDocument();
  });

  it("a sync storage read error does not hide the documents", async () => {
    syncStorage.mockImplementation(async () => {
      throw new Error("500");
    });
    renderList();
    expect(await screen.findByText(/Sync storage could not be read/)).toBeInTheDocument();
    expect(screen.getByText("Spec P-2")).toBeInTheDocument();
  });

  it("saving the folder path is the box's sync configuration", async () => {
    renderList();
    await screen.findByText("/drive");
    const folder = screen.getByTestId("fact-folder");
    await userEvent.click(within(folder).getByRole("button", { name: "Change" }));
    const input = within(folder).getByRole("textbox", { name: "Folder path" });
    await userEvent.clear(input);
    await userEvent.type(input, "/Users/me/Google Drive/Specs");
    await userEvent.click(within(folder).getByRole("button", { name: "Save" }));
    await waitFor(() =>
      expect(saveSyncStorage).toHaveBeenCalledWith("core", { folder_path: "/Users/me/Google Drive/Specs" }),
    );
  });

  it("saving the blob directory sends only that field", async () => {
    renderList();
    await screen.findByText("/drive");
    const blobs = screen.getByTestId("fact-blobs");
    await userEvent.click(within(blobs).getByRole("button", { name: "Change" }));
    await userEvent.type(within(blobs).getByRole("textbox", { name: "Blob directory" }), "/srv/blobs");
    await userEvent.click(within(blobs).getByRole("button", { name: "Save" }));
    await waitFor(() => expect(saveSyncStorage).toHaveBeenCalledWith("core", { blob_dir: "/srv/blobs" }));
  });

  it("Run sync refreshes the facts and the row sync states", async () => {
    runSyncStorage.mockImplementation(async () =>
      status({
        rows: served.map((p) => ({ prd_id: p.id, title: p.title, status: p.status, sync_state: "in_sync" as const, file_name: "", error: "" })),
        report: { ...conflictReport, conflicts: [], conflict_details: [], read_failures: [] },
        last_run: { at: now, export_status: "failed", error: "" },
        run: { ...conflictReport, conflicts: [], conflict_details: [], read_failures: [], export_status: "failed" },
      }),
    );
    renderList();
    expect(await screen.findByTestId("row-sync-P-1")).toHaveTextContent("conflict");
    await userEvent.click(within(panel()).getByRole("button", { name: "Run sync" }));
    await waitFor(() => expect(screen.getByTestId("row-sync-P-1")).toHaveTextContent("in sync"));
    expect(runSyncStorage).toHaveBeenCalledWith("core");
    expect(screen.getByTestId("last-run")).toHaveTextContent("export failed; the ledger was left as it was");
  });

  it("Open sync report opens the conflict record with the report", async () => {
    renderList();
    await screen.findByText("/drive");
    await userEvent.click(within(panel()).getByRole("button", { name: "Open sync report" }));
    expect(await screen.findByTestId("conflict-page")).toHaveTextContent("/prds/conflict/P-1");
    expect((lastState as { report: { conflicts: string[] } }).report.conflicts).toEqual(["P-1"]);
  });
});
