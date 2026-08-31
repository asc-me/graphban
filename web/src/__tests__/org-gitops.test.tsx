import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import type { ReactNode } from "react";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { LeftNav } from "@/components/shell/LeftNav";
import { docFor } from "@/features/docs/content";
import { OrgGitops } from "@/features/orgadmin/OrgGitops";
import { ProjectProvider } from "@/features/ProjectContext";
import { adminPath } from "@/lib/routes";
import type { GitopsView } from "@/lib/types";

/**
 * GRPH-P31 PR 3 — hosted org editor.
 *
 * Overlay rows come from org GET `projects`, not `useProjects()`. Binding the overlay
 * input to the resolved value would copy-down house `stage` onto the project.
 */

const unmeasured = { value: null, source: "unmeasured" as const };

const app = { id: "prj_app", name: "App", tag: "APP" };
const lib = { id: "prj_lib", name: "Lib", tag: "LIB" };

function fields(over: Partial<GitopsView["fields"]> = {}): GitopsView["fields"] {
  return {
    base_branch: unmeasured,
    no_push_to_base: unmeasured,
    branch_name_pattern: unmeasured,
    pr_title_pattern: unmeasured,
    reviewer_bar: unmeasured,
    ...over,
  };
}

function view(over: Partial<GitopsView> = {}): GitopsView {
  return {
    project_id: null,
    org_id: "org_1",
    fields: fields(),
    version_from: unmeasured,
    control: { state: "local", writable: true, message: "" },
    was: null,
    projects: [app, lib],
    ...over,
  };
}

const houseStage = view({
  fields: fields({ base_branch: { value: "stage", source: "org" } }),
});

const appInherit = view({
  project_id: "prj_app",
  fields: fields({ base_branch: { value: "stage", source: "org" } }),
  projects: [],
});

const libOverlay = view({
  project_id: "prj_lib",
  fields: fields({ base_branch: { value: "main", source: "project" } }),
  projects: [],
});

const project = {
  id: "prj_app",
  tag: "APP",
  name: "App",
  accent: "#c6f24e",
  visibility: "private",
  description: "",
  share_global_memory: false,
  auto_extract: true,
  mcp_enabled: true,
  embed_model: "",
  credential_id: null,
  model_override: "",
  memory_auto_reject: true,
  memory_write_mode: "review",
  memory_llm_judge: false,
  agent_adjudication: false,
  allow_self_review: false,
};

const spies = vi.hoisted(() => ({
  orgGitops: vi.fn(),
  updateOrgGitops: vi.fn(),
  gitops: vi.fn(),
  updateGitops: vi.fn(),
  projects: vi.fn(),
  orgs: vi.fn(),
}));

vi.mock("@/lib/api", () => ({
  api: {
    orgGitops: spies.orgGitops,
    updateOrgGitops: spies.updateOrgGitops,
    gitops: spies.gitops,
    updateGitops: spies.updateGitops,
    projects: spies.projects,
    orgs: spies.orgs,
    counts: vi.fn(async () => ({ items: 0, items_in_progress: 0, requests: 0, review: 0 })),
    adminWhoami: vi.fn(async () => ({ is_platform_admin: false })),
    syncStatus: vi.fn(async () => ({
      linked: false, source: "", cloud_url: "", org: "", credential_set: false, linked_at: null, projects: [],
    })),
  },
}));

function renderPage() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={qc}>
      <MemoryRouter initialEntries={[adminPath("gitops")]}>
        <OrgGitops />
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

function renderNav(ui: ReactNode, path = "/p/APP/tracker") {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={qc}>
      <MemoryRouter initialEntries={[path]}>
        <ProjectProvider>
          <Routes>
            <Route path="*" element={ui} />
          </Routes>
        </ProjectProvider>
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

describe("Org gitops editor", () => {
  beforeEach(() => {
    for (const s of Object.values(spies)) s.mockReset();
    spies.orgs.mockResolvedValue([{ id: "org_1", name: "Acme", plan: "team", role: "owner" }]);
    spies.orgGitops.mockResolvedValue(houseStage);
    spies.updateOrgGitops.mockImplementation(async (_id: string, body: unknown) => ({
      ...houseStage,
      ...((body as object) ?? {}),
    }));
    spies.gitops.mockImplementation(async (id: string) => {
      if (id === "prj_lib") return libOverlay;
      if (id === "prj_app") return appInherit;
      throw new Error(`unexpected gitops GET ${id}`);
    });
    spies.updateGitops.mockResolvedValue(appInherit);
    // B (Lib) is absent from the readable project list — the sabotage for roster source.
    spies.projects.mockResolvedValue([project]);
  });

  it("shows house stage and an inheriting overlay as empty, not stage in the input", async () => {
    renderPage();
    const house = await screen.findByLabelText("House base branch");
    expect(house).toHaveValue("stage");
    expect(screen.queryByRole("checkbox")).not.toBeInTheDocument();

    const appInput = await screen.findByLabelText("APP overlay base branch");
    expect(appInput).toHaveValue("");
    expect(screen.getByText("inherits stage")).toBeInTheDocument();
    expect(appInput).not.toHaveValue("stage");

    const libInput = await screen.findByLabelText("LIB overlay base branch");
    expect(libInput).toHaveValue("main");
  });

  it("saves an untouched overlay as {} and does not copy-down stage", async () => {
    const user = userEvent.setup();
    renderPage();
    await screen.findByLabelText("APP overlay base branch");
    await user.click(screen.getByRole("button", { name: "Save APP overlay" }));
    expect(spies.updateGitops).toHaveBeenCalledWith("prj_app", {});
    expect(spies.updateGitops).not.toHaveBeenCalledWith(
      "prj_app",
      expect.objectContaining({ base_branch: "stage" }),
    );
  });

  it("clears a set overlay with × as JSON null, not omit", async () => {
    const appSet = view({
      project_id: "prj_app",
      fields: fields({ base_branch: { value: "main", source: "project" } }),
      projects: [],
    });
    spies.gitops.mockImplementation(async (id: string) => {
      if (id === "prj_lib") return libOverlay;
      if (id === "prj_app") return appSet;
      throw new Error(`unexpected gitops GET ${id}`);
    });
    const user = userEvent.setup();
    renderPage();
    const input = await screen.findByLabelText("APP overlay base branch");
    expect(input).toHaveValue("main");
    await user.click(screen.getByRole("button", { name: "Clear APP overlay base branch" }));
    expect(input).toHaveValue("");
    await user.click(screen.getByRole("button", { name: "Save APP overlay" }));
    expect(spies.updateGitops).toHaveBeenCalledTimes(1);
    expect(spies.updateGitops).toHaveBeenCalledWith("prj_app", { base_branch: null });
  });

  it("does not send null when a set overlay is keyboard-emptied — only × clears", async () => {
    const user = userEvent.setup();
    renderPage();
    const input = await screen.findByLabelText("LIB overlay base branch");
    expect(input).toHaveValue("main");
    await user.clear(input);
    expect(input).toHaveValue("main");
    await user.click(screen.getByRole("button", { name: "Save LIB overlay" }));
    expect(spies.updateGitops).toHaveBeenCalledWith("prj_lib", {});
    expect(spies.updateGitops).not.toHaveBeenCalledWith(
      "prj_lib",
      expect.objectContaining({ base_branch: null }),
    );
  });

  it("still renders a project the caller cannot read, because the roster is org GET", async () => {
    renderPage();
    expect(await screen.findByText("Lib")).toBeInTheDocument();
    expect(await screen.findByLabelText("LIB overlay base branch")).toBeInTheDocument();
    expect(spies.projects).not.toHaveBeenCalled();
  });

  it("keeps the house form when one overlay GET fails", async () => {
    spies.gitops.mockImplementation(async (id: string) => {
      if (id === "prj_lib") throw new Error("500");
      return appInherit;
    });
    renderPage();
    expect(await screen.findByLabelText("House base branch")).toHaveValue("stage");
    expect(await screen.findByText("could not load overlay")).toBeInTheDocument();
    expect(screen.getByText("Lib")).toBeInTheDocument();
    expect(screen.queryByLabelText("LIB overlay base branch")).not.toBeInTheDocument();
  });

  it("treats an empty org as unmeasured, not as a set process", async () => {
    spies.orgGitops.mockResolvedValue(view({ projects: [] }));
    renderPage();
    const house = await screen.findByLabelText("House base branch");
    expect(house).toHaveValue("");
    expect(house).toHaveAttribute("placeholder", "Unmeasured — not main");
    expect(screen.queryByText("Controlled by the org admin.")).not.toBeInTheDocument();
    expect(screen.queryByText(/git process is set/i)).not.toBeInTheDocument();
    expect(screen.getAllByText(/unmeasured/i).length).toBeGreaterThan(0);
  });

  it("disables save when writable is false (member GET)", async () => {
    spies.orgGitops.mockResolvedValue({
      ...houseStage,
      control: { state: "local", writable: false, message: "" },
    });
    spies.gitops.mockImplementation(async (id: string) => ({
      ...(id === "prj_lib" ? libOverlay : appInherit),
      control: { state: "local", writable: false, message: "" },
    }));
    spies.updateOrgGitops.mockRejectedValue(
      new Error(JSON.stringify({ detail: "Not authorized as org admin" })),
    );
    renderPage();
    const save = await screen.findByRole("button", { name: "Save house process" });
    expect(save).toBeDisabled();
    await userEvent.click(save);
    expect(spies.updateOrgGitops).not.toHaveBeenCalled();
  });

  it("does not feed the page from useProjects", () => {
    const sources = import.meta.glob("../features/orgadmin/OrgGitops.tsx", {
      query: "?raw",
      import: "default",
      eager: true,
    }) as Record<string, string>;
    const src = Object.values(sources)[0] ?? "";
    expect(src).not.toMatch(/\buseProjects\b/);
    expect(src).toContain('source === "project"');
  });

  it("matches docs via adminPath, not a literal org prefix", () => {
    expect(docFor(adminPath("gitops")).title).toBe("Org gitops");
    expect(docFor(adminPath("gitops")).badge).toBe("GITOPS");
  });
});

describe("Gitops nav is admin-only", () => {
  beforeEach(() => {
    spies.projects.mockResolvedValue([project]);
    spies.orgs.mockResolvedValue([{ id: "org_1", name: "Acme", plan: "team", role: "owner" }]);
  });

  it("shows Gitops for an admin inside the Admin group", async () => {
    renderNav(<LeftNav hosted />);
    expect(await screen.findByText("Gitops")).toBeInTheDocument();
    expect(screen.getByText("Users & access")).toBeInTheDocument();
  });

  it("hides Gitops and the Admin group from a member", async () => {
    spies.orgs.mockResolvedValue([{ id: "org_1", name: "Acme", plan: "team", role: "member" }]);
    renderNav(<LeftNav hosted />);
    expect(await screen.findByText("Tracker")).toBeInTheDocument();
    expect(screen.queryByText("Gitops")).not.toBeInTheDocument();
    expect(screen.queryByText("Users & access")).not.toBeInTheDocument();
    expect(screen.getByText("no admin group")).toBeInTheDocument();
  });
});
