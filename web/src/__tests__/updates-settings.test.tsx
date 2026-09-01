import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { docFor } from "@/features/docs/content";
import { ProjectProvider } from "@/features/ProjectContext";
import { SettingsView } from "@/features/settings/SettingsView";
import { settingsPath } from "@/lib/routes";
import type { Project, UpdateCheck } from "@/lib/types";

const proj: Project = {
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

function payload(over: Partial<UpdateCheck> = {}): UpdateCheck {
  return {
    state: "current",
    running: { version: "2026.09.1", git_sha: "d596e57" },
    latest: { tag: "2026.09.1", url: "https://github.com/asc-me/graphban/releases/tag/2026.09.1" },
    apply: false,
    hosted: false,
    note: "",
    ...over,
  };
}

const { updateCheckSpy, updateApplySpy, projectsSpy, configSpy } = vi.hoisted(() => ({
  updateCheckSpy: vi.fn(async () => payload()),
  updateApplySpy: vi.fn(async (tag: string) => ({ ok: true, started: true, tag })),
  projectsSpy: vi.fn(async () => [proj]),
  configSpy: vi.fn(async () => ({ hosted_mode: false, signup_mode: "closed" })),
}));

vi.mock("@/lib/api", () => ({
  api: {
    projects: projectsSpy,
    config: configSpy,
    updateCheck: updateCheckSpy,
    updateApply: updateApplySpy,
    credentials: vi.fn(async () => []),
    platform: vi.fn(async () => null),
    syncStatus: vi.fn(async () => ({ linked: false, url: "", projects: [] })),
    members: vi.fn(async () => []),
    apiKeys: vi.fn(async () => []),
    gitops: vi.fn(async () => ({ fields: {}, control: { state: "local", writable: true, message: "" } })),
  },
}));

function renderPage(path = settingsPath("deployment/updates"), hosted = false) {
  configSpy.mockResolvedValue({ hosted_mode: hosted, signup_mode: "closed" });
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={client}>
      <MemoryRouter initialEntries={[path]}>
        <ProjectProvider>
          <Routes>
            <Route path="/settings/*" element={<SettingsView />} />
          </Routes>
        </ProjectProvider>
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

describe("Updates Settings page", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    localStorage.setItem("gb_last_project_tag", "APP");
    projectsSpy.mockResolvedValue([proj]);
    updateCheckSpy.mockResolvedValue(payload());
  });

  it("sits under This box as Updates", async () => {
    renderPage();
    expect(await screen.findByText("This box")).toBeInTheDocument();
    const link = screen.getByRole("link", { name: "Updates" });
    expect(link).toHaveAttribute("href", settingsPath("deployment/updates"));
  });

  it("current says this box is on the latest release and has Check plus disabled Install", async () => {
    renderPage();
    expect((await screen.findAllByText(/on the latest release/i)).length).toBeGreaterThan(0);
    expect(screen.getByText("2026.09.1")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /check for updates/i })).toBeEnabled();
    const install = screen.getByRole("button", { name: /^install$/i });
    expect(install).toBeDisabled();
    expect(screen.getByText(/already on the latest release/i)).toBeInTheDocument();
  });

  it("available names the newer tag and does not say latest release", async () => {
    updateCheckSpy.mockResolvedValue(
      payload({
        state: "available",
        latest: { tag: "2026.10.1", url: "https://example/2026.10.1" },
      }),
    );
    renderPage();
    expect(await screen.findByText(/is available/i)).toBeInTheDocument();
    expect(screen.getAllByText(/2026\.10\.1/).length).toBeGreaterThan(0);
    expect(screen.queryByText(/on the latest release/i)).not.toBeInTheDocument();
    expect(screen.queryByText(/up to date/i)).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: /check for updates/i })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /^install$/i })).toBeDisabled();
  });

  it("available with apply enables Install", async () => {
    updateCheckSpy.mockResolvedValue(
      payload({
        state: "available",
        apply: true,
        latest: { tag: "2026.10.1", url: "https://example/2026.10.1" },
      }),
    );
    renderPage();
    await waitFor(() => expect(screen.getByRole("button", { name: /^install$/i })).toBeEnabled());
  });

  it("THE CALL: Confirm install posts the advertised tag", async () => {
    const user = userEvent.setup();
    updateCheckSpy.mockResolvedValue(
      payload({
        state: "available",
        apply: true,
        latest: { tag: "2026.10.1", url: "https://example/2026.10.1" },
      }),
    );
    updateApplySpy.mockImplementation(async (tag: string) => {
      updateCheckSpy.mockResolvedValue(
        payload({
          state: "current",
          apply: true,
          running: { version: "2026.10.1", git_sha: "newsha" },
          latest: { tag: "2026.10.1", url: "https://example/2026.10.1" },
        }),
      );
      return { ok: true, started: true, tag };
    });
    renderPage();
    await user.click(await screen.findByRole("button", { name: /^install$/i }));
    await user.click(screen.getByRole("button", { name: /confirm install 2026\.10\.1/i }));
    await waitFor(() => expect(updateApplySpy).toHaveBeenCalledWith("2026.10.1"));
    expect((await screen.findAllByText(/on the latest release/i)).length).toBeGreaterThan(0);
  });

  it("unknown does not look current", async () => {
    updateCheckSpy.mockResolvedValue(
      payload({
        state: "unknown",
        latest: null,
        note: "could not reach the update feed — not current",
        running: { version: "2026.09.1", git_sha: "d596e57" },
      }),
    );
    renderPage();
    expect(await screen.findByText(/could not tell whether a newer cut exists/i)).toBeInTheDocument();
    expect(screen.queryByText(/on the latest release/i)).not.toBeInTheDocument();
    expect(screen.queryByText(/up to date/i)).not.toBeInTheDocument();
    expect(screen.queryByText(/is available/i)).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: /^install$/i })).toBeDisabled();
  });

  it("hosted has Check and no Install", async () => {
    updateCheckSpy.mockResolvedValue(payload({ hosted: true }));
    renderPage(settingsPath("deployment/updates"), true);
    expect(await screen.findByRole("button", { name: /check for updates/i })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /^install$/i })).not.toBeInTheDocument();
  });

  it("THE CALL: Check for updates refetches the payload", async () => {
    const user = userEvent.setup();
    renderPage();
    const btn = await screen.findByRole("button", { name: /check for updates/i });
    expect(updateCheckSpy).toHaveBeenCalledTimes(1);
    await user.click(btn);
    await waitFor(() => expect(updateCheckSpy).toHaveBeenCalledTimes(2));
  });

  it("docs overlay matches Updates before the /settings catch-all", () => {
    const doc = docFor(settingsPath("deployment/updates"));
    expect(doc.title).toBe("Updates");
    expect(doc.badge).toBe("UPDATES");
    expect(docFor("/settings").title).toBe("Settings");
  });
});

describe("Updates nav source", () => {
  it("adds Updates under This box", () => {
    const sources = import.meta.glob("../features/settings/SettingsView.tsx", {
      query: "?raw",
      import: "default",
      eager: true,
    }) as Record<string, string>;
    const src = Object.values(sources)[0] ?? "";
    expect(src).toContain('group: "This box"');
    expect(src).toContain('label: "Updates"');
    expect(src).toContain('settingsPath("deployment/updates")');
  });
});
