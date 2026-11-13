import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { NewProjectDialog } from "@/features/onboarding/NewProjectDialog";

/**
 * The tag field at project creation (PRD-13 / AL-260).
 *
 * Both derivation and the availability rule are server calls. The rule in particular is
 * not guessable from the client — it excludes tags this deployment previously held and
 * prefixes reserved by ids issued before tags existed — so these tests assert the form
 * *asks* rather than deciding for itself.
 */
const api = vi.hoisted(() => ({
  tagSuggestion: vi.fn(),
  tagCheck: vi.fn(),
  createProject: vi.fn(),
}));
vi.mock("@/lib/api", () => ({ api }));

vi.mock("@/features/ProjectContext", () => ({
  useProjectCtx: () => ({ setActiveId: vi.fn() }),
}));

vi.mock("@/lib/queries", () => ({
  keys: { projects: ["projects"] },
  useConfirmSetup: () => ({ mutateAsync: vi.fn(), isPending: false }),
}));

function renderDialog() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={qc}>
      <NewProjectDialog open onOpenChange={() => {}} />
    </QueryClientProvider>,
  );
}

const tagInput = () => screen.getByLabelText("Tag") as HTMLInputElement;

beforeEach(() => {
  vi.clearAllMocks();
  api.tagSuggestion.mockResolvedValue({ tag: "GW" });
  api.tagCheck.mockResolvedValue({ tag: "GW", available: true, reason: "" });
  api.createProject.mockResolvedValue({ id: "graph-widgets", name: "Graph Widgets", tag: "GW" });
});

describe("project tag field", () => {
  it("prefills the tag from the name using the server's derivation", async () => {
    const user = userEvent.setup();
    renderDialog();

    await user.type(screen.getByPlaceholderText("e.g. Core Platform"), "Graph Widgets");
    await waitFor(() => expect(api.tagSuggestion).toHaveBeenCalledWith("Graph Widgets"));
    await waitFor(() => expect(tagInput().value).toBe("GW"));
  });

  it("stops following the name once the tag has been edited", async () => {
    const user = userEvent.setup();
    renderDialog();

    await user.type(screen.getByPlaceholderText("e.g. Core Platform"), "Graph Widgets");
    await waitFor(() => expect(tagInput().value).toBe("GW"));

    await user.clear(tagInput());
    await user.type(tagInput(), "ZED");
    await user.type(screen.getByPlaceholderText("e.g. Core Platform"), " Renamed");

    // A deliberate choice must survive a later edit to the name.
    await waitFor(() => expect(tagInput().value).toBe("ZED"));
  });

  it("uppercases and caps input at four characters", async () => {
    const user = userEvent.setup();
    renderDialog();

    await user.type(tagInput(), "abcdefg");
    expect(tagInput().value).toBe("ABCD");
  });

  it("shows the server's reason and blocks submit when the tag is unavailable", async () => {
    const user = userEvent.setup();
    api.tagCheck.mockResolvedValue({
      tag: "PRD",
      available: false,
      reason: "reserved by ids issued before project tags existed",
    });
    renderDialog();

    await user.type(screen.getByPlaceholderText("e.g. Core Platform"), "Product Docs");
    await user.clear(tagInput());
    await user.type(tagInput(), "PRD");

    await waitFor(() =>
      expect(
        screen.getByText("reserved by ids issued before project tags existed"),
      ).toBeInTheDocument(),
    );

    expect(screen.getByRole("button", { name: /next/i })).toBeDisabled();
    expect(api.createProject).not.toHaveBeenCalled();
  });

  it("submits the chosen tag", async () => {
    const user = userEvent.setup();
    renderDialog();

    await user.type(screen.getByPlaceholderText("e.g. Core Platform"), "Graph Widgets");
    await waitFor(() => expect(tagInput().value).toBe("GW"));
    await waitFor(() => expect(screen.getByRole("button", { name: /next/i })).toBeEnabled());

    for (let i = 0; i < 3; i++) await user.click(screen.getByRole("button", { name: /next/i }));
    await user.click(screen.getByRole("button", { name: /create project/i }));
    await waitFor(() =>
      expect(api.createProject).toHaveBeenCalledWith(
        expect.objectContaining({ name: "Graph Widgets", tag: "GW" }),
      ),
    );
  });

  it("previews what keys will look like", async () => {
    const user = userEvent.setup();
    renderDialog();

    await user.type(tagInput(), "GRPH");
    expect(await screen.findByText("GRPH-12")).toBeInTheDocument();
    expect(screen.getByText("GRPH-R33")).toBeInTheDocument();
    expect(screen.getByText("GRPH-P4")).toBeInTheDocument();
  });
});
