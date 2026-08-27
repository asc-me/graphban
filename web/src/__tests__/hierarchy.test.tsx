import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter, Route, Routes, useLocation } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { ProjectProvider, useProjectCtx } from "@/features/ProjectContext";
import { ORG_BASE, adminPath, orgPath, projectPath, tagFromPath, viewFromPath } from "@/lib/routes";
import type { Project } from "@/lib/types";

/**
 * PRD-21 D1 — the two-level hierarchy.
 *
 * The routes themselves are a morning's work; the part that can corrupt data is that the
 * project used to be an ambient module variable synced by an effect. These tests pin the
 * two properties that replace it: the route is the only source, and a tag is a lookup
 * rather than a grant.
 */

const core: Project = {
  id: "prj_core", name: "Core", tag: "CORE", accent: "#c6f24e", visibility: "private",
  description: "", share_global_memory: false, auto_extract: true, mcp_enabled: true,
  embed_model: "",
  credential_id: null,
  model_override: "", memory_auto_reject: true, memory_write_mode: "review",
  memory_llm_judge: false, agent_adjudication: false, allow_self_review: false,
};
const web: Project = { ...core, id: "prj_web", name: "Web", tag: "WEB", accent: "#7ca2ff" };

vi.mock("@/lib/api", () => ({
  api: { projects: vi.fn(async () => [core, web]) },
}));

function Probe() {
  const { active, notFound, loading } = useProjectCtx();
  const { pathname } = useLocation();
  if (loading) return <div>loading</div>;
  return (
    <div>
      <span data-testid="active">{active ? `${active.tag}:${active.id}` : "none"}</span>
      <span data-testid="notfound">{String(notFound)}</span>
      <span data-testid="path">{pathname}</span>
    </div>
  );
}

function renderAt(path: string) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={qc}>
      <MemoryRouter initialEntries={[path]}>
        <ProjectProvider>
          <Routes>
            <Route path="/p/:tag/*" element={<Probe />} />
            <Route path="*" element={<Probe />} />
          </Routes>
        </ProjectProvider>
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

describe("path helpers", () => {
  it("builds every org path from one base", () => {
    expect(orgPath()).toBe(ORG_BASE);
    expect(orgPath("projects")).toBe(`${ORG_BASE}/projects`);
    expect(adminPath("users")).toBe(`${ORG_BASE}/admin/users`);
  });

  it("has no source file spelling the org base as a literal", () => {
    // The assertion above cannot catch a hardcoded "/org": the literal and the base are
    // the same string today, so both spellings pass. What actually protects the per-org
    // host is that nothing outside routes.ts writes the base by hand — the moment the
    // base becomes "" for a custom domain, a stray literal is a link to a dead path.
    const sources = import.meta.glob("../**/*.{ts,tsx}", {
      query: "?raw",
      import: "default",
      eager: true,
    }) as Record<string, string>;

    const offenders: string[] = [];
    for (const [path, text] of Object.entries(sources)) {
      // Sibling test files resolve as `./galaxy.test.tsx` — no `__tests__` segment — so
      // excluding by directory alone let one through. Match the filename too.
      if (path.includes("/lib/routes.ts") || path.includes(".test.")) continue;
      for (const line of text.split("\n")) {
        // Prose in a doc comment may name the path — that is documentation, not a link.
        const trimmed = line.trim();
        if (trimmed.startsWith("*") || trimmed.startsWith("//") || trimmed.startsWith("/*")) {
          continue;
        }
        // A quoted path that starts with /org — `to="/org/admin"`, `"/org"`, etc.
        if (/["'`]\/org(\/|["'`])/.test(line)) offenders.push(`${path}: ${trimmed}`);
      }
    }
    expect(offenders).toEqual([]);
  });

  it("uses the tag verbatim — no canonical-lowercase second spelling", () => {
    expect(projectPath("GRPH", "code")).toBe("/p/GRPH/code");
    expect(projectPath("GRPH")).toBe("/p/GRPH");
  });

  it("reads the tag out of a path and ignores anything that is not one", () => {
    expect(tagFromPath("/p/GRPH/code")).toBe("GRPH");
    expect(tagFromPath("/p/grph")).toBe("grph"); // typed by a human; matched case-insensitively
    expect(tagFromPath("/org/admin/users")).toBeNull();
    expect(tagFromPath("/tracker")).toBeNull();
    // Too long to be a tag (TAG_RE is 2–4 chars), so it is not treated as one.
    expect(tagFromPath("/p/NOTATAGATALL/code")).toBeNull();
  });

  it("keeps the view when moving between projects", () => {
    expect(viewFromPath("/p/GRPH/prds/42")).toBe("prds/42");
    expect(viewFromPath("/p/GRPH")).toBe("");
  });
});

describe("the active project comes from the route", () => {
  // Visiting a project remembers it, so each case starts from a known store rather than
  // inheriting the previous test's last-used.
  beforeEach(() => localStorage.clear());

  it("resolves the tag in the URL", async () => {
    renderAt("/p/WEB/tracker");
    expect(await screen.findByTestId("active")).toHaveTextContent("WEB:prj_web");
  });

  it("matches case-insensitively, because a URL is typed by hand", async () => {
    renderAt("/p/web/tracker");
    expect(await screen.findByTestId("active")).toHaveTextContent("WEB:prj_web");
  });

  it("changes with the route rather than a render later", async () => {
    // The whole reason the ambient variable was deleted: two different URLs must resolve
    // to two different projects synchronously, with nothing cached in between.
    const first = renderAt("/p/CORE/tracker");
    expect(await screen.findByTestId("active")).toHaveTextContent("CORE:prj_core");
    first.unmount();

    renderAt("/p/WEB/tracker");
    expect(await screen.findByTestId("active")).toHaveTextContent("WEB:prj_web");
  });

  it("treats an unknown tag as not-found, never as a fallback project", async () => {
    // A tag is a lookup, not a grant. Falling back to the first readable project here
    // would silently show one project's data at another project's URL.
    renderAt("/p/ZZ/tracker");
    expect(await screen.findByTestId("notfound")).toHaveTextContent("true");
    expect(screen.getByTestId("active")).toHaveTextContent("none");
  });

  it("falls back to a project only where the path names none", async () => {
    renderAt("/tracker");
    expect(await screen.findByTestId("active")).toHaveTextContent("CORE:prj_core");
    expect(screen.getByTestId("notfound")).toHaveTextContent("false");
  });

  it("prefers the last project you were in when the path names none", async () => {
    // Last-used is a hint for *which page to show* and never an input to a request, so a
    // stale entry can send you to the wrong page but can never write to the wrong project.
    const visit = renderAt("/p/WEB/tracker");
    expect(await screen.findByTestId("active")).toHaveTextContent("WEB:prj_web");
    visit.unmount();

    renderAt("/tracker");
    expect(await screen.findByTestId("active")).toHaveTextContent("WEB:prj_web");
  });
});

describe("switching project", () => {
  it("navigates and keeps the current view", async () => {
    function Switcher() {
      const { projects, setActiveId } = useProjectCtx();
      return (
        <>
          <Probe />
          {projects.map((p) => (
            <button key={p.id} onClick={() => setActiveId(p.id)}>
              go {p.tag}
            </button>
          ))}
        </>
      );
    }
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    render(
      <QueryClientProvider client={qc}>
        <MemoryRouter initialEntries={["/p/CORE/prds/42"]}>
          <ProjectProvider>
            <Routes>
              <Route path="/p/:tag/*" element={<Switcher />} />
            </Routes>
          </ProjectProvider>
        </MemoryRouter>
      </QueryClientProvider>,
    );

    await userEvent.click(await screen.findByRole("button", { name: "go WEB" }));
    expect(screen.getByTestId("path")).toHaveTextContent("/p/WEB/prds/42");
    expect(screen.getByTestId("active")).toHaveTextContent("WEB:prj_web");
  });
});
