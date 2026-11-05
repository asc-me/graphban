import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { ProjectProvider } from "@/features/ProjectContext";

/** The Spec reader (GRPH-1319): a PRD, the preamble, or one set's constraints. These render
 *  the reader through its routes and assert on what it says, not on the panels it reuses. */

vi.mock("@/features/assistant/AssistantPanel", () => ({ AssistantPanel: () => <div>assistant panel</div> }));
vi.mock("@/features/prds/GrillPanel", () => ({ GrillPanel: () => <div>grill panel</div> }));
vi.mock("@/features/prds/ApprovalEval", () => ({ ApprovalEval: () => <div>approval eval</div> }));

class NotFoundError extends Error {
  status = 404;
}

const mocks = vi.hoisted(() => ({
  prd: vi.fn(),
  prdVersions: vi.fn(async () => []),
  grillState: vi.fn(async () => null),
  intentDiff: vi.fn(async () => null),
  items: vi.fn(async () => []),
  specPrd: vi.fn(),
  specPreamble: vi.fn(),
  putPreamble: vi.fn(),
  specDesignSet: vi.fn(),
  acceptSetConstraints: vi.fn(),
  compileScreen: vi.fn(),
  updatePrd: vi.fn(),
  me: vi.fn(async () => ({ id: "u1", name: "Dana Reyes", email: "dana@example.com" })),
}));

vi.mock("@/lib/api", () => ({
  setActiveProjectId: vi.fn(),
  api: {
    projects: vi.fn(async () => [{ id: "core", name: "Core", tag: "GRPH" }]),
    platform: vi.fn(async () => ({ effective_chat_provider: "anthropic" })),
    prdRequests: vi.fn(async () => []),
    ...mocks,
  },
}));
vi.mock("@/lib/publicApi", () => ({ publicApi: { uploadAttachment: vi.fn() } }));

const { PrdEditorView } = await import("@/features/prds/PrdEditorView");
const { PreambleReaderView, SetConstraintsReaderView } = await import("@/features/prds/SpecReaderView");

const H = (c: string) => `sha256:${c.repeat(64)}`;

function prd(status: string, linked: string[] = []) {
  return {
    id: "GRPH-P9", title: "Screens", status, version: "v1.0", linked, project_id: "core",
    body: "## Goals\n\nShip it.", updated_at: "", created_at: "",
  };
}

function facts(status: string, baselined: boolean, design_set: unknown = null) {
  return {
    prd_id: "GRPH-P9", status, version: "v1.0", content_hash: H("a"),
    baseline_hash: baselined ? H("b") : null, baseline_version: baselined ? "v1.0" : null, design_set,
  };
}

function set(accepted: boolean, prompt = "none", refused?: string[]) {
  return {
    id: "DS-1", prd_id: "GRPH-P9", status: "open", stale_reason: "", constraints_hash: H("c"),
    constraints_accepted: accepted, preamble_version: 1, body_state: "stored", body: "Teal only.",
    screens: [{
      screen: "home", tier: "BACKED", tier_confirmed: true, prompt,
      compile_refused: refused ?? (accepted ? [] : ["the set constraints are not accepted"]),
    }],
    history: accepted ? [{ constraints_hash: H("c"), person: "Dana Reyes", relayed_by: "user:u1", current: true }] : [],
  };
}

function preamble(over: Record<string, unknown> = {}) {
  return {
    id: "DOC-1", version: 1, title: "Preamble", cites_prd_id: "GRPH-P1", prd_hash: H("d"),
    content_hash: H("e"), created_by: "key:Dana", sections: [{ heading: "", text: "Be calm." }],
    body_state: "stored", body: "Be calm.", history: [{ version: 1, content_hash: H("e"), created_by: "key:Dana", governing: true }],
    stale_sets: [], ...over,
  };
}

function show(path: string) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  render(
    <QueryClientProvider client={qc}>
      <MemoryRouter initialEntries={[path]}>
        <ProjectProvider>
          <Routes>
            <Route path="/prds" element={<div>specs list</div>} />
            <Route path="/prds/preamble" element={<PreambleReaderView />} />
            <Route path="/prds/constraints/:setId" element={<SetConstraintsReaderView />} />
            <Route path="/prds/:id" element={<PrdEditorView />} />
          </Routes>
        </ProjectProvider>
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

beforeEach(() => {
  for (const m of Object.values(mocks)) m.mockReset();
  mocks.prdVersions.mockResolvedValue([]);
  mocks.grillState.mockResolvedValue(null);
  mocks.intentDiff.mockResolvedValue(null);
  mocks.items.mockResolvedValue([]);
  mocks.me.mockResolvedValue({ id: "u1", name: "Dana Reyes", email: "dana@example.com" });
});

describe("PRD document", () => {
  it("names the kind, says a draft is not the spec, and shows no baseline hash", async () => {
    mocks.prd.mockResolvedValue(prd("draft"));
    mocks.specPrd.mockResolvedValue(facts("draft", false));
    show("/prds/GRPH-P9");
    expect(await screen.findByTestId("draft-line")).toHaveTextContent("A draft is not the spec. Default search does not return it.");
    expect(screen.getByTestId("spec-reader-kind")).toHaveTextContent("PRD");
    expect(await screen.findByTestId("no-baseline")).toBeInTheDocument();
    expect(screen.queryByTestId("hash-baseline")).not.toBeInTheDocument();
  });

  it("labels the baseline on an approved PRD, and saving writes the next draft", async () => {
    mocks.prd.mockResolvedValue(prd("approved"));
    mocks.specPrd.mockResolvedValue(facts("approved", true));
    mocks.updatePrd.mockResolvedValue(prd("draft"));
    show("/prds/GRPH-P9");
    const chip = await screen.findByTestId("hash-baseline");
    expect(chip).toHaveTextContent("baseline");
    expect(within(chip).getByText("baseline").className).toContain("text-st-done");
    expect(screen.getByTestId("approved-line")).toHaveTextContent("Saving writes the next draft");
    expect(screen.queryByTestId("draft-line")).not.toBeInTheDocument();

    const user = userEvent.setup();
    const editor = document.querySelector("textarea")!;
    await user.type(editor, " More.");
    await user.click(screen.getByRole("button", { name: "Save" }));
    await waitFor(() => expect(mocks.updatePrd).toHaveBeenCalled());
    expect(mocks.updatePrd.mock.calls[0][1]).toMatchObject({ status: "draft" });
  });

  it("saving a draft PRD writes that draft without touching status", async () => {
    mocks.prd.mockResolvedValue(prd("draft"));
    mocks.specPrd.mockResolvedValue(facts("draft", false));
    mocks.updatePrd.mockResolvedValue(prd("draft"));
    show("/prds/GRPH-P9");
    await screen.findByTestId("draft-line");
    const user = userEvent.setup();
    await user.type(document.querySelector("textarea")!, " x");
    await user.click(screen.getByRole("button", { name: "Save" }));
    await waitFor(() => expect(mocks.updatePrd).toHaveBeenCalled());
    expect(mocks.updatePrd.mock.calls[0][1]).not.toHaveProperty("status");
  });

  it("offers draft and review, and shows approved and closed refused with their reasons", async () => {
    mocks.prd.mockResolvedValue(prd("review"));
    mocks.specPrd.mockResolvedValue(facts("review", false));
    show("/prds/GRPH-P9");
    const user = userEvent.setup();
    await screen.findByTestId("spec-facts");
    await user.click(screen.getByRole("button", { name: /Review/ }));
    const approved = await screen.findByTestId("status-refused-approved");
    expect(approved).toHaveAttribute("data-disabled");
    expect(approved).toHaveTextContent("Finishing the grill approves a PRD");
    expect(screen.getByTestId("status-refused-closed")).toHaveTextContent("Closing the PRD closes it");
  });

  it("shows the PRD's design set by prompt state, and opens it", async () => {
    mocks.prd.mockResolvedValue(prd("approved"));
    mocks.specPrd.mockResolvedValue(facts("approved", true, set(true, "stale")));
    mocks.specDesignSet.mockResolvedValue(set(true, "stale"));
    show("/prds/GRPH-P9");
    expect(await screen.findByTestId("prd-design-set-counts")).toHaveTextContent("0 accepted · 1 stale · 0 no prompt yet");
    await userEvent.setup().click(screen.getByRole("button", { name: "Open design set" }));
    expect(await screen.findByTestId("constraints-reader")).toBeInTheDocument();
  });

  it("lists linked items and says every one is done when done ones are hidden", async () => {
    mocks.prd.mockResolvedValue(prd("draft", ["GRPH-1"]));
    mocks.specPrd.mockResolvedValue(facts("draft", false));
    mocks.items.mockResolvedValue([{ id: "GRPH-1", title: "Build", status: "done" }] as never);
    show("/prds/GRPH-P9");
    const user = userEvent.setup();
    await screen.findByTestId("spec-facts");
    await user.click(within(screen.getByTestId("prd-editor-tabs")).getByRole("button", { name: /Requests/ }));
    expect(await screen.findByText("Build")).toBeInTheDocument();
    await user.click(screen.getByTestId("hide-done"));
    expect(screen.getByTestId("all-done")).toHaveTextContent("Every linked item is done.");
  });

  it("Back returns to Specs", async () => {
    mocks.prd.mockResolvedValue(prd("draft"));
    mocks.specPrd.mockResolvedValue(facts("draft", false));
    show("/prds/GRPH-P9");
    await screen.findByTestId("spec-facts");
    await userEvent.setup().click(screen.getByRole("button", { name: "Specs" }));
    expect(await screen.findByText("specs list")).toBeInTheDocument();
  });
});

describe("Preamble document", () => {
  it("says not found for a missing row instead of a blank document", async () => {
    mocks.specPreamble.mockRejectedValue(new NotFoundError("no preamble"));
    show("/prds/preamble");
    expect(await screen.findByTestId("spec-not-found")).toHaveTextContent("was not found");
  });

  it("says a pending body is pending", async () => {
    mocks.specPreamble.mockResolvedValue(preamble({ body_state: "pending", body: null }));
    show("/prds/preamble");
    expect(await screen.findByTestId("body-pending")).toBeInTheDocument();
  });

  it("says an unreadable body is a read failure, not an empty spec", async () => {
    mocks.specPreamble.mockResolvedValue(preamble({ body_state: "unreadable", body: null }));
    show("/prds/preamble");
    expect(await screen.findByTestId("body-unreadable")).toHaveTextContent("read failure");
  });

  it("shows a stored empty body as the body", async () => {
    mocks.specPreamble.mockResolvedValue(preamble({ body: "" }));
    show("/prds/preamble");
    expect(await screen.findByTestId("body-empty")).toBeInTheDocument();
  });

  it("has preview and history only, and save shows the new version and stale sets", async () => {
    mocks.specPreamble.mockResolvedValue(preamble());
    mocks.putPreamble.mockResolvedValue(preamble({
      version: 2, body: "Be bold.",
      stale_sets: [{ id: "DS-1", prd_id: "GRPH-P9", status: "stale", stale_reason: "preamble", preamble_version: 1 }],
    }));
    show("/prds/preamble");
    await screen.findByTestId("preamble-reader");
    const tabs = within(screen.getByTestId("spec-doc-tabs")).getAllByRole("button").map((b) => b.getAttribute("data-tab"));
    expect(tabs).toEqual(["preview", "history"]);
    expect(screen.queryByText(/Grill|Coverage|Acceptance|Assistant/)).not.toBeInTheDocument();

    const user = userEvent.setup();
    await user.click(screen.getByRole("button", { name: "Edit" }));
    const ed = screen.getByTestId("preamble-editor");
    await user.clear(ed);
    await user.type(ed, "Be bold.");
    await user.click(screen.getByRole("button", { name: "Save" }));
    await waitFor(() => expect(mocks.putPreamble).toHaveBeenCalledWith("core", "Be bold.", "GRPH-P1"));
    expect(await screen.findByTestId("preamble-version")).toHaveTextContent("v2");
    expect(screen.getByTestId("preamble-stale-sets")).toHaveTextContent("DS-1 · GRPH-P9 · pinned v1 · stale (preamble)");
  });
});

describe("Set constraints document", () => {
  it("has no Edit, names the person on Accept, and shows compile refused before acceptance", async () => {
    mocks.specDesignSet.mockResolvedValue(set(false));
    mocks.acceptSetConstraints.mockResolvedValue(set(true));
    show("/prds/constraints/DS-1");
    await screen.findByTestId("constraints-reader");
    expect(screen.getByTestId("constraints-edit")).toBeDisabled();
    expect(screen.getByTestId("constraints-note")).toHaveTextContent("accepted as written, not hand-edited");
    expect(screen.getByTestId("constraints-acceptance")).toHaveTextContent("not accepted");
    expect(screen.getByTestId("screen-compile")).toHaveTextContent("compile refused: the set constraints are not accepted");

    const accept = await screen.findByRole("button", { name: "Accept as Dana Reyes" });
    await userEvent.setup().click(accept);
    await waitFor(() => expect(mocks.acceptSetConstraints).toHaveBeenCalledWith("DS-1"));
    expect(await screen.findByRole("button", { name: "Compile" })).toBeInTheDocument();
    expect(screen.getByTestId("constraints-acceptance")).toHaveTextContent("accepted");
  });

  it("says a stale prompt stays stale until compiled again after acceptance", async () => {
    mocks.specDesignSet.mockResolvedValue(set(true, "stale"));
    show("/prds/constraints/DS-1");
    expect(await screen.findByTestId("stale-until-compiled")).toHaveTextContent("stays stale until it is compiled again");
    expect(screen.getByTestId("screen-prompt")).toHaveTextContent("stale");
  });

  it("says a missing set was not found", async () => {
    mocks.specDesignSet.mockRejectedValue(new NotFoundError("design set not found"));
    show("/prds/constraints/DS-nope");
    expect(await screen.findByTestId("spec-not-found")).toHaveTextContent("Design set DS-nope was not found");
  });
});
