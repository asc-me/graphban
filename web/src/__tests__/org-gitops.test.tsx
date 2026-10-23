import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen } from "@testing-library/react";
import type { ReactNode } from "react";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { LeftNav } from "@/components/shell/LeftNav";
import { docFor } from "@/features/docs/content";
import { ProjectProvider } from "@/features/ProjectContext";
import { adminPath, consoleHref } from "@/lib/routes";

/**
 * The org gitops editor moved to graphban-control. What stays here is the rail:
 * an admin's Gitops entry leaves for the console, and a member does not see it.
 */

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
  projects: vi.fn(),
  orgs: vi.fn(),
}));

vi.mock("@/lib/api", () => ({
  api: {
    projects: spies.projects,
    orgs: spies.orgs,
    counts: vi.fn(async () => ({ items: 0, items_in_progress: 0, requests: 0, review: 0 })),
    adminWhoami: vi.fn(async () => ({ is_platform_admin: false })),
    syncStatus: vi.fn(async () => ({
      linked: false, source: "", cloud_url: "", org: "", credential_set: false, linked_at: null, projects: [],
    })),
  },
}));

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

describe("Gitops nav is admin-only", () => {
  beforeEach(() => {
    spies.projects.mockReset();
    spies.orgs.mockReset();
    spies.projects.mockResolvedValue([project]);
    spies.orgs.mockResolvedValue([{ id: "org_1", name: "Acme", plan: "team", role: "owner" }]);
  });

  it("shows Gitops for an admin as a link to the console", async () => {
    renderNav(<LeftNav hosted />);
    const gitops = await screen.findByRole("link", { name: "Gitops" });
    expect(gitops).toHaveAttribute("href", consoleHref(adminPath("gitops")));
    expect(screen.getByRole("link", { name: "Users & access" })).toHaveAttribute(
      "href",
      consoleHref(adminPath("users")),
    );
  });

  it("hides Gitops and the Admin group from a member", async () => {
    spies.orgs.mockResolvedValue([{ id: "org_1", name: "Acme", plan: "team", role: "member" }]);
    renderNav(<LeftNav hosted />);
    expect(await screen.findByText("Tracker")).toBeInTheDocument();
    expect(screen.queryByText("Gitops")).not.toBeInTheDocument();
    expect(screen.queryByText("Users & access")).not.toBeInTheDocument();
    expect(screen.getByText("no admin group")).toBeInTheDocument();
  });

  it("matches docs via adminPath, not a literal org prefix", () => {
    expect(docFor(adminPath("gitops")).title).toBe("Org gitops");
    expect(docFor(adminPath("gitops")).badge).toBe("GITOPS");
  });
});
