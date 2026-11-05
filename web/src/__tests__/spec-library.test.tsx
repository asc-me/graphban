import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter, Route, Routes, useLocation, useParams, useSearchParams } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { ProjectProvider } from "@/features/ProjectContext";
import type { SpecLibrary, SpecLibraryPrd } from "@/lib/api";

/** The Specs library (GRPH-1318). These drive the page: filter, select, and the one Open. */

const mocks = vi.hoisted(() => ({
  specLibrary: vi.fn(),
  syncStorage: vi.fn(),
  prd: vi.fn(),
  specPrd: vi.fn(),
  updatePrd: vi.fn(),
  prdVersions: vi.fn(async () => []),
  grillState: vi.fn(async () => null),
  intentDiff: vi.fn(async () => null),
  prdRequests: vi.fn(async () => []),
  platform: vi.fn(async () => ({ effective_chat_provider: "anthropic" })),
}));

vi.mock("@/lib/api", () => ({
  setActiveProjectId: vi.fn(),
  api: {
    projects: vi.fn(async () => [{ id: "core", name: "Core", tag: "GRPH" }]),
    ...mocks,
  },
}));
vi.mock("@/lib/publicApi", () => ({ publicApi: { uploadAttachment: vi.fn() } }));
vi.mock("@/features/assistant/AssistantPanel", () => ({ AssistantPanel: () => null }));
vi.mock("@/features/prds/GrillPanel", () => ({ GrillPanel: () => null }));
vi.mock("@/features/prds/ApprovalEval", () => ({ ApprovalEval: () => null }));

const { SpecsLibraryView } = await import("@/features/prds/SpecsLibraryView");
const { PrdEditorView } = await import("@/features/prds/PrdEditorView");

const H = (c: string) => `sha256:${c.repeat(8)}`;

function prd(over: Partial<SpecLibraryPrd> = {}): SpecLibraryPrd {
  return {
    id: "GRPH-P1",
    title: "Empty draft",
    version: "v0.1",
    status: "draft",
    content_hash: H("a"),
    baseline_hash: null,
    baseline_version: null,
    sync_state: "not_exported",
    conflict: null,
    ...over,
  };
}

function library(over: Partial<SpecLibrary> = {}): SpecLibrary {
  return {
    prds: [
      prd(),
      prd({
        id: "GRPH-P2",
        title: "Screens",
        status: "draft",
        content_hash: H("c"),
        baseline_hash: H("b"),
        baseline_version: "v1.0",
        sync_state: "ledger_changed",
      }),
      prd({
        id: "GRPH-P9",
        title: "Governed",
        version: "v1.0",
        status: "approved",
        content_hash: H("d"),
        baseline_hash: H("d"),
        baseline_version: "v1.0",
        sync_state: "conflict",
        conflict: {
          ledger_hash: "ledger-aaa",
          file_hash: "file-bbb",
          file_name: "GRPH-P9.md",
          byte_length: 20,
          path: "PRDs/GRPH-P9.md",
        },
      }),
    ],
    preamble: { id: "DOC-1", title: "Preamble", version: 3, content_hash: H("e") },
    documents: [
      {
        kind: "constraints",
        set_id: "DS-1",
        prd_id: "GRPH-P9",
        screen: "",
        title: "Set constraints",
        status: "accepted",
        hash: H("f"),
        set_status: "open",
      },
      {
        kind: "screen",
        set_id: "DS-1",
        prd_id: "GRPH-P9",
        screen: "Home",
        title: "Home",
        status: "none",
        hash: "",
        set_status: "open",
      },
    ],
    conflict_ids: ["GRPH-P9"],
    ...over,
  };
}

function syncStatus() {
  return {
    folder: { path: "/tmp/specs", configured: true, ok: true, writable: true, error: "", project_path: "/tmp/specs/core" },
    blob_dir: { path: "/tmp/blobs", configured: true, ok: true, writable: true, error: "" },
    index: { available: true, reason: "", chunks: 0, mode: "lexical" as const },
    rows: [],
    report: {
      folder: "/tmp/specs",
      prds_dir: "/tmp/specs/core/PRDs",
      exported: [],
      imported: [],
      updated_db: [],
      updated_file: [],
      conflicts: ["GRPH-P9"],
      conflict_details: [{
        prd_id: "GRPH-P9",
        status: "approved",
        ledger_hash: "ledger-aaa",
        file_hash: "file-bbb",
        file_name: "GRPH-P9.md",
        byte_length: 20,
        path: "PRDs/GRPH-P9.md",
      }],
      export_status: "ok" as const,
      in_sync: 1,
      read_failures: [],
    },
    last_run: { at: null, export_status: null, error: "" },
  };
}

function Where() {
  const loc = useLocation();
  return <div data-testid="where">{loc.pathname}{loc.search}</div>;
}

function ConflictProbe() {
  const loc = useLocation();
  const state = loc.state as { report?: { conflict_details?: { ledger_hash: string }[] } } | null;
  return <div data-testid="conflict-probe">{state?.report?.conflict_details?.[0]?.ledger_hash ?? "no-report"}</div>;
}

function ScreenProbe() {
  const { setId } = useParams();
  const [params] = useSearchParams();
  return <div data-testid="screen-probe">{setId}?screen={params.get("screen") ?? ""}</div>;
}

function show(opts: { staleTime?: number; reader?: boolean } = {}) {
  const qc = new QueryClient({
    defaultOptions: {
      queries: { retry: false, refetchOnWindowFocus: false, staleTime: opts.staleTime ?? 0 },
    },
  });
  render(
    <QueryClientProvider client={qc}>
      <MemoryRouter initialEntries={["/prds"]}>
        <ProjectProvider>
          <Where />
          <Routes>
            <Route path="/prds/conflict/:id" element={<ConflictProbe />} />
            <Route path="/prds/preamble" element={<div>preamble reader</div>} />
            <Route path="/prds/constraints/:setId" element={<ScreenProbe />} />
            <Route path="/prds/new" element={<div>new prd</div>} />
            <Route path="/prds/:id" element={opts.reader ? <PrdEditorView /> : <div>prd reader</div>} />
            <Route path="/prds" element={<SpecsLibraryView />} />
          </Routes>
        </ProjectProvider>
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

beforeEach(() => {
  mocks.specLibrary.mockReset();
  mocks.syncStorage.mockReset();
  mocks.syncStorage.mockResolvedValue(syncStatus());
  mocks.prd.mockReset();
  mocks.specPrd.mockReset();
  mocks.updatePrd.mockReset();
  mocks.prdVersions.mockReset();
  mocks.prdVersions.mockResolvedValue([]);
  mocks.grillState.mockReset();
  mocks.grillState.mockResolvedValue(null);
  mocks.intentDiff.mockReset();
  mocks.intentDiff.mockResolvedValue(null);
  mocks.prdRequests.mockReset();
  mocks.prdRequests.mockResolvedValue([]);
  mocks.platform.mockReset();
  mocks.platform.mockResolvedValue({ effective_chat_provider: "anthropic" });
});

describe("Specs library", () => {
  it("labels a baseline, labels no baseline, and keeps a draft that still has one", async () => {
    mocks.specLibrary.mockResolvedValue(library());
    show();
    const draft = await screen.findByTestId("spec-row-prd-GRPH-P1");
    expect(within(draft).getByTestId("no-baseline")).toHaveTextContent("no baseline");
    expect(within(draft).getByText("Draft")).toBeInTheDocument();
    expect(within(draft).queryByTestId("hash-baseline")).toBeNull();

    const saved = screen.getByTestId("spec-row-prd-GRPH-P2");
    expect(within(saved).getByTestId("hash-baseline")).toBeInTheDocument();
    expect(within(saved).getByText("Draft")).toBeInTheDocument();
    expect(screen.getAllByTestId("spec-row-prd-GRPH-P2")).toHaveLength(1);

    const approved = screen.getByTestId("spec-row-prd-GRPH-P9");
    expect(within(approved).getByTestId("hash-baseline")).toBeInTheDocument();
    expect(within(approved).getByText("Approved")).toBeInTheDocument();
    expect(screen.queryByRole("group", { name: "Filter by status" })).toBeNull();
    expect(screen.getByRole("region", { name: "Sync storage" })).toBeInTheDocument();

    await userEvent.setup().click(saved);
    expect(screen.getByTestId("where")).toHaveTextContent(/^\/prds$/);
    const detail = screen.getByTestId("spec-detail");
    expect(within(detail).getByText("Draft")).toBeInTheDocument();
    expect(within(detail).getByTestId("hash-baseline")).toBeInTheDocument();
  });

  it("filters the loaded list by title and does not ask the server again", async () => {
    mocks.specLibrary.mockResolvedValue(library());
    show();
    await screen.findByTestId("spec-row-screen-DS-1-Home");
    const calls = mocks.specLibrary.mock.calls.length;
    await userEvent.setup().type(screen.getByRole("textbox", { name: "Filter specs by id or title" }), "home");
    expect(screen.getByTestId("spec-row-screen-DS-1-Home")).toBeInTheDocument();
    expect(screen.queryByTestId("spec-row-prd-GRPH-P1")).toBeNull();
    expect(screen.queryByTestId("spec-row-preamble")).toBeNull();
    expect(mocks.specLibrary.mock.calls.length).toBe(calls);
  });

  it("stays on this page when a conflict row is selected", async () => {
    mocks.specLibrary.mockResolvedValue(library());
    show();
    await userEvent.setup().click(await screen.findByTestId("spec-row-prd-GRPH-P9"));
    expect(screen.getByTestId("where")).toHaveTextContent(/^\/prds$/);
    expect(screen.queryByTestId("conflict-probe")).toBeNull();
    expect(screen.getByTestId("conflict-path")).toHaveTextContent("PRDs/GRPH-P9.md");
    expect(screen.getByTestId("conflict-ledger")).toHaveTextContent("ledger-aaa");
    expect(screen.getByTestId("conflict-file")).toHaveTextContent("file-bbb");
    expect(screen.getByTestId("conflict-note")).toHaveTextContent("An approved PRD takes no body from the folder.");
    expect(screen.getByTestId("spec-open")).toHaveTextContent("Open conflict");
  });

  it("opens the conflict record with the sync report", async () => {
    mocks.specLibrary.mockResolvedValue(library());
    show();
    await userEvent.setup().click(await screen.findByTestId("spec-row-prd-GRPH-P9"));
    await userEvent.setup().click(screen.getByTestId("spec-open"));
    expect(screen.getByTestId("where")).toHaveTextContent("/prds/conflict/GRPH-P9");
    expect(screen.getByTestId("conflict-probe")).toHaveTextContent("ledger-aaa");
  });

  it("links New PRD at the empty draft and the single conflict count at that conflict", async () => {
    mocks.specLibrary.mockResolvedValue(library());
    show();
    const count = await screen.findByTestId("conflict-count");
    expect(screen.getByRole("link", { name: "New PRD" })).toHaveAttribute("href", "/prds/new");
    expect(count).toHaveAttribute("href", "/prds/conflict/GRPH-P9");
    expect(count).toHaveTextContent("1 conflict");
    await userEvent.setup().click(count);
    expect(screen.getByTestId("where")).toHaveTextContent("/prds/conflict/GRPH-P9");
    expect(screen.getByTestId("conflict-probe")).toHaveTextContent("ledger-aaa");
  });

  it("shows a count, not a link, when more than one PRD is in conflict", async () => {
    const data = library();
    data.prds.push(prd({
      id: "GRPH-P8",
      title: "Other",
      status: "approved",
      sync_state: "conflict",
      conflict: {
        ledger_hash: "ledger-ccc",
        file_hash: "file-ddd",
        file_name: "GRPH-P8.md",
        byte_length: 4,
        path: "PRDs/GRPH-P8.md",
      },
    }));
    data.conflict_ids = ["GRPH-P9", "GRPH-P8"];
    mocks.specLibrary.mockResolvedValue(data);
    show();
    const count = await screen.findByTestId("conflict-count");
    expect(count.tagName).toBe("SPAN");
    expect(count).toHaveTextContent("2 conflicts");
    expect(count).not.toHaveAttribute("href");
  });

  it("opens a screen on that screen, the preamble on the preamble, and a PRD on its reader", async () => {
    mocks.specLibrary.mockResolvedValue(library());
    const user = userEvent.setup();
    show();
    await user.click(await screen.findByTestId("spec-row-screen-DS-1-Home"));
    await user.click(screen.getByTestId("spec-open"));
    expect(screen.getByTestId("screen-probe")).toHaveTextContent("DS-1?screen=Home");
  });

  it("opens the preamble", async () => {
    mocks.specLibrary.mockResolvedValue(library());
    const user = userEvent.setup();
    show();
    await user.click(await screen.findByTestId("spec-row-preamble"));
    await user.click(screen.getByTestId("spec-open"));
    expect(screen.getByTestId("where")).toHaveTextContent("/prds/preamble");
  });

  it("opens a PRD that is not in conflict on its reader", async () => {
    mocks.specLibrary.mockResolvedValue(library());
    const user = userEvent.setup();
    show();
    await user.click(await screen.findByTestId("spec-row-prd-GRPH-P1"));
    await user.click(screen.getByTestId("spec-open"));
    expect(screen.getByTestId("where")).toHaveTextContent("/prds/GRPH-P1");
  });

  it("says there is no preamble, with nothing to open", async () => {
    mocks.specLibrary.mockResolvedValue(library({ preamble: null }));
    show();
    const note = await screen.findByTestId("no-preamble");
    expect(note).toHaveTextContent("No preamble in this project.");
    expect(within(note).queryByRole("button")).toBeNull();
    expect(within(note).queryByRole("link")).toBeNull();
  });

  it("saving an approved PRD through the reader refreshes the row to draft and keeps the baseline", async () => {
    const approved = library({
      prds: [prd({
        id: "GRPH-P9",
        title: "Governed",
        version: "v1.0",
        status: "approved",
        content_hash: H("d"),
        baseline_hash: H("d"),
        baseline_version: "v1.0",
        sync_state: "in_sync",
        conflict: null,
      })],
      preamble: null,
      documents: [],
      conflict_ids: [],
    });
    const drafted = {
      ...approved,
      prds: [{ ...approved.prds[0], status: "draft" as const, version: "v1.1", content_hash: H("e") }],
    };
    let saved = false;
    mocks.specLibrary.mockImplementation(async () => (saved ? drafted : approved));
    mocks.prd.mockResolvedValue({
      id: "GRPH-P9", title: "Governed", status: "approved", version: "v1.0",
      linked: [], project_id: "core", body: "Ship it.", updated_at: "", created_at: "",
    });
    mocks.specPrd.mockResolvedValue({
      prd_id: "GRPH-P9", status: "approved", version: "v1.0", content_hash: H("d"),
      baseline_hash: H("d"), baseline_version: "v1.0", design_set: null,
    });
    mocks.updatePrd.mockImplementation(async () => {
      saved = true;
      return {
        id: "GRPH-P9", title: "Governed", status: "draft", version: "v1.1",
        linked: [], project_id: "core", body: "Ship it. More.", updated_at: "", created_at: "",
      };
    });

    const user = userEvent.setup();
    show({ staleTime: 15_000, reader: true });
    const row = await screen.findByTestId("spec-row-prd-GRPH-P9");
    expect(within(row).getByText("Approved")).toBeInTheDocument();
    expect(within(row).getByTestId("hash-baseline")).toBeInTheDocument();

    await user.click(row);
    await user.click(screen.getByTestId("spec-open"));
    await screen.findByTestId("approved-line");
    await user.type(document.querySelector("textarea")!, " More.");
    await user.click(screen.getByRole("button", { name: "Save" }));
    await waitFor(() => expect(mocks.updatePrd).toHaveBeenCalled());
    expect(mocks.updatePrd.mock.calls[0][1]).toMatchObject({ status: "draft" });

    await user.click(screen.getByRole("button", { name: "Specs" }));
    await waitFor(() => {
      const again = screen.getByTestId("spec-row-prd-GRPH-P9");
      expect(within(again).getByText("Draft")).toBeInTheDocument();
      expect(within(again).getByTestId("hash-baseline")).toBeInTheDocument();
    });
    expect(mocks.specLibrary.mock.calls.length).toBeGreaterThan(1);
  });

  it("says there are no specs yet and still offers New PRD", async () => {
    mocks.specLibrary.mockResolvedValue({ prds: [], preamble: null, documents: [], conflict_ids: [] });
    show();
    expect(await screen.findByText("No specs yet")).toBeInTheDocument();
    const links = screen.getAllByRole("link", { name: "New PRD" });
    expect(links.length).toBeGreaterThan(0);
    for (const link of links) expect(link).toHaveAttribute("href", "/prds/new");
    expect(screen.queryByTestId("conflict-count")).toBeNull();
  });
});
