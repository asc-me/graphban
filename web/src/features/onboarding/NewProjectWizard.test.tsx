import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { NewProjectWizard } from "@/features/onboarding/NewProjectWizard";

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

async function toRepo(user: ReturnType<typeof userEvent.setup>) {
  await user.type(screen.getByLabelText("Project name"), "Core Platform");
  await waitFor(() => expect(next()).toBeEnabled());
  await user.click(next());
}

beforeEach(() => {
  vi.clearAllMocks();
  mockReducedMotion(false);
  api.tagSuggestion.mockResolvedValue({ tag: "CORE" });
  api.tagCheck.mockResolvedValue({ tag: "CORE", available: true, reason: "" });
  api.createProject.mockResolvedValue({ id: "core", name: "Core Platform", tag: "CORE" });
  api.updateProject.mockResolvedValue({});
  confirm.mockResolvedValue({});
});

describe("New project wizard", () => {
  it("shows the four-step rail and blocks Next until the identity is valid", async () => {
    const user = userEvent.setup();
    renderWizard();
    expect(screen.getByRole("list", { name: "Steps" })).toBeInTheDocument();
    for (const l of ["Name & tag", "Repository", "Model", "Review"]) expect(screen.getAllByText(l).length).toBeGreaterThan(0);
    expect(next()).toBeDisabled();
    await user.type(screen.getByLabelText("Project name"), "Core Platform");
    await waitFor(() => expect(next()).toBeEnabled());
  });

  it("blocks Next when the tag is taken", async () => {
    const user = userEvent.setup();
    api.tagCheck.mockResolvedValue({ tag: "CORE", available: false, reason: "taken" });
    renderWizard();
    await user.type(screen.getByLabelText("Project name"), "Core Platform");
    await waitFor(() => expect(screen.getByText("taken")).toBeInTheDocument());
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
    const user = userEvent.setup();
    api.tagCheck.mockRejectedValue(new Error("network"));
    renderWizard();
    await user.type(screen.getByLabelText("Project name"), "Core Platform");
    await waitFor(() => expect(screen.getByText(/Couldn’t check whether this tag is available/)).toBeInTheDocument());
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
    expect(screen.getByRole("heading", { name: "Pick a model" })).toBeInTheDocument();
  });

  it("lists five review rows, Edit goes back, and Create saves name, tag, accent, repo and model", async () => {
    const user = userEvent.setup();
    renderWizard();
    await toRepo(user);
    await user.type(screen.getByLabelText("Git remote"), "git@github.com:acme/core.git");
    await user.click(next());
    await user.click(next());
    for (const k of ["NAME", "TAG", "ACCENT", "REPOSITORY", "MODEL"]) expect(screen.getByText(k)).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Edit repository" }));
    expect(screen.getByRole("heading", { name: "Point it at a repository" })).toBeInTheDocument();
    await user.click(next());
    await user.click(next());
    await user.click(screen.getByRole("button", { name: /create project/i }));
    await waitFor(() =>
      expect(api.createProject).toHaveBeenCalledWith({ name: "Core Platform", tag: "CORE", accent: "#c6f24e" }),
    );
    expect(api.updateProject).toHaveBeenCalledWith("core", { repo_url: "git@github.com:acme/core.git" });
    expect(confirm).toHaveBeenCalledWith({ projectId: "core", body: { decision: "offline" } });
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

  it("locks the identity step after a partial failure so edits cannot be dropped", async () => {
    const user = userEvent.setup();
    api.updateProject.mockRejectedValueOnce(new Error("boom"));
    renderWizard();
    await toRepo(user);
    await user.type(screen.getByLabelText("Git remote"), "git@github.com:acme/core.git");
    await user.click(next());
    await user.click(next());
    await user.click(screen.getByRole("button", { name: /create project/i }));
    await screen.findByText(/was created, but a follow-up step failed/);
    await user.click(screen.getByRole("button", { name: "Back" }));
    await user.click(screen.getByRole("button", { name: "Back" }));
    await user.click(screen.getByRole("button", { name: "Back" }));
    expect(screen.getByLabelText("Project name")).toBeDisabled();
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
      await user.click(screen.getByRole("radio", { name: "accent #a78bfa" }));
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
      await user.click(screen.getByRole("button", { name: /create project/i }));
      await screen.findByText("Core Platform is ready.");
      expect(caption()).toBe("CREATED · CORE");
      const kids = [...footer.children];
      expect(kids.map((k) => k.textContent)).toEqual(["", "Create another", "Open project"]);
    });

    it("shows Checking… while the tag availability call is in flight", async () => {
      const user = userEvent.setup();
      api.tagCheck.mockReturnValue(new Promise(() => {}));
      renderWizard();
      await user.type(screen.getByLabelText("Project name"), "Core Platform");
      expect(await screen.findByText("Checking…")).toBeInTheDocument();
      expect(screen.getByLabelText("Tag")).toHaveClass("w-[120px]");
    });
  });
});
