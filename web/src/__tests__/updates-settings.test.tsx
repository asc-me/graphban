import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { docFor } from "@/features/docs/content";
import { ProjectProvider } from "@/features/ProjectContext";
import { SettingsView } from "@/features/settings/SettingsView";
import { installTiming, pageReload } from "@/features/settings/UpdatesPanel";
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
    notes: {
      running: { tag: "2026.09.1", state: "empty", body: "" },
      latest: null,
    },
    apply: false,
    via: "",
    hosted: false,
    note: "",
    ...over,
  };
}

function available(): UpdateCheck {
  return payload({
    state: "available",
    apply: true,
    via: "compose",
    latest: { tag: "2026.10.1", url: "https://example/2026.10.1" },
  });
}

const { updateCheckSpy, updateApplySpy, healthSpy, projectsSpy, configSpy } = vi.hoisted(() => ({
  updateCheckSpy: vi.fn(async () => payload()),
  updateApplySpy: vi.fn(async (tag: string) => ({ ok: true, started: true, tag })),
  healthSpy: vi.fn(async (_timeoutMs: number) => ({ status: "ok", version: "2026.09.1", git_sha: "d596e57" })),
  projectsSpy: vi.fn(async () => [proj]),
  configSpy: vi.fn(async () => ({ hosted_mode: false, signup_mode: "closed" })),
}));

vi.mock("@/lib/api", () => ({
  api: {
    projects: projectsSpy,
    config: configSpy,
    updateCheck: updateCheckSpy,
    updateApply: updateApplySpy,
    health: healthSpy,
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
    vi.restoreAllMocks();
    healthSpy.mockReset().mockResolvedValue({ status: "ok", version: "2026.09.1", git_sha: "d596e57" });
    updateApplySpy.mockReset().mockImplementation(async (tag: string) => ({ ok: true, started: true, tag }));
    localStorage.setItem("gb_last_project_tag", "APP");
    projectsSpy.mockResolvedValue([proj]);
    updateCheckSpy.mockResolvedValue(payload());
    Object.assign(installTiming, { pollMs: 1, requestMs: 50, budgetMs: 2000, reloadMs: 1 });
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
    expect(screen.getAllByText("2026.09.1").length).toBeGreaterThan(0);
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
        via: "compose",
        latest: { tag: "2026.10.1", url: "https://example/2026.10.1" },
      }),
    );
    renderPage();
    await waitFor(() => expect(screen.getByRole("button", { name: /^install$/i })).toBeEnabled());
  });

  it("THE CALL: Confirm install posts the advertised tag, says it installed, and reloads", async () => {
    const user = userEvent.setup();
    const reload = vi.spyOn(pageReload, "run").mockImplementation(() => {});
    updateCheckSpy.mockResolvedValue(available());
    healthSpy.mockResolvedValue({ status: "ok", version: "2026.10.1", git_sha: "newsha" });
    renderPage();
    await user.click(await screen.findByRole("button", { name: /^install$/i }));
    await user.click(screen.getByRole("button", { name: /confirm install 2026\.10\.1/i }));
    await waitFor(() => expect(updateApplySpy).toHaveBeenCalledWith("2026.10.1"));
    expect(await screen.findByText(/installed 2026\.10\.1/i)).toBeInTheDocument();
    await waitFor(() => expect(reload).toHaveBeenCalledTimes(1));
  });

  it("install walks building, then restarting, then installed", async () => {
    const user = userEvent.setup();
    const reload = vi.spyOn(pageReload, "run").mockImplementation(() => {});
    let serving: "old" | "down" | "new" = "old";
    healthSpy.mockImplementation(async () => {
      if (serving === "down") throw new TypeError("Failed to fetch");
      return { status: "ok", version: serving === "old" ? "2026.09.1" : "2026.10.1", git_sha: "x" };
    });
    updateCheckSpy.mockResolvedValue(available());
    renderPage();
    await user.click(await screen.findByRole("button", { name: /^install$/i }));
    await user.click(screen.getByRole("button", { name: /confirm install 2026\.10\.1/i }));
    expect(await screen.findByText(/building 2026\.10\.1\. this box keeps serving 2026\.09\.1/i)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /installing/i })).toBeDisabled();
    expect(reload).not.toHaveBeenCalled();
    serving = "down";
    expect(await screen.findByText(/restarting on 2026\.10\.1/i)).toBeInTheDocument();
    serving = "new";
    expect(await screen.findByText(/installed 2026\.10\.1/i)).toBeInTheDocument();
    await waitFor(() => expect(reload).toHaveBeenCalledTimes(1));
  });

  it("the install poll reads /health, not the GitHub-backed update check", async () => {
    const user = userEvent.setup();
    vi.spyOn(pageReload, "run").mockImplementation(() => {});
    let polls = 0;
    healthSpy.mockImplementation(async () => {
      polls += 1;
      return { status: "ok", version: polls > 20 ? "2026.10.1" : "2026.09.1", git_sha: "x" };
    });
    updateCheckSpy.mockResolvedValue(available());
    renderPage();
    await user.click(await screen.findByRole("button", { name: /^install$/i }));
    const before = updateCheckSpy.mock.calls.length;
    await user.click(screen.getByRole("button", { name: /confirm install 2026\.10\.1/i }));
    expect(await screen.findByText(/installed 2026\.10\.1/i)).toBeInTheDocument();
    expect(polls).toBeGreaterThan(20);
    // One refetch after success so the panel shows the new cut; none per poll.
    expect(updateCheckSpy.mock.calls.length - before).toBeLessThanOrEqual(1);
  });

  it("a box that never serves the tag ends in an error that names the helper log", async () => {
    const user = userEvent.setup();
    const reload = vi.spyOn(pageReload, "run").mockImplementation(() => {});
    Object.assign(installTiming, { budgetMs: 60 });
    updateCheckSpy.mockResolvedValue(available());
    renderPage();
    await user.click(await screen.findByRole("button", { name: /^install$/i }));
    await user.click(screen.getByRole("button", { name: /confirm install 2026\.10\.1/i }));
    expect(await screen.findByText(/still not serving 2026\.10\.1/i)).toBeInTheDocument();
    expect(screen.getByText(/deploy\.log/)).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /installing/i })).not.toBeInTheDocument();
    expect(reload).not.toHaveBeenCalled();
  });

  it("a refused apply says why and does not poll", async () => {
    const user = userEvent.setup();
    updateApplySpy.mockRejectedValueOnce(
      Object.assign(new Error(JSON.stringify({ detail: "tag is not the advertised cut" })), { status: 409 }),
    );
    updateCheckSpy.mockResolvedValue(available());
    renderPage();
    await user.click(await screen.findByRole("button", { name: /^install$/i }));
    await user.click(screen.getByRole("button", { name: /confirm install 2026\.10\.1/i }));
    expect(await screen.findByText(/install was refused: tag is not the advertised cut/i)).toBeInTheDocument();
    expect(healthSpy).not.toHaveBeenCalled();
  });

  it("a dropped apply request is the apply starting, so it still polls", async () => {
    const user = userEvent.setup();
    vi.spyOn(pageReload, "run").mockImplementation(() => {});
    updateApplySpy.mockRejectedValueOnce(new TypeError("Failed to fetch"));
    healthSpy.mockResolvedValue({ status: "ok", version: "2026.10.1", git_sha: "newsha" });
    updateCheckSpy.mockResolvedValue(available());
    renderPage();
    await user.click(await screen.findByRole("button", { name: /^install$/i }));
    await user.click(screen.getByRole("button", { name: /confirm install 2026\.10\.1/i }));
    expect(await screen.findByText(/installed 2026\.10\.1/i)).toBeInTheDocument();
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
    expect(screen.getByText(/hosted\. the operator updates this instance/i)).toBeInTheDocument();
  });

  it("empty via is not compose", async () => {
    updateCheckSpy.mockResolvedValue(
      payload({
        state: "available",
        apply: true,
        via: "",
        latest: { tag: "2026.10.1", url: "https://example/2026.10.1" },
      }),
    );
    renderPage();
    expect(await screen.findByText(/how this box is deployed/i)).toBeInTheDocument();
    expect(screen.getByText(/could not tell — not compose/i)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /^install$/i })).toBeDisabled();
  });

  it("names compose and native methods", async () => {
    updateCheckSpy.mockResolvedValue(
      payload({ via: "compose", apply: true, state: "available" }),
    );
    const { unmount } = renderPage();
    expect(await screen.findByText(/compose, with a host helper/i)).toBeInTheDocument();
    unmount();
    updateCheckSpy.mockResolvedValue(payload({ via: "native" }));
    renderPage();
    expect(await screen.findByText(/native install at \/opt\/graphban/i)).toBeInTheDocument();
  });

  it("THE CALL: Check for updates refetches the payload", async () => {
    const user = userEvent.setup();
    renderPage();
    const btn = await screen.findByRole("button", { name: /check for updates/i });
    expect(updateCheckSpy).toHaveBeenCalledTimes(1);
    await user.click(btn);
    await waitFor(() => expect(updateCheckSpy).toHaveBeenCalledTimes(2));
  });

  it("current has one notes accordion, not a duplicate latest list", async () => {
    updateCheckSpy.mockResolvedValue(
      payload({
        notes: {
          running: { tag: "2026.09.1", state: "present", body: "On this box.\n" },
          latest: null,
        },
      }),
    );
    renderPage();
    expect(await screen.findByText("This release")).toBeInTheDocument();
    expect(screen.getByText(/on this box/i)).toBeInTheDocument();
    expect(screen.queryByText("Latest release")).not.toBeInTheDocument();
  });

  it("available shows this release and latest notes", async () => {
    updateCheckSpy.mockResolvedValue(
      payload({
        state: "available",
        apply: true,
        via: "compose",
        latest: { tag: "2026.10.1", url: "https://example/2026.10.1" },
        notes: {
          running: { tag: "2026.09.1", state: "empty", body: "" },
          latest: { tag: "2026.10.1", state: "present", body: "What is new.\n" },
        },
      }),
    );
    renderPage();
    expect(await screen.findByText("This release")).toBeInTheDocument();
    expect(screen.getByText("Latest release")).toBeInTheDocument();
    expect(screen.getByText(/what is new/i)).toBeInTheDocument();
    expect(screen.getByText(/no notes on this release/i)).toBeInTheDocument();
  });

  it("unknown notes are not an empty changelog", async () => {
    updateCheckSpy.mockResolvedValue(
      payload({
        notes: {
          running: { tag: "2026.09.1", state: "unknown", body: "" },
          latest: null,
        },
      }),
    );
    renderPage();
    expect(await screen.findByText(/could not load notes for this release/i)).toBeInTheDocument();
    expect(screen.queryByText(/no notes on this release/i)).not.toBeInTheDocument();
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
