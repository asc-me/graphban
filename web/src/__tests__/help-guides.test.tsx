import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, fireEvent, render, screen, within } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, describe, expect, it, vi } from "vitest";

import { DocsReader, openDocsReader } from "@/features/docs/DocsReader";
import { QuickStartDialog } from "@/features/onboarding/QuickStartDialog";
import { onGuideRequested, onTourRequested } from "@/features/tour/replay";

/** GRPH-1512 — Help lists exactly three guides, and each opens what it names. */

vi.mock("@/features/ProjectContext", () => ({
  ProjectProvider: ({ children }: { children: React.ReactNode }) => <>{children}</>,
  useProjectCtx: () => ({ projects: [{ id: "core", name: "Core" }], loading: false, active: { id: "core", name: "Core" }, notFound: false }),
}));
// Every read stays pending: the guide screens need none of them.
vi.mock("@/lib/api", () => ({
  hasSession: () => true,
  api: new Proxy({}, { get: () => () => new Promise(() => {}) }),
}));

afterEach(() => {
  cleanup();
  localStorage.clear();
});

function renderHelp(hasProject = true) {
  render(
    <MemoryRouter initialEntries={["/tracker"]}>
      <DocsReader hasProject={hasProject} />
    </MemoryRouter>,
  );
  openDocsReader();
}

async function guideButtons() {
  const nav = await screen.findByRole("navigation", { name: "Guides" });
  return within(nav).getAllByRole("button");
}

describe("GRPH-1512 Help ▸ three guides", () => {
  it("lists exactly Concepts, Take the tour, Start-up prompts, Setup steps, in that order", async () => {
    renderHelp();
    const names = (await guideButtons()).map((b) => b.textContent);
    expect(names).toEqual(["Concepts", "Take the tour", "Start-up prompts", "Setup steps"]);
  });

  it("Concepts and Start-up prompts request their guide; Take the tour starts the tour", async () => {
    const guide = vi.fn();
    const tour = vi.fn();
    const offG = onGuideRequested(guide);
    const offT = onTourRequested(tour);
    renderHelp();
    fireEvent.click((await guideButtons())[0]);
    expect(guide).toHaveBeenLastCalledWith("concepts");
    openDocsReader();
    fireEvent.click((await guideButtons())[1]);
    expect(tour).toHaveBeenCalledTimes(1);
    openDocsReader();
    fireEvent.click((await guideButtons())[2]);
    expect(guide).toHaveBeenLastCalledWith("prompts");
    expect(guide).toHaveBeenCalledTimes(2);
    openDocsReader();
    fireEvent.click((await guideButtons())[3]);
    expect(guide).toHaveBeenLastCalledWith("sections");
    offG();
    offT();
  });

  it("without a project the tour entry stays listed, disabled with why", async () => {
    const tour = vi.fn();
    const off = onTourRequested(tour);
    renderHelp(false);
    const buttons = await guideButtons();
    expect(buttons).toHaveLength(4);
    expect(buttons[1]).toBeDisabled();
    expect(buttons[1]).toHaveAttribute("title", expect.stringMatching(/project/i));
    fireEvent.click(buttons[1]);
    expect(tour).not.toHaveBeenCalled();
    off();
  });
});

function renderDialog(initialGuide: "concepts" | "prompts" | "sections") {
  render(
    <QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}>
      <MemoryRouter>
        <QuickStartDialog open initialGuide={initialGuide} hosted={false} onFinishLater={() => {}} onFinish={async () => {}} />
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

describe("GRPH-1512 the dialog lands on the guide Help asked for", () => {
  it("Concepts opens the concepts panels at concept 1", async () => {
    renderDialog("concepts");
    const panel = (await screen.findByRole("list", { name: "Concepts" })).closest("section");
    expect(panel).toHaveAttribute("data-concept", "0");
    expect(within(panel as HTMLElement).getAllByRole("listitem")[0]).toHaveAttribute("aria-current", "step");
    expect(screen.queryByTestId("qs-prompts")).toBeNull();
  });

  it("Start-up prompts opens the prompts screen", async () => {
    renderDialog("prompts");
    const prompts = await screen.findByTestId("qs-prompts");
    expect(within(prompts).getByRole("heading", { name: "Start-up prompts" })).toBeInTheDocument();
    expect(within(prompts).getByRole("list", { name: "Start-up prompts" })).toBeInTheDocument();
    expect(document.querySelector("[data-concept]")).toBeNull();
  });

  it("GRPH-1546: after Finish, Help's Setup steps entry reaches Sections", async () => {
    localStorage.clear();
    renderHelp();
    const names = (await guideButtons()).map((b) => b.textContent);
    expect(names).toContain("Setup steps");
    cleanup();
    renderDialog("sections");
    expect(await screen.findByRole("list", { name: "Sections" })).toBeInTheDocument();
    expect(screen.queryByTestId("qs-prompts")).toBeNull();
  });
});
