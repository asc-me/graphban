import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { docFor } from "@/features/docs/content";
import { ProjectProvider } from "@/features/ProjectContext";
import {
  GITOPS_BASE_CHIPS,
  GITOPS_CUSTOM,
  GITOPS_MODELS,
  GITOPS_MODEL_OPTIONS,
  GITOPS_NAMING_TOKENS,
  GITOPS_PRELINK_KEY,
  UNLINK_WARNING,
  UNMEASURED_PLACEHOLDER,
  UNTIL_LINKED,
  noteGitopsUnlinked,
} from "@/features/settings/GitopsPanel";
import { SettingsView } from "@/features/settings/SettingsView";
import { keys } from "@/lib/queries";
import { settingsPath } from "@/lib/routes";
import type { GitopsView, GitopsWas, Project } from "@/lib/types";

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

const unmeasured = { value: null, source: "unmeasured" as const };

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
    projects: [],
    version_from: version_from ?? unmeasured,
    release_defined_in: unmeasured,
    model: unmeasured,
    plan: null,
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

const { gitopsSpy, updateSpy, projectsSpy, configSpy } = vi.hoisted(() => ({
  gitopsSpy: vi.fn(async (_id: string) => state1),
  updateSpy: vi.fn(async (_id: string, _body: unknown) => state1),
  projectsSpy: vi.fn(async () => [projA, projB]),
  configSpy: vi.fn(async () => ({ hosted_mode: false, signup_mode: "closed" })),
}));

vi.mock("@/lib/api", () => ({
  api: {
    projects: projectsSpy,
    gitops: gitopsSpy,
    updateGitops: updateSpy,
    config: configSpy,
    // Hosted Settings tabs other than Gitops still mount; they must not throw
    // or the CALL pin never reaches HostedSettingsTabs.
    credentials: vi.fn(async () => []),
    platform: vi.fn(async () => null),
    syncStatus: vi.fn(async () => ({ linked: false, url: "", projects: [] })),
    members: vi.fn(async () => []),
    apiKeys: vi.fn(async () => []),
    createApiKey: vi.fn(),
    updateCheck: vi.fn(async () => ({
      state: "unknown",
      running: { version: "0.1.0", git_sha: "" },
      latest: { tag: "", url: "" },
      apply: false,
      hosted: true,
      note: "",
    })),
  },
}));

/** Same defaults as `web/src/main.tsx`. The cache/link tests must use these. */
const PRODUCTION_QUERIES = {
  retry: false as const,
  refetchOnWindowFocus: false as const,
  staleTime: 15_000,
};

function renderPage(path = settingsPath("deployment/gitops"), tag = "APP", qc?: QueryClient) {
  localStorage.setItem("gb_last_project_tag", tag);
  const client = qc ?? new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return {
    qc: client,
    ...render(
      <QueryClientProvider client={client}>
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
    sessionStorage.removeItem(GITOPS_PRELINK_KEY);
    localStorage.removeItem("gb_last_project_tag");
    projectsSpy.mockResolvedValue([projA, projB]);
    configSpy.mockResolvedValue({ hosted_mode: false, signup_mode: "closed" });
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

  it("unlink warning fires from the unlink call — Gitops need not have been visited while linked", async () => {
    noteGitopsUnlinked();
    gitopsSpy.mockResolvedValue(state2);
    renderPage();
    expect(await screen.findByRole("alert")).toHaveTextContent(UNLINK_WARNING);
    expect(screen.getByLabelText("Base branch")).toBeEnabled();
    expect(screen.getByLabelText("Base branch")).toHaveValue("test");
    expect(screen.getByRole("button", { name: "Save gitops" })).toBeEnabled();
  });

  it("does not first-paint a pre-link cache after link drops the row (production staleTime)", async () => {
    const qc = new QueryClient({ defaultOptions: { queries: PRODUCTION_QUERIES } });
    gitopsSpy.mockResolvedValue(state2);
    const first = renderPage(settingsPath("deployment/gitops"), "APP", qc);
    expect(await screen.findByLabelText("Base branch")).toHaveValue("test");
    first.unmount();
    qc.removeQueries({ queryKey: ["gitops"] });

    // A GET must not hide the cache: if the 15s-fresh row survived, this hang
    // never runs and `test` stays on screen.
    gitopsSpy.mockImplementation(() => new Promise(() => {}));
    const second = renderPage(settingsPath("deployment/gitops"), "APP", qc);
    expect(await screen.findByText(/Loading gitops/)).toBeInTheDocument();
    expect(screen.queryByDisplayValue("test")).not.toBeInTheDocument();
    expect(screen.queryByLabelText("Base branch")).not.toBeInTheDocument();
    second.unmount();
  });

  it("gcTime 0 drops the row on unmount so remount cannot serve cached test", async () => {
    const qc = new QueryClient({ defaultOptions: { queries: PRODUCTION_QUERIES } });
    gitopsSpy.mockResolvedValue(state2);
    const first = renderPage(settingsPath("deployment/gitops"), "APP", qc);
    expect(await screen.findByLabelText("Base branch")).toHaveValue("test");
    first.unmount();
    await waitFor(() => expect(qc.getQueryData(keys.gitops("prj_a"))).toBeUndefined());

    gitopsSpy.mockImplementation(() => new Promise(() => {}));
    const second = renderPage(settingsPath("deployment/gitops"), "APP", qc);
    expect(await screen.findByText(/Loading gitops/)).toBeInTheDocument();
    expect(screen.queryByDisplayValue("test")).not.toBeInTheDocument();
    second.unmount();
  });

  it("after the pane unmounts, a live GET is state 4, not cached test", async () => {
    const qc = new QueryClient({ defaultOptions: { queries: PRODUCTION_QUERIES } });
    gitopsSpy.mockResolvedValue(state2);
    const first = renderPage(settingsPath("deployment/gitops"), "APP", qc);
    expect(await screen.findByLabelText("Base branch")).toHaveValue("test");
    first.unmount();
    await waitFor(() => expect(qc.getQueryData(keys.gitops("prj_a"))).toBeUndefined());

    gitopsSpy.mockResolvedValue(state4);
    renderPage(settingsPath("deployment/gitops"), "APP", qc);
    expect(await screen.findByRole("status")).toHaveTextContent("Controlled by the org admin.");
    expect(screen.getByLabelText("Base branch")).toHaveValue("stage");
    expect(screen.queryByDisplayValue("test")).not.toBeInTheDocument();
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

  it("shows the filed checklist key and a Tracker link", async () => {
    gitopsSpy.mockResolvedValue(
      view({ plan: { id: "APP-12", title: "Gitops: PRs to base" } }),
    );
    renderPage();
    expect(await screen.findByText("APP-12")).toBeInTheDocument();
    expect(screen.getByText(/Gitops: PRs to base/)).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Tracker" })).toHaveAttribute("href", "/tracker");
    expect(screen.getByText(/Graphban does not run git/)).toBeInTheDocument();
    expect(screen.getByText(/Process, not code/)).toBeInTheDocument();
    expect(screen.getByText(/stall in review/)).toBeInTheDocument();
  });

  it("model picker is first, Unmeasured is first, and nothing is pre-selected", async () => {
    renderPage();
    const picker = await screen.findByLabelText("Gitops model");
    expect(picker.tagName).toBe("SELECT");
    expect(picker).toHaveValue("");
    const labels = [...picker.querySelectorAll("option")].map((o) => o.textContent);
    expect(labels[0]).toBe("Unmeasured");
    expect([...GITOPS_MODELS]).not.toContain(GITOPS_CUSTOM);
    expect(
      GITOPS_MODEL_OPTIONS.map(([id]) => id).filter((id) => id && id !== GITOPS_CUSTOM),
    ).toEqual([...GITOPS_MODELS]);
    expect(labels).toEqual([
      "Unmeasured",
      "Custom",
      "Push to base",
      "PRs to base",
      "PRs to integration",
    ]);
  });

  it("picker says Custom when fields are set and model is empty", async () => {
    // THE CALL. Empty model + measured fields used to look like Unmeasured.
    gitopsSpy.mockResolvedValue(
      view({ fields: { base_branch: { value: "main", source: "project" } } }),
    );
    renderPage();
    const picker = await screen.findByLabelText("Gitops model");
    expect(picker).toHaveValue(GITOPS_CUSTOM);
    expect(screen.getByRole("option", { name: "Custom" })).toBeInTheDocument();
    expect(screen.getByText(/no longer match a preset/i)).toBeInTheDocument();
  });

  it("selecting Custom does not PATCH model custom", async () => {
    gitopsSpy.mockResolvedValue(
      view({
        model: { value: "prs_to_base", source: "project" },
        fields: { base_branch: { value: "main", source: "project" } },
      }),
    );
    const user = userEvent.setup();
    renderPage();
    await user.selectOptions(await screen.findByLabelText("Gitops model"), GITOPS_CUSTOM);
    await user.click(screen.getByRole("button", { name: "Save gitops" }));
    await waitFor(() => expect(updateSpy).toHaveBeenCalled());
    const body = updateSpy.mock.calls[0][1] as { model?: unknown };
    expect(body.model).toBeNull();
    expect(body.model).not.toBe(GITOPS_CUSTOM);
  });

  it("picking PRs to base writes the preset fields and not the release locator", async () => {
    const user = userEvent.setup();
    renderPage();
    await user.selectOptions(await screen.findByLabelText("Gitops model"), "prs_to_base");
    expect(screen.getByLabelText("Branch name pattern")).toHaveValue("feat/{item_id}-{slug}");
    expect(screen.getByLabelText("PR title pattern")).toHaveValue("{item_id} {slug}");
    expect(screen.getByLabelText("Do not push to the base")).toHaveValue("true");
    expect(screen.getByLabelText("Reviewer bar")).toHaveValue("both");
    expect(screen.getByLabelText("Version from")).toHaveValue("calver");
    expect(screen.getByLabelText("Base branch")).toHaveValue("");
    expect(screen.getByLabelText("Release defined in")).toHaveValue("");
    await user.click(screen.getByRole("button", { name: "main" }));
    await user.click(screen.getByRole("button", { name: "Save gitops" }));
    await waitFor(() =>
      expect(updateSpy).toHaveBeenCalledWith(
        "prj_a",
        expect.objectContaining({
          model: "prs_to_base",
          base_branch: "main",
          no_push_to_base: true,
          reviewer_bar: "both",
          version_from: "calver",
        }),
      ),
    );
    const body = updateSpy.mock.calls[0][1] as { release_defined_in?: unknown };
    expect(body).not.toHaveProperty("release_defined_in");
  });

  it("hosted Settings never mounts GitopsPanel", async () => {
    // THE CALL (GRPH-620). This file always mocked hosted_mode:false, so putting
    // <GitopsPanel /> inside HostedSettingsTabs left every gitops-settings test
    // green. Render the hosted view; gitops must not load on any tab.
    configSpy.mockResolvedValue({ hosted_mode: true, signup_mode: "closed" });
    const user = userEvent.setup();
    renderPage("/settings");

    expect(await screen.findByRole("link", { name: /^AI Providers$/ })).toBeInTheDocument();
    expect(screen.queryByText("This box")).not.toBeInTheDocument();
    expect(screen.queryByRole("link", { name: "Gitops" })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Gitops" })).not.toBeInTheDocument();

    for (const tab of [
      "AI Providers",
      "Integrations",
      "Sync / Link",
      "Project",
      "Members",
      "API keys",
      "Account",
      "Updates",
    ]) {
      await user.click(screen.getByRole("link", { name: tab }));
      expect(screen.queryByRole("button", { name: "Save gitops" })).not.toBeInTheDocument();
      expect(screen.queryByLabelText("Base branch")).not.toBeInTheDocument();
    }
    expect(gitopsSpy).not.toHaveBeenCalled();
  });

  it("SelfHostPane does not grey Gitops with a wrapper — only control.writable does", async () => {
    // THE CALL (GRPH-620). Wrapping <GitopsPanel /> in opacity-60 pointer-events-none
    // at SelfHostPane left every callee test green: inputs stay enabled, the banner
    // still comes from control.message. Greying a writable page is the lie.
    renderPage();
    expect(await screen.findByLabelText("Base branch")).toBeEnabled();
    const heading = screen.getByRole("heading", { name: /^Gitops$/ });
    for (let el: HTMLElement | null = heading; el && el !== document.body; el = el.parentElement) {
      const cls = typeof el.className === "string" ? el.className : "";
      expect(cls).not.toMatch(/\bopacity-60\b/);
      expect(cls).not.toMatch(/\bpointer-events-none\b/);
    }
  });

  it("docs overlay matches gitops before the /settings catch-all", () => {
    const gitops = docFor(settingsPath("deployment/gitops"));
    expect(gitops.title).toBe("Gitops");
    expect(gitops.badge).toBe("GITOPS");
    expect(docFor(settingsPath("deployment/providers")).title).toBe("AI providers");
    expect(docFor(settingsPath("project/providers")).title).toBe("AI providers");
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

  it("SelfHostPane returns GitopsPanel directly, and HostedSettingsTabs never names it", () => {
    const sources = import.meta.glob("../features/settings/SettingsView.tsx", {
      query: "?raw",
      import: "default",
      eager: true,
    }) as Record<string, string>;
    const src = Object.values(sources)[0] ?? "";
    const live = src.replace(/\/\*[\s\S]*?\*\//g, "").replace(/\/\/.*$/gm, "");
    expect(live).toMatch(
      /if \(pathname\.startsWith\(settingsPath\("deployment\/gitops"\)\)\) return <GitopsPanel \/>/,
    );
    expect(live).toContain('label: "AI Providers"');
    const hosted = live.split("function HostedSettingsTabs")[1] ?? "";
    expect(hosted).not.toContain("GitopsPanel");
    expect(hosted).not.toContain("SyncLinkPanel");
    expect(hosted).toContain("CloudOrgLinkPanel");
  });
});
