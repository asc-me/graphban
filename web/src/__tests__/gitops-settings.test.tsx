import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { docFor } from "@/features/docs/content";
import { ProjectProvider } from "@/features/ProjectContext";
import {
  GITOPS_BASE_CHIPS,
  GITOPS_NAMING_TOKENS,
  UNLINK_WARNING,
  UNMEASURED_PLACEHOLDER,
  UNTIL_LINKED,
} from "@/features/settings/GitopsPanel";
import { SettingsView } from "@/features/settings/SettingsView";
import { settingsPath } from "@/lib/routes";
import type { GitopsField, GitopsView, GitopsWas, Project } from "@/lib/types";

const projA: Project = {
  id: "prj_a",
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
const projB: Project = { ...projA, id: "prj_b", tag: "LIB", name: "Lib", accent: "#7ca2ff" };

const unmeasured: GitopsField = { value: null, source: "unmeasured" };

function emptyWas(): GitopsWas {
  return {
    base_branch: null,
    no_push_to_base: null,
    branch_name_pattern: null,
    pr_title_pattern: null,
    reviewer_bar: null,
  };
}

function view(
  over: Omit<Partial<GitopsView>, "fields" | "control"> & {
    fields?: Partial<GitopsView["fields"]>;
    control?: Partial<GitopsView["control"]>;
  } = {},
): GitopsView {
  const { fields, control, version_from, ...rest } = over;
  return {
    project_id: "prj_a",
    org_id: null,
    was: null,
    version_from: version_from ?? unmeasured,
    ...rest,
    fields: {
      base_branch: unmeasured,
      no_push_to_base: unmeasured,
      branch_name_pattern: unmeasured,
      pr_title_pattern: unmeasured,
      reviewer_bar: unmeasured,
      ...fields,
    },
    control: { state: "local", writable: true, message: "", ...control },
  };
}

/** Four-state table, pinned copy. */
const state1 = view({ project_id: "prj_a" });
const state2 = view({
  project_id: "prj_a",
  fields: { base_branch: { value: "test", source: "project" } },
});
const state3 = view({
  project_id: "prj_a",
  control: {
    state: "linked_unset",
    writable: false,
    message: "Linked; the org has not set a git process.",
  },
  was: { ...emptyWas(), base_branch: "test" },
});
const state4 = view({
  project_id: "prj_a",
  fields: { base_branch: { value: "stage", source: "org" } },
  control: {
    state: "linked_set",
    writable: false,
    message: "Controlled by the org admin.",
  },
  was: { ...emptyWas(), base_branch: "test" },
});
const unreachable = view({
  project_id: "prj_a",
  control: {
    state: "linked_unreachable",
    writable: false,
    message: "Linked; the org could not be reached. Git process is unmeasured — not the local values.",
  },
  was: { ...emptyWas(), base_branch: "test" },
});

const { gitopsSpy, updateSpy, projectsSpy } = vi.hoisted(() => ({
  gitopsSpy: vi.fn(async (_id: string) => state1),
  updateSpy: vi.fn(async (_id: string, _body: unknown) => state1),
  projectsSpy: vi.fn(async () => [projA, projB]),
}));

vi.mock("@/lib/api", () => ({
  api: {
    projects: projectsSpy,
    gitops: gitopsSpy,
    updateGitops: updateSpy,
    config: vi.fn(async () => ({ hosted_mode: false, signup_mode: "closed" })),
  },
}));

function renderPage(path = settingsPath("deployment/gitops"), tag = "APP") {
  localStorage.setItem("gb_last_project_tag", tag);
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return {
    qc,
    ...render(
      <QueryClientProvider client={qc}>
        <MemoryRouter initialEntries={[path]}>
          <ProjectProvider>
            <Routes>
              <Route path="/settings/*" element={<SettingsView />} />
            </Routes>
          </ProjectProvider>
        </MemoryRouter>
      </QueryClientProvider>,
    ),
  };
}

describe("Gitops Settings page", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    sessionStorage.removeItem("gb_gitops_prelink");
    localStorage.removeItem("gb_last_project_tag");
    projectsSpy.mockResolvedValue([projA, projB]);
    gitopsSpy.mockImplementation(async (id: string) =>
      id === "prj_b" ? view({ project_id: "prj_b" }) : state1,
    );
    updateSpy.mockResolvedValue(state1);
  });

  it("sits under This box as Gitops, not a renamed Deployment group", async () => {
    renderPage();
    expect(await screen.findByText("This box")).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Gitops" })).toHaveAttribute(
      "href",
      settingsPath("deployment/gitops"),
    );
    expect(screen.getByText("This project")).toBeInTheDocument();
    expect(screen.queryByText("Deployment")).not.toBeInTheDocument();
  });

  it("state 1: editable unmeasured, names the project, placeholder is not main as a value", async () => {
    renderPage();
    expect(await screen.findByText("App · APP")).toBeInTheDocument();
    expect(screen.getByText(UNTIL_LINKED)).toBeInTheDocument();
    const base = screen.getByLabelText("Base branch");
    expect(base).toBeEnabled();
    expect(base).toHaveValue("");
    expect(base).toHaveAttribute("placeholder", UNMEASURED_PLACEHOLDER);
    expect(screen.queryByDisplayValue("main")).not.toBeInTheDocument();
    expect(screen.queryByRole("status")).not.toBeInTheDocument();
    expect(screen.queryByText(/controlled by the org admin/i)).not.toBeInTheDocument();

    const push = screen.getByLabelText("Do not push to the base");
    expect(push.tagName).toBe("SELECT");
    expect(push).toHaveValue("");
    expect(screen.queryByRole("checkbox")).not.toBeInTheDocument();

    for (const chip of GITOPS_BASE_CHIPS) {
      expect(screen.getByRole("button", { name: chip })).toBeInTheDocument();
    }
    for (const token of GITOPS_NAMING_TOKENS) {
      expect(screen.getAllByRole("button", { name: `{${token}}` }).length).toBeGreaterThan(0);
    }
    expect(gitopsSpy).toHaveBeenCalledWith("prj_a");
  });

  it("state 2: shows local test and still names the active project", async () => {
    gitopsSpy.mockResolvedValue(state2);
    renderPage();
    expect(await screen.findByText("App · APP")).toBeInTheDocument();
    expect(screen.getByLabelText("Base branch")).toBeEnabled();
    expect(screen.getByLabelText("Base branch")).toHaveValue("test");
    expect(screen.queryByRole("status")).not.toBeInTheDocument();
    expect(screen.queryByText("was: test")).not.toBeInTheDocument();
  });

  it("state 3: greys unmeasured, pinned linked-unset banner, was is muted not the form", async () => {
    gitopsSpy.mockResolvedValue(state3);
    renderPage();
    const banner = await screen.findByRole("status");
    expect(banner).toHaveTextContent("Linked; the org has not set a git process.");
    expect(banner.textContent).not.toMatch(/controlled by the org admin/i);
    const base = screen.getByLabelText("Base branch");
    expect(base).toBeDisabled();
    expect(base).toHaveValue("");
    expect(screen.getByText("was: test")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Save gitops" })).toBeDisabled();
  });

  it("state 4: greys org stage, pinned linked-set banner, was is not the form value", async () => {
    gitopsSpy.mockResolvedValue(state4);
    renderPage();
    expect(await screen.findByRole("status")).toHaveTextContent("Controlled by the org admin.");
    const base = screen.getByLabelText("Base branch");
    expect(base).toBeDisabled();
    expect(base).toHaveValue("stage");
    expect(screen.queryByDisplayValue("test")).not.toBeInTheDocument();
    expect(screen.getByText("was: test")).toBeInTheDocument();
  });

  it("linked_unreachable: unmeasured form, not was.test, and not the linked_unset banner", async () => {
    gitopsSpy.mockResolvedValue(unreachable);
    renderPage();
    expect(await screen.findByRole("status")).toHaveTextContent(
      "Linked; the org could not be reached. Git process is unmeasured — not the local values.",
    );
    expect(screen.queryByText("Linked; the org has not set a git process.")).not.toBeInTheDocument();
    const base = screen.getByLabelText("Base branch");
    expect(base).toBeDisabled();
    expect(base).toHaveValue("");
    expect(screen.getByText("was: test")).toBeInTheDocument();
  });

  it("greys without inventing a banner: empty control.message is not a process", async () => {
    gitopsSpy.mockResolvedValue(
      view({
        control: { state: "linked_unset", writable: false, message: "" },
      }),
    );
    renderPage();
    expect(await screen.findByLabelText("Base branch")).toBeDisabled();
    expect(screen.queryByRole("status")).not.toBeInTheDocument();
    expect(screen.queryByText(/controlled by the org admin/i)).not.toBeInTheDocument();
    expect(screen.queryByText("Linked; the org has not set a git process.")).not.toBeInTheDocument();
  });

  it("two projects: B is unmeasured, not A's test, and names B", async () => {
    gitopsSpy.mockImplementation(async (id: string) => {
      if (id === "prj_a") return state2;
      return view({ project_id: "prj_b" });
    });
    renderPage(settingsPath("deployment/gitops"), "LIB");
    expect(await screen.findByText("Lib · LIB")).toBeInTheDocument();
    expect(screen.queryByText("App · APP")).not.toBeInTheDocument();
    expect(screen.getByLabelText("Base branch")).toHaveValue("");
    expect(screen.queryByDisplayValue("test")).not.toBeInTheDocument();
    expect(screen.queryByDisplayValue("main")).not.toBeInTheDocument();
    expect(gitopsSpy).toHaveBeenCalledWith("prj_b");
    expect(gitopsSpy).not.toHaveBeenCalledWith("prj_a");
  });

  it("requires an active project — no box-wide contract", async () => {
    projectsSpy.mockResolvedValue([]);
    renderPage();
    expect(await screen.findByText(/select a project/i)).toBeInTheDocument();
    expect(gitopsSpy).not.toHaveBeenCalled();
  });

  it("unlink warning: pre-link values, not the org's last contract; save is not blocked", async () => {
    gitopsSpy.mockResolvedValue(state4);
    const { qc } = renderPage();
    expect(await screen.findByRole("status")).toHaveTextContent("Controlled by the org admin.");
    gitopsSpy.mockResolvedValue(state2);
    await qc.invalidateQueries({ queryKey: ["gitops"] });
    expect(await screen.findByRole("alert")).toHaveTextContent(UNLINK_WARNING);
    expect(screen.getByLabelText("Base branch")).toBeEnabled();
    expect(screen.getByLabelText("Base branch")).toHaveValue("test");
    expect(screen.getByRole("button", { name: "Save gitops" })).toBeEnabled();
  });

  it("saves sparse: only the edited field, and × sends JSON null", async () => {
    gitopsSpy.mockResolvedValue(state2);
    updateSpy.mockResolvedValue(state2);
    const user = userEvent.setup();
    renderPage();
    const base = await screen.findByLabelText("Base branch");
    await user.click(screen.getByRole("button", { name: "stage" }));
    expect(base).toHaveValue("stage");
    await user.click(screen.getByRole("button", { name: /Save gitops|Saved/ }));
    await waitFor(() => expect(updateSpy).toHaveBeenCalledWith("prj_a", { base_branch: "stage" }));

    updateSpy.mockClear();
    await user.click(screen.getAllByLabelText("Clear")[0]);
    expect(base).toHaveValue("");
    await user.click(screen.getByRole("button", { name: /Save gitops|Saved/ }));
    await waitFor(() => expect(updateSpy).toHaveBeenCalledWith("prj_a", { base_branch: null }));
  });

  it("token chips insert on patterns only", async () => {
    const user = userEvent.setup();
    renderPage();
    const pattern = await screen.findByLabelText("Branch name pattern");
    await user.click(screen.getAllByRole("button", { name: "{item_id}" })[0]);
    expect(pattern).toHaveValue("{item_id}");
    expect(screen.getByLabelText("Base branch")).toHaveValue("");
    expect(screen.getByLabelText("Base branch")).toHaveAttribute("placeholder", UNMEASURED_PLACEHOLDER);
  });

  it("no_push_to_base is a tri-state select; null is sendable", async () => {
    gitopsSpy.mockResolvedValue(
      view({ fields: { no_push_to_base: { value: true, source: "project" } } }),
    );
    const user = userEvent.setup();
    renderPage();
    const push = await screen.findByLabelText("Do not push to the base");
    expect(push.tagName).toBe("SELECT");
    expect(push).toHaveValue("true");
    expect(screen.queryByRole("checkbox")).not.toBeInTheDocument();
    await user.selectOptions(push, "");
    await user.click(screen.getByRole("button", { name: "Save gitops" }));
    await waitFor(() => expect(updateSpy).toHaveBeenCalledWith("prj_a", { no_push_to_base: null }));
  });

  it("docs overlay matches gitops before the /settings catch-all", () => {
    const gitops = docFor(settingsPath("deployment/gitops"));
    expect(gitops.title).toBe("Gitops");
    expect(gitops.badge).toBe("GITOPS");
    expect(docFor(settingsPath("project/providers")).title).toBe("Settings");
    expect(docFor("/settings").title).toBe("Settings");
  });
});

describe("Gitops nav group source", () => {
  it("adds Gitops under This box without renaming the group", () => {
    const sources = import.meta.glob("../features/settings/SettingsView.tsx", {
      query: "?raw",
      import: "default",
      eager: true,
    }) as Record<string, string>;
    const src = Object.values(sources)[0] ?? "";
    expect(src).toContain('group: "This box"');
    expect(src).toContain('label: "Gitops"');
    expect(src).toContain('settingsPath("deployment/gitops")');
    expect(src).not.toMatch(/group:\s*"Deployment"/);
  });
});
