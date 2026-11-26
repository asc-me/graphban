/** Settings → Project → Archive (GRPH-1955): archive the active project with a reason, and list
 * archived projects to unarchive. */
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { beforeEach, expect, it, vi } from "vitest";

import { ProjectProvider } from "@/features/ProjectContext";
import { ProjectArchivePanel } from "@/features/settings/ProjectArchivePanel";
import type { Project } from "@/lib/types";

const base: Project = {
  id: "core", tag: "CORE", name: "core", accent: "#c6f24e",
  visibility: "private", description: "", share_global_memory: false, auto_extract: true,
  mcp_enabled: true, embed_model: "", memory_auto_reject: true, memory_write_mode: "review",
  memory_llm_judge: false, agent_adjudication: false, allow_self_review: false,
  credential_id: null, model_override: "",
};
const walk: Project = {
  ...base, id: "p91-walk", tag: "PW", name: "p91-walk",
  archived_at: "2026-11-20T10:00:00Z", archived_reason: "walk finished",
};

const archiveProject = vi.fn(async (_id: string, _reason: string) => base);
const unarchiveProject = vi.fn(async (_id: string) => walk);

vi.mock("@/lib/api", () => ({
  setActiveProjectId: vi.fn(),
  api: {
    projects: vi.fn(async () => [base]),
    projectsIncludingArchived: vi.fn(async () => [base, walk]),
    archiveProject: (id: string, reason: string) => archiveProject(id, reason),
    unarchiveProject: (id: string) => unarchiveProject(id),
  },
}));

function renderPanel() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  render(
    <QueryClientProvider client={qc}>
      <MemoryRouter>
        <ProjectProvider>
          <ProjectArchivePanel />
        </ProjectProvider>
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

beforeEach(() => {
  archiveProject.mockClear();
  unarchiveProject.mockClear();
});

it("archives the active project only once a reason is given", async () => {
  renderPanel();
  const button = await screen.findByRole("button", { name: "Archive CORE" });
  expect(button).toBeDisabled();
  const user = userEvent.setup();
  await user.type(screen.getByLabelText("Reason for archiving"), "  test project done ");
  expect(button).toBeEnabled();
  await user.click(button);
  expect(archiveProject).toHaveBeenCalledWith("core", "test project done");
});

it("lists archived projects with their reason and unarchives one", async () => {
  renderPanel();
  const button = await screen.findByRole("button", { name: "Unarchive PW" });
  expect(screen.getByText(/walk finished/)).toBeInTheDocument();
  expect(screen.queryByRole("button", { name: "Unarchive CORE" })).toBeNull();
  await userEvent.setup().click(button);
  expect(unarchiveProject).toHaveBeenCalledWith("p91-walk");
});
