import { readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import * as React from "react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { NewProjectWizard, PROJECT_ACCENTS, PROJECT_ACCENT_SWATCHES, radioArrows } from "@/features/onboarding/NewProjectWizard";
import { projectFill } from "@/lib/projectFill";

function mockReducedMotion(reduce: boolean) {
  Object.defineProperty(window, "matchMedia", {
    configurable: true,
    writable: true,
    value: (q: string) => ({
      matches: reduce && q.includes("prefers-reduced-motion"),
      media: q,
      onchange: null,
      addListener: vi.fn(),
      removeListener: vi.fn(),
      addEventListener: vi.fn(),
      removeEventListener: vi.fn(),
      dispatchEvent: vi.fn(),
    }),
  });
}

const caption = () => screen.getByTestId("pw-caption").textContent;
const animated = () =>
  [...screen.getByTestId("pw-hero").querySelectorAll<SVGElement>("*")].filter((el) => !!el.style.animation);

const api = vi.hoisted(() => ({
  tagSuggestion: vi.fn(),
  tagCheck: vi.fn(),
  createProject: vi.fn(),
  updateProject: vi.fn(),
  createLine: vi.fn(),
  createCredential: vi.fn(),
  setProjectCredential: vi.fn(),
}));
vi.mock("@/lib/api", () => ({ api }));
const setActiveId = vi.hoisted(() => vi.fn());
vi.mock("@/features/ProjectContext", () => ({ useProjectCtx: () => ({ setActiveId }) }));
const confirm = vi.hoisted(() => vi.fn());
vi.mock("@/lib/queries", () => ({
  keys: { projects: ["projects"] },
  useConfirmSetup: () => ({ mutateAsync: confirm, isPending: false }),
}));

function renderWizard(onOpenChange = vi.fn()) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  render(
    <QueryClientProvider client={qc}>
      <NewProjectWizard open onOpenChange={onOpenChange} />
    </QueryClientProvider>,
  );
  return onOpenChange;
}

const next = () => screen.getByRole("button", { name: /next/i });

// TagField debounces the name suggestion and the availability check by 250ms each.
// Next enables only after that check has answered. A waitFor on the button spends
// Testing Library's 1s budget on those timers and fails when this file runs beside
// another (GRPH-1771). Advance the clock instead of waiting out wall time.
const TAG_DEBOUNCE_MS = 250;

async function answerTagCheck(name: string, tag: string) {
  vi.useFakeTimers({ toFake: ["setTimeout", "clearTimeout", "setInterval", "clearInterval", "Date"] });
  try {
    fireEvent.change(screen.getByLabelText("Project name"), { target: { value: name } });
    await act(async () => {
      await vi.advanceTimersByTimeAsync(TAG_DEBOUNCE_MS);
    });
    await act(async () => {
      await vi.advanceTimersByTimeAsync(TAG_DEBOUNCE_MS);
    });
    expect(api.tagCheck).toHaveBeenCalledWith(tag);
  } finally {
    vi.useRealTimers();
  }
}

async function toRepo(user: ReturnType<typeof userEvent.setup>) {
  await answerTagCheck("Core Platform", "CORE");
  // The available answer for the typed name is what enables Next.
  expect(screen.getByText("CORE-12")).toBeInTheDocument();
  expect(next()).toBeEnabled();
  await user.click(next());
}

beforeEach(() => {
  vi.clearAllMocks();
  mockReducedMotion(false);
  api.tagSuggestion.mockResolvedValue({ tag: "CORE" });
  api.tagCheck.mockResolvedValue({ tag: "CORE", available: true, reason: "" });
  api.createProject.mockResolvedValue({ id: "core", name: "Core Platform", tag: "CORE" });
  api.updateProject.mockResolvedValue({});
  api.createLine.mockResolvedValue({ id: "CORE-L1", key: "CORE-L1", title: "Maintenance" });
  confirm.mockResolvedValue({});
});

describe("New project wizard", () => {
  it("shows the five-step rail and blocks Next until the identity is valid", async () => {
    renderWizard();
    expect(screen.getByRole("list", { name: "Steps" })).toBeInTheDocument();
    for (const l of ["Name & tag", "Repository", "Maintenance", "Model", "Review"]) expect(screen.getAllByText(l).length).toBeGreaterThan(0);
    expect(next()).toBeDisabled();
    await answerTagCheck("Core Platform", "CORE");
    expect(screen.getByText("CORE-12")).toBeInTheDocument();
    expect(next()).toBeEnabled();
  });

  it("blocks Next when the tag is taken", async () => {
    api.tagCheck.mockResolvedValue({ tag: "CORE", available: false, reason: "taken" });
    renderWizard();
    await answerTagCheck("Core Platform", "CORE");
    expect(screen.getByText("taken")).toBeInTheDocument();
    expect(screen.queryByText("CORE-12")).not.toBeInTheDocument();
    expect(next()).toBeDisabled();
  });

  it("strips symbols from the tag as you type", async () => {
    const user = userEvent.setup();
    renderWizard();
    const tag = screen.getByLabelText("Tag");
    await user.clear(tag);
    await user.type(tag, "a-b !");
    expect(tag).toHaveValue("AB");
  });

  it("shows the format hint, not the server string, for an invalid tag", async () => {
    const user = userEvent.setup();
    api.tagCheck.mockResolvedValue({ tag: "A", available: false, reason: "tag must be 2-4 characters (got 'A')" });
    renderWizard();
    const tag = screen.getByLabelText("Tag");
    await user.clear(tag);
    await user.type(tag, "a");
    expect(screen.getByText("2–4 letters or digits, starting with a letter")).toBeInTheDocument();
    await waitFor(() => expect(api.tagCheck).toHaveBeenCalled());
    expect(screen.queryByText(/got 'A'/)).not.toBeInTheDocument();
  });

  it("shows a message when the availability check fails", async () => {
    api.tagCheck.mockRejectedValue(new Error("network"));
    renderWizard();
    await answerTagCheck("Core Platform", "CORE");
    expect(screen.getByText(/Couldn’t check whether this tag is available/)).toBeInTheDocument();
    expect(next()).toBeDisabled();
  });

  it("skips an empty remote and blocks an invalid one", async () => {
    const user = userEvent.setup();
    renderWizard();
    await toRepo(user);
    await user.type(screen.getByLabelText("Git remote"), "nonsense");
    expect(next()).toBeDisabled();
    expect(screen.getByText(/doesn’t look like a git remote/)).toBeInTheDocument();
    await user.clear(screen.getByLabelText("Git remote"));
    await user.click(screen.getByRole("button", { name: "Skip" }));
    expect(screen.getByRole("heading", { name: "Add a maintenance line?" })).toBeInTheDocument();
  });

  it("lists six review rows, Edit goes back, and Create saves name, tag, accent, repo and model", async () => {
    const user = userEvent.setup();
    renderWizard();
    await toRepo(user);
    await user.type(screen.getByLabelText("Git remote"), "git@github.com:acme/core.git");
    await user.click(next());
    await user.click(next());
    await user.click(next());
    for (const k of ["NAME", "TAG", "ACCENT", "REPOSITORY", "MAINTENANCE LINE", "MODEL"]) expect(screen.getByText(k)).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Edit repository" }));
    expect(screen.getByRole("heading", { name: "Point it at a repository" })).toBeInTheDocument();
    await user.click(next());
    await user.click(next());
    await user.click(next());
    await user.click(screen.getByRole("button", { name: /create project/i }));
    await waitFor(() =>
      expect(api.createProject).toHaveBeenCalledWith({
        name: "Core Platform",
        tag: "CORE",
        accent: "#c6f24e",
        accent2: null,
        pattern: "none",
      }),
    );
    expect(api.updateProject).toHaveBeenCalledWith("core", { repo_url: "git@github.com:acme/core.git" });
    expect(confirm).toHaveBeenCalledWith({ projectId: "core", body: { decision: "offline" } });
    expect(api.createLine).not.toHaveBeenCalled();
    expect(await screen.findByText("Core Platform is ready.")).toBeInTheDocument();
    expect(screen.getByText("gban setup")).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Open project" }));
    expect(setActiveId).toHaveBeenCalledWith("core");
  });

  it("Create another resets to step one", async () => {
    const user = userEvent.setup();
    renderWizard();
    await toRepo(user);
    await user.click(next());
    await user.click(next());
    await user.click(next());
    await user.click(screen.getByRole("button", { name: /create project/i }));
    await user.click(await screen.findByRole("button", { name: "Create another" }));
    expect((screen.getByLabelText("Project name") as HTMLInputElement).value).toBe("");
  });

  it("stores an Ollama choice through the credential writer", async () => {
    const user = userEvent.setup();
    api.createCredential.mockResolvedValue({ id: "c1", state: "valid" });
    api.setProjectCredential.mockResolvedValue({});
    renderWizard();
    await toRepo(user);
    await user.click(next());
    await user.click(next());
    await user.click(screen.getByRole("radio", { name: /Local/ }));
    await user.click(next());
    await user.click(screen.getByRole("button", { name: /create project/i }));
    await waitFor(() => expect(api.createCredential).toHaveBeenCalledWith("core", expect.objectContaining({ kind: "ollama" })));
    expect(api.setProjectCredential).toHaveBeenCalledWith("core", { credential_id: "c1" });
  });

  it("shows the error line on a failed create and keeps the values", async () => {
    const user = userEvent.setup();
    api.createProject.mockRejectedValueOnce(new Error("boom"));
    renderWizard();
    await toRepo(user);
    await user.click(next());
    await user.click(next());
    await user.click(next());
    await user.click(screen.getByRole("button", { name: /create project/i }));
    expect(await screen.findByText("Could not create the project. Please try again.")).toBeInTheDocument();
    expect(screen.getByText("Core Platform")).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: /create project/i }));
    await waitFor(() => expect(api.createProject).toHaveBeenCalledTimes(2));
  });

  it("after a partial failure it names the created project, locks name/tag, and retries only the failed write", async () => {
    const user = userEvent.setup();
    api.updateProject.mockRejectedValueOnce(new Error("boom"));
    renderWizard();
    await toRepo(user);
    await user.type(screen.getByLabelText("Git remote"), "git@github.com:acme/core.git");
    await user.click(next());
    await user.click(next());
    await user.click(next());
    await user.click(screen.getByRole("button", { name: /create project/i }));
    expect(await screen.findByText(/Core Platform was created, but a follow-up step failed/)).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Edit name" })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Edit tag" })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Edit accent" })).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Edit repository" })).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: /create project/i }));
    expect(await screen.findByText("Core Platform is ready.")).toBeInTheDocument();
    expect(api.createProject).toHaveBeenCalledTimes(1);
    expect(api.updateProject).toHaveBeenCalledTimes(2);
    expect(api.updateProject).toHaveBeenLastCalledWith("core", { repo_url: "git@github.com:acme/core.git" });
  });

  it("after a partial failure the graphic does not say NOT CREATED or draw the broken line (GRPH-1612)", async () => {
    const user = userEvent.setup();
    api.updateProject.mockRejectedValueOnce(new Error("boom"));
    renderWizard();
    await toRepo(user);
    await user.type(screen.getByLabelText("Git remote"), "git@github.com:acme/core.git");
    await user.click(next());
    await user.click(next());
    await user.click(next());
    await user.click(screen.getByRole("button", { name: /create project/i }));
    await screen.findByText(/was created, but a follow-up step failed/);
    expect(caption()).not.toBe("NOT CREATED");
    expect(screen.getByTestId("pw-hero").querySelector('path[stroke-dasharray="4 6"]')).toBeNull();
  });

  it("re-reads the project list when Cancel closes the wizard after a partial failure (GRPH-1612)", async () => {
    const user = userEvent.setup();
    const invalidate = vi.spyOn(QueryClient.prototype, "invalidateQueries");
    api.updateProject.mockRejectedValueOnce(new Error("boom"));
    const onOpenChange = renderWizard();
    await toRepo(user);
    await user.type(screen.getByLabelText("Git remote"), "git@github.com:acme/core.git");
    await user.click(next());
    await user.click(next());
    await user.click(next());
    await user.click(screen.getByRole("button", { name: /create project/i }));
    await screen.findByText(/was created, but a follow-up step failed/);
    expect(invalidate).not.toHaveBeenCalledWith({ queryKey: ["projects"] });
    await user.click(screen.getByRole("button", { name: "Cancel" }));
    expect(invalidate).toHaveBeenCalledWith({ queryKey: ["projects"] });
    expect(onOpenChange).toHaveBeenCalledWith(false);
    invalidate.mockRestore();
  });

  it("locks the identity step after a partial failure so edits cannot be dropped", async () => {
    const user = userEvent.setup();
    api.updateProject.mockRejectedValueOnce(new Error("boom"));
    renderWizard();
    await toRepo(user);
    await user.type(screen.getByLabelText("Git remote"), "git@github.com:acme/core.git");
    await user.click(next());
    await user.click(next());
    await user.click(next());
    await user.click(screen.getByRole("button", { name: /create project/i }));
    await screen.findByText(/was created, but a follow-up step failed/);
    await user.click(screen.getByRole("button", { name: "Back" }));
    await user.click(screen.getByRole("button", { name: "Back" }));
    await user.click(screen.getByRole("button", { name: "Back" }));
    await user.click(screen.getByRole("button", { name: "Back" }));
    expect(screen.getByLabelText("Project name")).toBeDisabled();
  });

  describe("radio groups follow the WAI-ARIA keyboard model (GRPH-1613)", () => {
    const checked = () => screen.getAllByRole("radio").find((r) => r.getAttribute("aria-checked") === "true");
    const checkedIn = (name: string) =>
      within(screen.getByRole("radiogroup", { name })).getAllByRole("radio").find((r) => r.getAttribute("aria-checked") === "true");
    const tabStops = () => screen.getAllByRole("radio").filter((r) => r.tabIndex === 0);

    it("moves the Model selection with ↑ and ↓, wrapping at both ends, and focus follows", async () => {
      const user = userEvent.setup();
      renderWizard();
      await toRepo(user);
      await user.click(next());
      await user.click(next());
      const offline = screen.getByRole("radio", { name: /Offline/ });
      offline.focus();
      await user.keyboard("{ArrowDown}");
      expect(checked()).toHaveAccessibleName(/Local/);
      expect(checked()).toHaveFocus();
      await user.keyboard("{ArrowDown}");
      expect(checked()).toHaveAccessibleName(/Cloud/);
      await user.keyboard("{ArrowDown}");
      expect(checked()).toHaveAccessibleName(/Offline/);
      expect(checked()).toHaveFocus();
      await user.keyboard("{ArrowUp}");
      expect(checked()).toHaveAccessibleName(/Cloud/);
      expect(checked()).toHaveFocus();
      // GRPH-1618: the pattern's keys work on both axes — → is next and ← is previous here too.
      await user.keyboard("{ArrowRight}");
      expect(checked()).toHaveAccessibleName(/Offline/);
      expect(checked()).toHaveFocus();
      await user.keyboard("{ArrowLeft}");
      expect(checked()).toHaveAccessibleName(/Cloud/);
      await user.keyboard("{Home}");
      expect(checked()).toHaveAccessibleName(/Offline/);
      expect(checked()).toHaveFocus();
      await user.keyboard("{End}");
      expect(checked()).toHaveAccessibleName(/Cloud/);
      expect(checked()).toHaveFocus();
    });

    it("draws the selected accent with a box-shadow ring and leaves outline to focus-visible (GRPH-1745)", async () => {
      const user = userEvent.setup();
      renderWizard();
      const selected = screen.getByRole("radio", { name: "Lime" });
      const other = screen.getByRole("radio", { name: "Violet" });
      // An inline outline beats the global :focus-visible rule. Neither swatch may set one.
      expect(selected.style.outline).toBe("");
      expect(other.style.outline).toBe("");
      expect(selected).toHaveStyle({
        boxShadow: "0 0 0 2px var(--color-surface-3), 0 0 0 4px var(--color-fg)",
        outlineOffset: "5px",
      });
      expect(other.style.boxShadow).toBe("");
      expect(other).toHaveStyle({ outlineOffset: "5px" });
      // The focus ring starts outside the 4px selection ring, so the two do not overlap.
      const offset = Number.parseFloat(selected.style.outlineOffset);
      expect(offset).toBeGreaterThan(4);

      selected.focus();
      expect(selected).toHaveFocus();
      expect(selected.style.outline).toBe("");
      await user.click(other);
      expect(other).toHaveAttribute("aria-checked", "true");
      expect(other.style.outline).toBe("");
      expect(other).toHaveStyle({
        boxShadow: "0 0 0 2px var(--color-surface-3), 0 0 0 4px var(--color-fg)",
        outlineOffset: "5px",
      });
      expect(selected.style.outline).toBe("");
      expect(selected.style.boxShadow).toBe("");
    });

    it("moves the Accent selection with ← and →, wrapping at both ends, and focus follows", async () => {
      const user = userEvent.setup();
      renderWizard();
      const first = screen.getByRole("radio", { name: "Lime" });
      expect(first).toHaveAttribute("aria-checked", "true");
      first.focus();
      await user.keyboard("{ArrowLeft}");
      expect(checkedIn("Accent colour")).toHaveAccessibleName("Yellow");
      expect(checkedIn("Accent colour")).toHaveFocus();
      await user.keyboard("{ArrowRight}");
      expect(checkedIn("Accent colour")).toHaveAccessibleName("Lime");
      await user.keyboard("{ArrowRight}");
      expect(checkedIn("Accent colour")).toHaveAccessibleName("Mint");
      expect(checkedIn("Accent colour")).toHaveFocus();
      // GRPH-1618: ↓ is next and ↑ is previous on this horizontal group too.
      await user.keyboard("{ArrowDown}");
      expect(checkedIn("Accent colour")).toHaveAccessibleName("Teal");
      expect(checkedIn("Accent colour")).toHaveFocus();
      await user.keyboard("{ArrowUp}");
      expect(checkedIn("Accent colour")).toHaveAccessibleName("Mint");
      await user.keyboard("{End}");
      expect(checkedIn("Accent colour")).toHaveAccessibleName("Yellow");
      expect(checkedIn("Accent colour")).toHaveFocus();
      await user.keyboard("{Home}");
      expect(checkedIn("Accent colour")).toHaveAccessibleName("Lime");
      expect(checkedIn("Accent colour")).toHaveFocus();
    });

    it("is one Tab stop per group, on the checked option, and the next Tab leaves the group", async () => {
      const user = userEvent.setup();
      renderWizard();
      // Solid identity: style, colour, pattern. Ombre adds the second colour as its own stop.
      expect(screen.getAllByRole("radiogroup")).toHaveLength(3);
      for (const group of screen.getAllByRole("radiogroup")) {
        const stops = within(group).getAllByRole("radio").filter((r) => r.tabIndex === 0);
        expect(stops).toHaveLength(1);
        expect(stops[0]).toHaveAttribute("aria-checked", "true");
      }
      await user.click(screen.getByRole("radio", { name: "Sky" }));
      const accentGroup = screen.getByRole("radiogroup", { name: "Accent colour" });
      expect(within(accentGroup).getAllByRole("radio").filter((r) => r.tabIndex === 0)).toEqual([
        screen.getByRole("radio", { name: "Sky" }),
      ]);
      screen.getByRole("radio", { name: "Sky" }).focus();
      await user.tab();
      expect(accentGroup.contains(document.activeElement)).toBe(false);
      expect(screen.getByRole("radiogroup", { name: "Pattern" }).contains(document.activeElement)).toBe(true);

      await user.clear(screen.getByLabelText("Project name"));
      await toRepo(user);
      await user.click(next());
      await user.click(next());
      await user.click(screen.getByRole("radio", { name: /Cloud/ }));
      expect(tabStops()).toEqual([screen.getByRole("radio", { name: /Cloud/ })]);
      screen.getByRole("radio", { name: /Cloud/ }).focus();
      await user.tab();
      expect(document.activeElement?.getAttribute("role")).not.toBe("radio");
    });

    it("ignores the arrows on the Accent group once it is locked after a partial failure", async () => {
      const user = userEvent.setup();
      api.updateProject.mockRejectedValueOnce(new Error("boom"));
      renderWizard();
      await toRepo(user);
      await user.type(screen.getByLabelText("Git remote"), "git@github.com:acme/core.git");
      await user.click(next());
      await user.click(next());
      await user.click(next());
      await user.click(screen.getByRole("button", { name: /create project/i }));
      await screen.findByText(/was created, but a follow-up step failed/);
      for (let i = 0; i < 4; i++) await user.click(screen.getByRole("button", { name: "Back" }));
      const locked = screen.getByRole("radio", { name: "Lime" });
      expect(locked).toBeDisabled();
      // A disabled radio cannot take focus in a browser; dispatch straight at it so the
      // handler's own lock is what is under test.
      fireEvent.keyDown(locked, { key: "ArrowRight" });
      expect(checkedIn("Accent colour")).toBe(locked);
      fireEvent.keyDown(locked, { key: "ArrowLeft" });
      expect(checkedIn("Accent colour")).toBe(locked);
      fireEvent.keyDown(locked, { key: "End" });
      expect(checkedIn("Accent colour")).toBe(locked);
      fireEvent.keyDown(locked, { key: "ArrowDown" });
      expect(checkedIn("Accent colour")).toBe(locked);
    });

    it("skips a disabled option on every key, wrapping past it (GRPH-1618)", async () => {
      const user = userEvent.setup();
      const values = ["a", "b", "c", "d"] as const;
      const off = new Set(["b", "d"]);
      function Group() {
        const [v, setV] = React.useState<(typeof values)[number]>("a");
        return (
          <div role="radiogroup" aria-label="g">
            {values.map((x) => (
              <button
                key={x}
                type="button"
                role="radio"
                aria-checked={v === x}
                aria-label={x}
                tabIndex={v === x ? 0 : -1}
                onKeyDown={(e) => radioArrows(e, { values, current: v, set: setV, disabled: (y) => off.has(y) })}
              />
            ))}
          </div>
        );
      }
      render(<Group />);
      screen.getByRole("radio", { name: "a" }).focus();
      await user.keyboard("{ArrowRight}");
      expect(checked()).toHaveAccessibleName("c");
      expect(checked()).toHaveFocus();
      await user.keyboard("{ArrowDown}");
      expect(checked()).toHaveAccessibleName("a");
      await user.keyboard("{ArrowLeft}");
      expect(checked()).toHaveAccessibleName("c");
      await user.keyboard("{End}");
      expect(checked()).toHaveAccessibleName("c");
      await user.keyboard("{Home}");
      expect(checked()).toHaveAccessibleName("a");
      expect(checked()).toHaveFocus();
    });
  });

  describe("matches the Project Wizard design (GRPH-1587)", () => {
    it("captions each step the way the design does", async () => {
      const user = userEvent.setup();
      renderWizard();
      expect(caption()).toBe("PROJECT · UNNAMED");
      await toRepo(user);
      expect(caption()).toBe("REPO · NONE YET");
      await user.type(screen.getByLabelText("Git remote"), "git@github.com:acme/core.git");
      expect(caption()).toBe("REPO → CORE");
      await user.click(next());
      expect(caption()).toBe("LINE · NONE · FIXES STAY UNSCOPED");
      await user.click(screen.getByRole("radio", { name: /Create the maintenance line/ }));
      expect(caption()).toBe("LINE · CORE-L1 · MAINTENANCE");
      await user.click(next());
      expect(caption()).toBe("MODEL · STUB");
      await user.click(screen.getByRole("radio", { name: /Local/ }));
      expect(caption()).toBe("MODEL · OLLAMA");
      await user.click(screen.getByRole("radio", { name: /Cloud/ }));
      expect(caption()).toBe("MODEL · CLAUDE");
    });

    it("draws the logo, the wordmark and an accent square beside the caption", async () => {
      const user = userEvent.setup();
      renderWizard();
      expect(screen.getByText("Graphban")).toBeInTheDocument();
      expect(screen.getByTestId("pw-accent-square")).toHaveStyle({ background: "#c6f24e" });
      await user.click(screen.getByRole("radio", { name: "Violet" }));
      expect(screen.getByTestId("pw-accent-square")).toHaveStyle({ background: "#a78bfa" });
    });

    it("animates the graphic, and draws it still under prefers-reduced-motion", () => {
      renderWizard();
      expect(animated().length).toBeGreaterThan(0);
      expect(screen.getByTestId("pw-body").style.animation).not.toBe("");
    });

    it("draws nothing animated under prefers-reduced-motion", () => {
      mockReducedMotion(true);
      renderWizard();
      expect(animated()).toEqual([]);
      expect(screen.getByTestId("pw-body").style.animation).toBe("");
    });

    it("focuses the name field on step one and the title on the others", async () => {
      const user = userEvent.setup();
      renderWizard();
      expect(screen.getByLabelText("Project name")).toHaveFocus();
      await toRepo(user);
      expect(screen.getByRole("heading", { name: "Point it at a repository" })).toHaveFocus();
    });

    it("lists the models as a vertical radio stack with the credential field inside the chosen card", async () => {
      const user = userEvent.setup();
      renderWizard();
      await toRepo(user);
      await user.click(next());
      await user.click(next());
      const group = screen.getByRole("radiogroup", { name: "Model" });
      expect(group.className).toContain("flex-col");
      expect(screen.getAllByRole("radio").map((r) => r.getAttribute("aria-checked"))).toEqual(["true", "false", "false"]);
      expect(screen.getByText("stub")).toBeInTheDocument();
      expect(screen.queryByLabelText("Ollama URL")).not.toBeInTheDocument();
      await user.click(screen.getByRole("radio", { name: /Local/ }));
      expect(screen.getByTestId("pw-model-ollama")).toContainElement(screen.getByLabelText("Ollama URL"));
      await user.click(screen.getByRole("radio", { name: /Cloud/ }));
      expect(screen.queryByLabelText("Ollama URL")).not.toBeInTheDocument();
      expect(screen.getByTestId("pw-model-claude")).toContainElement(screen.getByLabelText("Anthropic API key"));
    });

    it("shows the review as a bordered card with example keys and accent Edit links", async () => {
      const user = userEvent.setup();
      renderWizard();
      await toRepo(user);
      await user.click(screen.getByRole("button", { name: "Skip" }));
      await user.click(next());
      await user.click(next());
      expect(screen.getByTestId("pw-summary").className).toMatch(/\bborder\b.*rounded-\[12px\]|rounded-\[12px\].*\bborder\b/);
      expect(screen.getByText("CORE · CORE-12, CORE-R33")).toHaveClass("font-mono");
      expect(screen.getByText("None yet")).toHaveClass("text-faint");
      expect(screen.getByRole("button", { name: "Edit name" })).toHaveClass("text-accent");
    });

    it("puts the error icon on a failed create", async () => {
      const user = userEvent.setup();
      api.createProject.mockRejectedValueOnce(new Error("boom"));
      renderWizard();
      await toRepo(user);
      await user.click(next());
      await user.click(next());
      await user.click(next());
      await user.click(screen.getByRole("button", { name: /create project/i }));
      const alert = await screen.findByRole("alert");
      expect(alert.querySelector("svg")).not.toBeNull();
      expect(caption()).toBe("NOT CREATED");
    });

    it("orders the footer Cancel · Skip · ← Back · Next, and right-aligns the ready buttons", async () => {
      const user = userEvent.setup();
      renderWizard();
      await toRepo(user);
      const footer = screen.getByTestId("pw-footer");
      const labels = () => [...footer.querySelectorAll("button")].map((b) => b.textContent);
      expect(labels()).toEqual(["Cancel", "Skip", "← Back", "Next →"]);
      expect(screen.getByRole("button", { name: "Back" })).toBeInTheDocument();
      expect(screen.getByRole("button", { name: "Cancel" }).className).not.toContain("border");
      await user.click(screen.getByRole("button", { name: "Skip" }));
      await user.click(next());
      await user.click(next());
      await user.click(screen.getByRole("button", { name: /create project/i }));
      await screen.findByText("Core Platform is ready.");
      expect(caption()).toBe("CREATED · CORE");
      const kids = [...footer.children];
      expect(kids.map((k) => k.textContent)).toEqual(["", "Create another", "Open project"]);
    });

    it("maintenance line: defaults to Not now, lights the line when Create is on, and is one arrow-key tab stop (GRPH-1749)", async () => {
      const user = userEvent.setup();
      renderWizard();
      await toRepo(user);
      await user.click(next());
      expect(screen.getByRole("heading", { name: "Add a maintenance line?" })).toBeInTheDocument();
      const off = screen.getByRole("radio", { name: /Not now/ });
      const on = screen.getByRole("radio", { name: /Create the maintenance line/ });
      expect(off).toHaveAttribute("aria-checked", "true");
      expect(on).toHaveAttribute("aria-checked", "false");
      expect(screen.getByText(/Fixes with no PRD stay unscoped/)).toBeInTheDocument();
      expect(screen.queryByTestId("pw-line-how")).not.toBeInTheDocument();
      expect(caption()).toBe("LINE · NONE · FIXES STAY UNSCOPED");
      expect(screen.getByTestId("pw-hero").querySelector('path[stroke-dasharray="4 6"]')).not.toBeNull();

      const stops = () => screen.getAllByRole("radio").filter((r) => r.tabIndex === 0);
      expect(screen.getAllByRole("radio")).toHaveLength(2);
      expect(stops()).toEqual([off]);
      off.focus();
      await user.keyboard("{ArrowUp}");
      expect(on).toHaveAttribute("aria-checked", "true");
      expect(on).toHaveFocus();
      expect(stops()).toEqual([on]);
      expect(on).toHaveTextContent("CORE-L1");
      expect(screen.getByTestId("pw-line-how")).toHaveTextContent("HOW THE LINE WORKS");
      expect(screen.getByTestId("pw-line-how")).toHaveTextContent("filed against CORE-L1");
      expect(screen.getByTestId("pw-line-how")).toHaveTextContent("acceptance clause");
      expect(screen.getByTestId("pw-line-how")).toHaveTextContent("SECURITY chip");
      expect(screen.queryByText(/Fixes with no PRD stay unscoped/)).not.toBeInTheDocument();
      expect(caption()).toBe("LINE · CORE-L1 · MAINTENANCE");
      expect(screen.getByTestId("pw-hero").querySelector("[data-lit]")).not.toBeNull();
      expect(screen.getByTestId("pw-hero").querySelector('path[stroke-dasharray="4 6"]')).toBeNull();
      await user.keyboard("{ArrowDown}");
      expect(off).toHaveAttribute("aria-checked", "true");
      await user.keyboard("{Home}");
      expect(on).toHaveFocus();
      await user.keyboard("{End}");
      expect(off).toHaveFocus();
      await user.tab();
      expect(document.activeElement?.getAttribute("role")).not.toBe("radio");
    });

    it("calls createLine once for the new project, and the review row and ready card name the line (GRPH-1749)", async () => {
      const user = userEvent.setup();
      renderWizard();
      await toRepo(user);
      await user.click(next());
      await user.click(screen.getByRole("radio", { name: /Create the maintenance line/ }));
      await user.click(next());
      await user.click(next());
      const row = screen.getByText("CORE-L1 · Maintenance");
      expect(row).toHaveClass("font-mono");
      expect(row).not.toHaveClass("text-faint");
      await user.click(screen.getByRole("button", { name: "Edit maintenance line" }));
      expect(screen.getByRole("heading", { name: "Add a maintenance line?" })).toBeInTheDocument();
      await user.click(next());
      await user.click(next());
      await user.click(screen.getByRole("button", { name: /create project/i }));
      await screen.findByText("Core Platform is ready.");
      expect(api.createLine).toHaveBeenCalledTimes(1);
      expect(api.createLine).toHaveBeenCalledWith("core");
      expect(screen.getByText(/· line CORE-L1/)).toBeInTheDocument();
    });

    it("makes no line call on Not now, and mutes that review row (GRPH-1749)", async () => {
      const user = userEvent.setup();
      renderWizard();
      await toRepo(user);
      await user.click(next());
      await user.click(next());
      await user.click(next());
      const row = screen.getByText("Not now");
      expect(row).toHaveClass("text-faint");
      expect(row).not.toHaveClass("font-mono");
      await user.click(screen.getByRole("button", { name: /create project/i }));
      await screen.findByText("Core Platform is ready.");
      expect(api.createLine).not.toHaveBeenCalled();
      expect(screen.queryByText(/line CORE-L1/)).not.toBeInTheDocument();
    });

    it("a failed createLine leaves the project created and does not draw NOT CREATED (GRPH-1749)", async () => {
      const user = userEvent.setup();
      api.createLine.mockRejectedValueOnce(new Error("boom"));
      renderWizard();
      await toRepo(user);
      await user.click(next());
      await user.click(screen.getByRole("radio", { name: /Create the maintenance line/ }));
      await user.click(next());
      await user.click(next());
      await user.click(screen.getByRole("button", { name: /create project/i }));
      expect(await screen.findByText(/the maintenance line wasn’t/)).toBeInTheDocument();
      expect(screen.getByText(/tracker’s Maintenance filter/)).toBeInTheDocument();
      expect(screen.queryByText("Core Platform is ready.")).not.toBeInTheDocument();
      expect(caption()).toBe("REVIEW · CORE");
      expect(screen.getByTestId("pw-hero").querySelector('path[stroke-dasharray="4 6"]')).toBeNull();
      expect(api.createProject).toHaveBeenCalledTimes(1);
      await user.click(screen.getByRole("button", { name: /create project/i }));
      await screen.findByText("Core Platform is ready.");
      expect(api.createProject).toHaveBeenCalledTimes(1);
      expect(api.createLine).toHaveBeenCalledTimes(2);
    });

    it("does not create the line twice when a later step fails after it exists (GRPH-1749)", async () => {
      const user = userEvent.setup();
      confirm.mockRejectedValueOnce(new Error("boom"));
      renderWizard();
      await toRepo(user);
      await user.click(next());
      await user.click(screen.getByRole("radio", { name: /Create the maintenance line/ }));
      await user.click(next());
      await user.click(next());
      await user.click(screen.getByRole("button", { name: /create project/i }));
      expect(await screen.findByText(/a follow-up step failed/)).toBeInTheDocument();
      expect(api.createLine).toHaveBeenCalledTimes(1);
      await user.click(screen.getByRole("button", { name: /create project/i }));
      await screen.findByText("Core Platform is ready.");
      expect(api.createLine).toHaveBeenCalledTimes(1);
      expect(screen.getByText(/· line CORE-L1/)).toBeInTheDocument();
    });

    it("draws the maintenance graphic still under prefers-reduced-motion (GRPH-1749)", async () => {
      mockReducedMotion(true);
      const user = userEvent.setup();
      renderWizard();
      await toRepo(user);
      await user.click(next());
      expect(animated()).toEqual([]);
      expect(screen.getByTestId("pw-body").style.animation).toBe("");
      expect(screen.getByTestId("pw-hero").querySelector('path[stroke-dasharray="4 6"]')).not.toBeNull();
      await user.click(screen.getByRole("radio", { name: /Create the maintenance line/ }));
      expect(animated()).toEqual([]);
      expect(screen.getByTestId("pw-line-how").style.animation).toBe("");
      expect(caption()).toBe("LINE · CORE-L1 · MAINTENANCE");
    });

    it("shows Checking… while the tag availability call is in flight", async () => {
      api.tagCheck.mockReturnValue(new Promise(() => {}));
      renderWizard();
      vi.useFakeTimers({ toFake: ["setTimeout", "clearTimeout", "setInterval", "clearInterval", "Date"] });
      try {
        fireEvent.change(screen.getByLabelText("Project name"), { target: { value: "Core Platform" } });
        await act(async () => {
          await vi.advanceTimersByTimeAsync(TAG_DEBOUNCE_MS);
        });
        expect(screen.getByText("Checking…")).toBeInTheDocument();
        expect(screen.getByLabelText("Tag")).toHaveClass("w-[120px]");
        expect(next()).toBeDisabled();
      } finally {
        vi.useRealTimers();
      }
    });
  });
});

describe("accent engine (GRPH-1750)", () => {
  const painted = (el: HTMLElement) => el.style.getPropertyValue("--tile-fill");
  const checkedIn = (name: string) =>
    within(screen.getByRole("radiogroup", { name }))
      .getAllByRole("radio")
      .find((r) => r.getAttribute("aria-checked") === "true");

  it("shows one colour row in solid and reveals an independent TO row in ombre", async () => {
    const user = userEvent.setup();
    renderWizard();
    expect(screen.queryByRole("radiogroup", { name: "Ombre, second colour" })).not.toBeInTheDocument();
    expect(screen.queryByText("FROM")).not.toBeInTheDocument();
    expect(within(screen.getByRole("radiogroup", { name: "Accent colour" })).getAllByRole("radio")).toHaveLength(12);

    await user.click(screen.getByRole("radio", { name: "OMBRE" }));
    const from = screen.getByRole("radiogroup", { name: "Ombre, first colour" });
    const to = screen.getByRole("radiogroup", { name: "Ombre, second colour" });
    expect(screen.getByText("FROM")).toBeInTheDocument();
    expect(screen.getByText("TO")).toBeInTheDocument();
    expect(within(from).getAllByRole("radio")).toHaveLength(12);
    expect(within(to).getAllByRole("radio")).toHaveLength(12);
    expect(checkedIn("Ombre, first colour")).toHaveAccessibleName("Lime");
    expect(checkedIn("Ombre, second colour")).toHaveAccessibleName("Sky");

    await user.click(within(from).getByRole("radio", { name: "Teal" }));
    expect(checkedIn("Ombre, first colour")).toHaveAccessibleName("Teal");
    expect(checkedIn("Ombre, second colour")).toHaveAccessibleName("Sky");
  });

  it("disables the TO swatch that matches FROM, skips it with the arrows, and moves TO by four when they would match", async () => {
    const user = userEvent.setup();
    renderWizard();
    await user.click(screen.getByRole("radio", { name: "OMBRE" }));
    const from = screen.getByRole("radiogroup", { name: "Ombre, first colour" });
    const to = screen.getByRole("radiogroup", { name: "Ombre, second colour" });
    const same = within(to).getByRole("radio", { name: "Lime, same as the first colour" });
    expect(same).toBeDisabled();
    expect(same).toHaveStyle({ opacity: "0.25" });
    expect(same).toHaveAttribute("aria-checked", "false");

    within(to).getByRole("radio", { name: "Sky" }).focus();
    await user.keyboard("{End}");
    expect(checkedIn("Ombre, second colour")).toHaveAccessibleName("Yellow");
    expect(checkedIn("Ombre, second colour")).toHaveFocus();
    await user.keyboard("{ArrowRight}");
    expect(checkedIn("Ombre, second colour")).toHaveAccessibleName("Mint");
    expect(checkedIn("Ombre, second colour")).toHaveFocus();
    await user.keyboard("{ArrowLeft}");
    expect(checkedIn("Ombre, second colour")).toHaveAccessibleName("Yellow");
    await user.keyboard("{Home}");
    expect(checkedIn("Ombre, second colour")).toHaveAccessibleName("Mint");

    await user.click(within(to).getByRole("radio", { name: "Indigo" }));
    await user.click(within(from).getByRole("radio", { name: "Indigo" }));
    expect(checkedIn("Ombre, first colour")).toHaveAccessibleName("Indigo");
    expect(checkedIn("Ombre, second colour")).toHaveAccessibleName("Rose");
    expect(within(to).getByRole("radio", { name: "Indigo, same as the first colour" })).toBeDisabled();
  });

  it("moves TO by four when switching to ombre while the two colours match", async () => {
    const user = userEvent.setup();
    renderWizard();
    await user.click(screen.getByRole("radio", { name: "Sky" }));
    await user.click(screen.getByRole("radio", { name: "OMBRE" }));
    expect(checkedIn("Ombre, first colour")).toHaveAccessibleName("Sky");
    expect(checkedIn("Ombre, second colour")).toHaveAccessibleName("Pink");
    expect(within(screen.getByRole("radiogroup", { name: "Ombre, second colour" })).getByRole("radio", { name: "Sky, same as the first colour" })).toBeDisabled();
  });

  it("offers the eight patterns as one tab stop, and keeps the pattern name out of the group label", async () => {
    const user = userEvent.setup();
    renderWizard();
    const patterns = screen.getByRole("radiogroup", { name: "Pattern" });
    expect(patterns).toHaveAccessibleName("Pattern");
    expect(within(patterns).getAllByRole("radio").map((r) => r.getAttribute("aria-label"))).toEqual([
      "None",
      "Speckled",
      "Cross hatch",
      "Chevron",
      "Herringbone",
      "Diagonal stripes",
      "Paisley",
      "Bubbles",
    ]);
    expect(screen.getByTestId("pw-pattern-name")).toHaveTextContent("None");
    const stops = within(patterns).getAllByRole("radio").filter((r) => r.tabIndex === 0);
    expect(stops).toEqual([within(patterns).getByRole("radio", { name: "None" })]);

    within(patterns).getByRole("radio", { name: "None" }).focus();
    await user.keyboard("{ArrowRight}");
    expect(checkedIn("Pattern")).toHaveAccessibleName("Speckled");
    expect(checkedIn("Pattern")).toHaveFocus();
    expect(patterns).toHaveAccessibleName("Pattern");
    expect(screen.getByTestId("pw-pattern-name")).toHaveTextContent("Speckled");
    await user.keyboard("{End}");
    expect(checkedIn("Pattern")).toHaveAccessibleName("Bubbles");
    await user.keyboard("{Home}");
    expect(checkedIn("Pattern")).toHaveAccessibleName("None");

    const selected = within(patterns).getByRole("radio", { name: "None" });
    const other = within(patterns).getByRole("radio", { name: "Paisley" });
    expect(selected.style.outline).toBe("");
    expect(other.style.outline).toBe("");
    expect(selected).toHaveStyle({
      boxShadow: "0 0 0 2px var(--color-surface-3), 0 0 0 4px var(--color-fg)",
      outlineOffset: "5px",
    });
    expect(other.style.boxShadow).toBe("");

    const mode = screen.getByRole("radiogroup", { name: "Accent style" });
    within(mode).getByRole("radio", { name: "SOLID" }).focus();
    await user.keyboard("{ArrowRight}");
    expect(checkedIn("Accent style")).toHaveAccessibleName("OMBRE");
    expect(checkedIn("Accent style")).toHaveFocus();
    await user.keyboard("{End}");
    expect(checkedIn("Accent style")).toHaveAccessibleName("OMBRE");
    await user.keyboard("{Home}");
    expect(checkedIn("Accent style")).toHaveAccessibleName("SOLID");
    expect(screen.getAllByRole("radiogroup")).toHaveLength(3);
    await user.keyboard("{ArrowDown}");
    expect(screen.getAllByRole("radiogroup")).toHaveLength(4);
    for (const group of screen.getAllByRole("radiogroup")) {
      expect(within(group).getAllByRole("radio").filter((r) => r.tabIndex === 0)).toHaveLength(1);
    }
  });

  it("paints the tile, the caption square, the review swatch and the ready card with one fill, and posts accent2 and pattern", async () => {
    const user = userEvent.setup();
    api.createProject.mockImplementation(async (body: { name: string; tag?: string; accent: string; accent2: string | null; pattern: string }) => ({
      id: "core",
      name: body.name,
      tag: body.tag,
      accent: body.accent,
      accent2: body.accent2,
      pattern: body.pattern,
    }));
    renderWizard();
    await user.click(screen.getByRole("radio", { name: "OMBRE" }));
    await user.click(within(screen.getByRole("radiogroup", { name: "Ombre, second colour" })).getByRole("radio", { name: "Indigo" }));
    await user.click(within(screen.getByRole("radiogroup", { name: "Pattern" })).getByRole("radio", { name: "Paisley" }));
    const expected = projectFill({ accent: "#c6f24e", accent2: "#9d9cff", pattern: "paisley" });
    expect(painted(screen.getByTestId("project-tile"))).toBe(expected);
    expect(painted(screen.getByTestId("pw-accent-square"))).toBe(expected);
    expect(painted(within(screen.getByRole("radiogroup", { name: "Ombre, first colour" })).getByRole("radio", { name: "Lime" }))).toBe(expected);
    expect(painted(within(screen.getByRole("radiogroup", { name: "Ombre, second colour" })).getByRole("radio", { name: "Indigo" }))).toBe(expected);
    expect(painted(within(screen.getByRole("radiogroup", { name: "Pattern" })).getByRole("radio", { name: "Paisley" }))).toBe(expected);

    await toRepo(user);
    await user.click(next());
    await user.click(next());
    await user.click(next());
    expect(screen.getByText("Lime → Indigo ombre · paisley")).toBeInTheDocument();
    expect(painted(screen.getByTestId("pw-review-swatch"))).toBe(expected);
    expect(painted(screen.getByTestId("pw-accent-square"))).toBe(expected);

    await user.click(screen.getByRole("button", { name: /create project/i }));
    expect(await screen.findByText("Core Platform is ready.")).toBeInTheDocument();
    const created = await api.createProject.mock.results[0]?.value;
    expect(created).toMatchObject({ accent: "#c6f24e", accent2: "#9d9cff", pattern: "paisley" });
    expect(api.createProject).toHaveBeenCalledWith({
      name: "Core Platform",
      tag: "CORE",
      accent: "#c6f24e",
      accent2: "#9d9cff",
      pattern: "paisley",
    });
    expect(painted(screen.getByTestId("project-tile"))).toBe(expected);
    expect(painted(screen.getByTestId("pw-accent-square"))).toBe(expected);
  });

  it("reads Lime · no pattern on the review row when the accent is solid", async () => {
    const user = userEvent.setup();
    renderWizard();
    await toRepo(user);
    await user.click(next());
    await user.click(next());
    await user.click(next());
    expect(screen.getByText("Lime · no pattern")).toBeInTheDocument();
    expect(screen.getByText("Lime · no pattern")).not.toHaveClass("font-mono");
  });
});

const HERE = dirname(fileURLToPath(import.meta.url));

/** The twelve hues from the Project Wizard design, in hue order. */
const PALETTE: [string, string][] = [
  ["#c6f24e", "Lime"],
  ["#a6ebc4", "Mint"],
  ["#4fd1c5", "Teal"],
  ["#67d4f0", "Cyan"],
  ["#a3cfff", "Sky"],
  ["#9d9cff", "Indigo"],
  ["#a78bfa", "Violet"],
  ["#d58cf0", "Orchid"],
  ["#f47fb4", "Pink"],
  ["#ffb3c1", "Rose"],
  ["#e6d2a8", "Sand"],
  ["#efd76a", "Yellow"],
];

function themeHexTokens(css: string): Record<string, string> {
  const tokens: Record<string, string> = {};
  for (const match of css.matchAll(/--(color-[a-z0-9_-]+):\s*(#[0-9a-fA-F]{6})\s*;/g)) {
    tokens[match[1]!] = match[2]!.toLowerCase();
  }
  return tokens;
}

describe("project accents stay off the status colours (GRPH-1747)", () => {
  it("holds the twelve named hues, and only brand lime overlaps a status token", () => {
    expect(PROJECT_ACCENT_SWATCHES.map((swatch) => [swatch.hex, swatch.name])).toEqual(PALETTE);
    const accents = [...PROJECT_ACCENTS];
    expect(accents).toEqual(PALETTE.map(([hex]) => hex));

    const tokens = themeHexTokens(readFileSync(join(HERE, "../../index.css"), "utf8"));
    for (const key of [
      "color-st-backlog",
      "color-st-next",
      "color-st-in_progress",
      "color-st-review",
      "color-st-done",
      "color-st-blocked",
      "color-op-accent",
      "color-accent-hi",
    ]) {
      expect(tokens[key], key).toMatch(/^#[0-9a-f]{6}$/);
    }
    const palette = new Set<string>(accents);
    // Lime is the brand accent, so it is also --color-st-in_progress. The design
    // keeps that one and excludes the other five status tokens.
    const overlaps = Object.entries(tokens).filter(
      ([key, value]) => key.startsWith("color-st-") && palette.has(value),
    );
    expect(overlaps).toEqual([["color-st-in_progress", "#c6f24e"]]);
    for (const key of ["color-op-accent", "color-accent-hi"]) {
      expect(palette.has(tokens[key]!)).toBe(false);
    }
    expect(palette.has("#ff8a3d")).toBe(false);

    const source = readFileSync(join(HERE, "projectAccents.ts"), "utf8");
    for (const name of [
      "operator orange",
      "--color-st-done",
      "--color-st-next",
      "--color-st-review",
      "--color-st-blocked",
      "--color-st-backlog",
    ]) {
      expect(source).toContain(name);
    }
  });

  it("names each swatch and lets the row wrap", () => {
    renderWizard();
    const group = screen.getByRole("radiogroup", { name: "Accent colour" });
    expect(group.className).toContain("flex-wrap");
    const radios = within(group).getAllByRole("radio");
    expect(radios).toHaveLength(PALETTE.length);
    radios.forEach((radio, i) => {
      const [hex, name] = PALETTE[i]!;
      // The swatch paints `--tile-fill`. A plain colour is that variable; a pattern
      // is a layer jsdom will not keep on `background-color`.
      expect(radio.style.getPropertyValue("--tile-fill")).toBe(hex);
      expect(radio.style.background).toContain("var(--tile-fill)");
      expect(radio).toHaveAccessibleName(name);
      expect(radio).toHaveAttribute("title", name);
    });
  });

  it("seeds the demo projects with palette colours only", () => {
    const seed = readFileSync(join(HERE, "../../../../backend/app/seed.py"), "utf8");
    const block = seed.slice(seed.indexOf("PROJECTS = ["), seed.indexOf("\nUSERS"));
    const accents = [...block.matchAll(/accent="(#[0-9a-fA-F]{6})"/g)].map((match) => match[1]!.toLowerCase());
    expect(accents).toEqual(["#c6f24e", "#a3cfff", "#e6d2a8"]);
    for (const accent of accents) expect(PROJECT_ACCENTS).toContain(accent);
  });
});
