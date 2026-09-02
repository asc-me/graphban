import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

/** The prototype handoff UI (GRPH-235).
 *
 *  The loop AL-68 left half-open: a high-fidelity item was MARKED and a human was TOLD.
 *  These tests pin the two behaviors that make the difference between a loop and a
 *  button: the verdict (not the pixels) goes back to the grill with the artifact URL,
 *  and the fidelity flip is a PROPOSAL the author confirms — the UI must never present
 *  it as automatic, and must never apply it without the click. */

const mocks = vi.hoisted(() => ({
  emit: vi.fn(), verdict: vi.fn(), updateItem: vi.fn(), upload: vi.fn(),
}));

vi.mock("@/lib/api", () => ({
  api: {
    prdPrototypeEmit: mocks.emit,
    prdPrototypeVerdict: mocks.verdict,
    updateItem: mocks.updateItem,
  },
}));

vi.mock("@/lib/publicApi", () => ({
  publicApi: { uploadAttachment: mocks.upload },
}));

const { PrototypeRow } = await import("@/features/prds/PrdEditorView");

// PrototypeRow reads id/title/fidelity only; the full tracker shape is irrelevant here
// and a 20-field builder would only test that TypeScript exists.
const highItem = { id: "GB-1", title: "Sync link first-run", fidelity: "high", status: "backlog" } as never;

function verdictOut(over: object = {}) {
  return {
    prd: "PRD-31", item: "GB-1", dimension: "open_decisions", turn_seq: 7,
    artifact_url: "/api/public/attachments/att_1", fidelity: "high",
    fidelity_proposal: { item: "GB-1", from: "high", to: "low", confirmed: false, how: "PATCH" },
    ...over,
  };
}

function setup() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={qc}>
      <PrototypeRow prdId="PRD-31" item={highItem} />
    </QueryClientProvider>,
  );
}

async function openPack(user: ReturnType<typeof userEvent.setup>) {
  mocks.emit.mockResolvedValue({
    prd: "PRD-31", item: "GB-1", dimension: "open_decisions", turn_seq: 3,
    prompt_pack: "PASTE ME INTO THE DESIGN TOOL",
  });
  await user.click(screen.getByRole("button", { name: /Prototype this/i }));
  await screen.findByText(/PASTE ME INTO THE DESIGN TOOL/);
}

describe("prototype handoff", () => {
  it("emits the prompt-pack for the item", async () => {
    const user = userEvent.setup();
    setup();
    await openPack(user);
    expect(mocks.emit).toHaveBeenCalledWith("PRD-31", { item_id: "GB-1" });
    expect(screen.getByText(/Bring the verdict back/i)).toBeInTheDocument();
  });

  it("refuses a verdict with no screenshot attached", async () => {
    const user = userEvent.setup();
    setup();
    await openPack(user);
    await user.type(screen.getByRole("textbox"), "the placement settles it");
    await user.click(screen.getByRole("button", { name: /Send verdict to grill/i }));
    expect(await screen.findByText(/attach the screenshot/i)).toBeInTheDocument();
    expect(mocks.verdict).not.toHaveBeenCalled();
  });

  it("carries the uploaded artifact id into the verdict call", async () => {
    const user = userEvent.setup();
    setup();
    await openPack(user);
    mocks.upload.mockResolvedValue({ id: "att_1", url: "/api/public/attachments/att_1" });
    mocks.verdict.mockResolvedValue(verdictOut());
    const file = new File(["png"], "screen.png", { type: "image/png" });
    await user.upload(document.querySelector('input[type="file"]')!, file);
    await user.type(screen.getByRole("textbox"), "one rail entry, no modal");
    await user.click(screen.getByRole("button", { name: /Send verdict to grill/i }));
    await screen.findByText(/Verdict recorded/i);
    expect(mocks.upload).toHaveBeenCalledTimes(1);
    expect(mocks.verdict).toHaveBeenCalledWith("PRD-31", {
      item_id: "GB-1", attachment_id: "att_1", verdict: "one rail entry, no modal",
    });
  });

  it("proposes the flip and applies it only on the click", async () => {
    const user = userEvent.setup();
    setup();
    await openPack(user);
    mocks.upload.mockResolvedValue({ id: "att_1", url: "/api/public/attachments/att_1" });
    mocks.verdict.mockResolvedValue(verdictOut());
    mocks.updateItem.mockResolvedValue({ id: "GB-1", title: "Sync link first-run", fidelity: "low", status: "backlog" });
    const file = new File(["png"], "screen.png", { type: "image/png" });
    await user.upload(document.querySelector('input[type="file"]')!, file);
    await user.type(screen.getByRole("textbox"), "settled");
    await user.click(screen.getByRole("button", { name: /Send verdict to grill/i }));
    await screen.findByText(/Verdict recorded/i);

    // Nothing has flipped yet — the verdict call alone must not touch fidelity.
    expect(mocks.updateItem).not.toHaveBeenCalled();

    await user.click(screen.getByRole("button", { name: /Set fidelity low/i }));
    await waitFor(() =>
      expect(mocks.updateItem).toHaveBeenCalledWith("GB-1", { fidelity: "low" }),
    );
  });

  it("offers no flip button once the proposal is absent (already low)", async () => {
    const user = userEvent.setup();
    setup();
    await openPack(user);
    mocks.upload.mockResolvedValue({ id: "att_1", url: "/api/public/attachments/att_1" });
    mocks.verdict.mockResolvedValue(verdictOut({ fidelity: "low", fidelity_proposal: null }));
    const file = new File(["png"], "screen.png", { type: "image/png" });
    await user.upload(document.querySelector('input[type="file"]')!, file);
    await user.type(screen.getByRole("textbox"), "settled");
    await user.click(screen.getByRole("button", { name: /Send verdict to grill/i }));
    await screen.findByText(/Verdict recorded/i);
    expect(screen.queryByRole("button", { name: /Set fidelity low/i })).not.toBeInTheDocument();
  });
});
