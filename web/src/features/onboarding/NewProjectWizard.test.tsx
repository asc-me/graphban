import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { NewProjectWizard } from "@/features/onboarding/NewProjectWizard";

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
    expect(screen.getByText("$ gban setup")).toBeInTheDocument();
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
});
