import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { docFor } from "@/features/docs/content";
import { ProjectProvider } from "@/features/ProjectContext";
import { SettingsView } from "@/features/settings/SettingsView";
import { installTiming, pageReload } from "@/features/settings/UpdatesPanel";
import { api } from "@/lib/api";
import { settingsPath } from "@/lib/routes";
import { readFileSync } from "node:fs";
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

const { updateCheckSpy, updateApplySpy, healthSpy, progressSpy, projectsSpy, configSpy } = vi.hoisted(() => ({
  updateCheckSpy: vi.fn(async () => payload()),
  updateApplySpy: vi.fn(async (tag: string) => ({ ok: true, started: true, tag })),
  healthSpy: vi.fn(async (_timeoutMs: number) => ({ status: "ok", version: "2026.09.1", git_sha: "d596e57" })),
  progressSpy: vi.fn(async (_timeoutMs: number): Promise<unknown> => ({
    state: "running", reason: "", tag: "2026.10.1", steps: [], current_line: "", error: "",
  })),
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
    updateProgress: progressSpy,
    rulePackStatus: vi.fn(async () => ({ installed: false, loaded: false, name: null, version: null, sha256: null, installed_at: null, installed_by: null, reason: "none" })),
    rulePackAvailable: vi.fn(async () => ({ state: "current", installed_version: null, version: null, notes: null, feed_error: null })),
    autoUpdate: vi.fn(async () => ({
      available: false, unavailable_reason: "no apply path", enabled: false, timezone: "UTC",
      timezone_set: false, host_now: "2026-11-02T10:30:00+00:00",
      check: { days: [0, 1, 2, 3, 4, 5, 6], start: "02:00", end: "05:00" },
      install: { days: [0, 1, 2, 3, 4, 5, 6], start: "02:00", end: "05:00" },
      next_check_at: null, last_check_at: null, last_check_state: "", last_auto_install_tag: "",
      last_auto_install_at: null, last_auto_install_outcome: "", last_auto_install_error: "",
    })),
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
    progressSpy.mockReset().mockResolvedValue({
      state: "idle", reason: "no log", tag: "", steps: [], current_line: "", error: "",
    });
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
    expect(await screen.findByRole("heading", { name: /update available/i })).toBeInTheDocument();
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
    await waitFor(() => expect(screen.getByTestId("phase-copy").textContent).toMatch(
      /building 2026\.10\.1\. this box keeps serving 2026\.09\.1/i));
    expect(screen.queryByRole("button", { name: /installing/i })).not.toBeInTheDocument();
    expect(reload).not.toHaveBeenCalled();
    serving = "down";
    await waitFor(() => expect(screen.getByTestId("phase-copy").textContent).toMatch(
      /restarting the API on 2026\.10\.1/i));
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

  it("after Install it polls update-progress, shows step marks and a timer, and restarts on a dropped request", async () => {
    const user = userEvent.setup();
    const reload = vi.spyOn(pageReload, "run").mockImplementation(() => {});
    const startedAt = Math.floor(Date.now() / 1000) - 75;
    let mode: "steps" | "down" | "new" = "steps";
    progressSpy.mockImplementation(async () => {
      if (mode !== "steps") throw new TypeError("Failed to fetch");
      return {
        state: "running", reason: "", tag: "2026.10.1", error: "",
        current_line: "#12 RUN pip install",
        steps: [
          { key: "pin", label: "Pinning the deploy worktree", status: "done", started_at: startedAt - 10 },
          { key: "build", label: "Building the images", status: "active", started_at: startedAt },
          { key: "verify", label: "Verifying release identity", status: "pending", started_at: null },
        ],
      };
    });
    healthSpy.mockImplementation(async () => {
      if (mode === "down") throw new TypeError("Failed to fetch");
      return { status: "ok", version: mode === "new" ? "2026.10.1" : "2026.09.1", git_sha: "x" };
    });
    updateCheckSpy.mockResolvedValue(available());
    progressSpy.mockResolvedValueOnce({ state: "idle", reason: "", tag: "", steps: [], current_line: "", error: "" }); // the mount probe
    renderPage();
    await user.click(await screen.findByRole("button", { name: /^install$/i }));
    await user.click(screen.getByRole("button", { name: /confirm install 2026\.10\.1/i }));
    expect(await screen.findByText(/Building the images/)).toBeInTheDocument();
    const steps = screen.getByRole("list", { name: /install steps/i }).querySelectorAll("li");
    expect(Array.from(steps).map((li) => li.getAttribute("data-status"))).toEqual(["done", "active", "pending"]);
    expect(screen.getByTestId("step-timer").textContent).toMatch(/^1:\d\d$/);
    expect(screen.getByText("#12 RUN pip install")).toBeInTheDocument();
    mode = "down";
    expect(await screen.findByText(/restarting the API/i)).toBeInTheDocument();
    mode = "new";
    expect(await screen.findByText(/installed 2026\.10\.1/i)).toBeInTheDocument();
    await waitFor(() => expect(reload).toHaveBeenCalledTimes(1));
  });

  it("says why there are no steps when update-progress is idle with a reason", async () => {
    const user = userEvent.setup();
    progressSpy.mockResolvedValue({
      state: "idle", reason: "no install log on this box", tag: "", steps: [], current_line: "", error: "",
    });
    healthSpy.mockResolvedValue({ status: "ok", version: "2026.09.1", git_sha: "x" });
    updateCheckSpy.mockResolvedValue(available());
    renderPage();
    await user.click(await screen.findByRole("button", { name: /^install$/i }));
    await user.click(screen.getByRole("button", { name: /confirm install 2026\.10\.1/i }));
    const status = await screen.findByRole("status");
    await waitFor(() => expect(status).toHaveTextContent("Step progress is not available: no install log on this box"));
    expect(screen.queryByRole("list", { name: /install steps/i })).toBeNull();
  });

  it("a failed install shows the error line and the log location, and stops polling", async () => {
    const user = userEvent.setup();
    const reload = vi.spyOn(pageReload, "run").mockImplementation(() => {});
    progressSpy.mockResolvedValue({
      state: "failed", reason: "", tag: "2026.10.1", current_line: "!! api serves a, expected b",
      error: "!! api serves a, expected b",
      steps: [{ key: "verify", label: "Verifying release identity", status: "failed", started_at: null }],
    });
    updateCheckSpy.mockResolvedValue(available());
    renderPage();
    await user.click(await screen.findByRole("button", { name: /^install$/i }));
    await user.click(screen.getByRole("button", { name: /confirm install 2026\.10\.1/i }));
    expect(await screen.findByText(/install failed: !! api serves a, expected b/i)).toBeInTheDocument();
    expect(screen.getByText(/deploy\.log/)).toBeInTheDocument();
    const calls = progressSpy.mock.calls.length;
    await new Promise((r) => setTimeout(r, 30));
    expect(progressSpy.mock.calls.length).toBe(calls);
    expect(reload).not.toHaveBeenCalled();
  });

  it("progress for another tag is an older attempt: no steps shown and no failure", async () => {
    const user = userEvent.setup();
    const reload = vi.spyOn(pageReload, "run").mockImplementation(() => {});
    progressSpy.mockResolvedValue({
      state: "failed", reason: "", tag: "2026.09.1", current_line: "!! old failure",
      error: "!! old failure",
      steps: [{ key: "build", label: "Building the images", status: "failed", started_at: null }],
    });
    healthSpy.mockResolvedValue({ status: "ok", version: "2026.09.1", git_sha: "x" });
    updateCheckSpy.mockResolvedValue(available());
    renderPage();
    await user.click(await screen.findByRole("button", { name: /^install$/i }));
    await user.click(screen.getByRole("button", { name: /confirm install 2026\.10\.1/i }));
    await waitFor(() => expect(progressSpy.mock.calls.length).toBeGreaterThan(2));
    expect(screen.queryByText(/install failed/i)).not.toBeInTheDocument();
    expect(screen.queryByText(/old failure/)).not.toBeInTheDocument();
    expect(screen.queryByRole("list", { name: /install steps/i })).not.toBeInTheDocument();
    expect(screen.getByTestId("phase-copy")).toBeInTheDocument();
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

  it("an unreachable feed names why, instead of reading as no release", async () => {
    updateCheckSpy.mockResolvedValue(
      payload({
        state: "unknown",
        latest: null,
        feed_error: "GitHub returned 403 — rate limited or throttled",
      }),
    );
    renderPage();
    expect(await screen.findByText(/GitHub returned 403/)).toBeInTheDocument();
    expect(screen.queryByText(/on the latest release/i)).not.toBeInTheDocument();
  });

  it("hosted has Check and no Install", async () => {
    updateCheckSpy.mockResolvedValue(payload({ hosted: true }));
    renderPage(settingsPath("deployment/updates"), true);
    // The page settles (and may remount) once the hosted config arrives; assert on the settled DOM.
    await waitFor(() => {
      expect(screen.getByRole("button", { name: /check for updates/i })).toBeInTheDocument();
      expect(screen.getByText(/hosted\. the operator updates this instance/i)).toBeInTheDocument();
    });
    expect(screen.queryByRole("button", { name: /^install$/i })).not.toBeInTheDocument();
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

  it("puts Check and Install in the Status header, before the release notes", async () => {
    updateCheckSpy.mockResolvedValue(available());
    renderPage();
    const check = await screen.findByRole("button", { name: /check for updates/i });
    const install = screen.getByRole("button", { name: /^install$/i });
    const notes = screen.getByText("Release notes");
    expect(check.compareDocumentPosition(notes) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
    expect(install.compareDocumentPosition(notes) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
  });

  it("shows step 4 of 6 with one segment per step and the failed one marked", async () => {
    updateCheckSpy.mockResolvedValue(available());
    const mk = (statuses: string[]) => ({
      state: "running", reason: "", tag: "2026.10.1", current_line: "", error: "",
      steps: statuses.map((status, i) => ({ key: `s${i}`, label: `Step ${i + 1}`, status, started_at: null })),
    });
    progressSpy.mockResolvedValue(mk(["done", "done", "done", "active", "pending", "pending"]));
    const user = userEvent.setup();
    progressSpy.mockResolvedValueOnce({ state: "idle", reason: "", tag: "", steps: [], current_line: "", error: "" }); // the mount probe
    renderPage();
    await user.click(await screen.findByRole("button", { name: /^install$/i }));
    await user.click(screen.getByRole("button", { name: /confirm install/i }));
    expect(await screen.findByText(/step 4 of 6/)).toBeInTheDocument();
    expect(screen.getAllByTestId("step-segment")).toHaveLength(6);
    progressSpy.mockResolvedValue({ ...mk(["done", "done", "done", "failed", "pending", "pending"]), state: "failed", error: "boom" });
    expect(await screen.findByText(/stopped at step 4/)).toBeInTheDocument();
    expect(screen.getAllByTestId("step-segment")[3]).toHaveAttribute("data-status", "failed");
  });

  it("a native install has no step total: no \"of M\", \"total not known\", one segment per step returned", async () => {
    updateCheckSpy.mockResolvedValue({ ...available(), via: "native" });
    progressSpy.mockResolvedValue({
      state: "running", reason: "", tag: "2026.10.1", current_line: "", error: "",
      steps: [
        { key: "download", label: "Downloading", status: "done", started_at: null },
        { key: "unpack", label: "Unpacking", status: "active", started_at: null },
      ],
    });
    const user = userEvent.setup();
    progressSpy.mockResolvedValueOnce({ state: "idle", reason: "", tag: "", steps: [], current_line: "", error: "" }); // the mount probe
    renderPage();
    await user.click(await screen.findByRole("button", { name: /^install$/i }));
    await user.click(screen.getByRole("button", { name: /confirm install/i }));
    const headline = await screen.findByTestId("progress-headline");
    expect(headline).toHaveTextContent(/^Upgrading to 2026\.10\.1 — step 2$/);
    expect(headline.textContent).not.toMatch(/\bof\b/);
    expect(screen.getByTestId("step-count")).toHaveTextContent("1 done · total not known");
    expect(screen.getAllByTestId("step-segment")).toHaveLength(2);
    expect(screen.getByTestId("step-tail")).toBeInTheDocument();
    expect(screen.getByText("Further steps appear only if the log prints them")).toBeInTheDocument();
    // Frame 5b copy: "Upgrading to <tag>." plus the restart note, never the compose sentence.
    expect(screen.getByTestId("phase-copy")).toHaveTextContent(/^Upgrading to 2026\.10\.1\.$/);
    expect(screen.getByTestId("native-note")).toHaveTextContent(/^The service restarts mid-upgrade/);
    expect(screen.queryByText(/This box keeps serving/)).toBeNull();
    expect(screen.queryByText(/Building 2026/)).toBeNull();
    // Only the two printed steps plus the trailing note; no pending step invented.
    const items = screen.getByRole("list", { name: /install steps/i }).querySelectorAll("li[data-status]");
    expect(items).toHaveLength(2);
  });

  it("a compose install shows the done / total count", async () => {
    updateCheckSpy.mockResolvedValue(available());
    progressSpy.mockResolvedValue({
      state: "running", reason: "", tag: "2026.10.1", current_line: "", error: "",
      steps: [
        { key: "a", label: "Fetch", status: "done", started_at: null },
        { key: "b", label: "Build", status: "active", started_at: null },
      ],
    });
    const user = userEvent.setup();
    progressSpy.mockResolvedValueOnce({ state: "idle", reason: "", tag: "", steps: [], current_line: "", error: "" }); // the mount probe
    renderPage();
    await user.click(await screen.findByRole("button", { name: /^install$/i }));
    await user.click(screen.getByRole("button", { name: /confirm install/i }));
    expect(await screen.findByTestId("step-count")).toHaveTextContent("1 / 2");
    expect(screen.queryByTestId("step-tail")).toBeNull();
    expect(screen.queryByText(/Further steps appear/)).toBeNull();
    expect(screen.queryByTestId("native-note")).toBeNull();
  });

  it("a failure with no failed step reads \"Upgrade to <tag> failed\"", async () => {
    updateCheckSpy.mockResolvedValue(available());
    progressSpy.mockResolvedValue({
      state: "failed", reason: "", tag: "2026.10.1", current_line: "", error: "boom",
      steps: [{ key: "a", label: "Fetch", status: "done", started_at: null }],
    });
    const user = userEvent.setup();
    renderPage();
    await user.click(await screen.findByRole("button", { name: /^install$/i }));
    await user.click(screen.getByRole("button", { name: /confirm install/i }));
    expect(await screen.findByTestId("progress-headline")).toHaveTextContent(/^Upgrade to 2026\.10\.1 failed$/);
    expect(screen.getByText(/Install failed: boom/)).toBeInTheDocument();
  });

  it("the notes pill counts top-level bullet lines and draws no category chip", async () => {
    updateCheckSpy.mockResolvedValue(payload({
      notes: {
        running: {
          tag: "2026.09.1", state: "present",
          body: "## What changed\n- #1201 gbfleet: teardown\n- #1202 UI: settings\n  - nested, not counted\n- #1203 infra: CI\nTrailing prose.\n",
        },
        latest: null,
      },
    }));
    renderPage();
    const pill = await screen.findByTestId("changes-pill");
    expect(pill).toHaveTextContent("3 changes");
    // A collapsed row: the pill sits in the summary, and the row starts closed.
    expect(pill.closest("summary")).not.toBeNull();
    expect(pill.closest("details")).not.toHaveAttribute("open");
    for (const chip of ["gbfleet", "UI", "infra", "API"]) {
      expect(screen.queryByText(chip, { exact: true })).toBeNull();
    }
  });

  it("no pill when the notes are empty or unknown", async () => {
    updateCheckSpy.mockResolvedValue(payload());
    renderPage();
    expect(await screen.findByText(/no notes on this release/i)).toBeInTheDocument();
    expect(screen.queryByTestId("changes-pill")).toBeNull();
  });

  beforeEach(() => {
    updateApplySpy.mockClear();
    Object.assign(installTiming, { pollMs: 1, requestMs: 50, budgetMs: 2000, reloadMs: 1 });
  });

  const runningSteps = [
    { key: "pin", label: "Pinning the deploy worktree", status: "done", started_at: null },
    { key: "build", label: "Building the images", status: "active", started_at: null },
  ];
  const running = (tag = "2026.10.1") => ({
    state: "running", reason: "", tag, current_line: "", error: "", steps: runningSteps,
  });

  it("resumes a running install on mount: progress for the tag, no enabled Install, no click", async () => {
    updateCheckSpy.mockResolvedValue(available());
    progressSpy.mockResolvedValue(running());
    renderPage();
    expect(await screen.findByTestId("progress-headline")).toHaveTextContent("Upgrading to 2026.10.1");
    expect(screen.queryByRole("button", { name: /^install$/i })).toBeNull();
    expect(updateApplySpy).not.toHaveBeenCalled();
  });

  it("a resumed install names its own tag in the phase line, not the latest available", async () => {
    updateCheckSpy.mockResolvedValue(available());
    progressSpy.mockResolvedValue(running("2026.09.9"));
    renderPage();
    expect(await screen.findByTestId("progress-headline")).toHaveTextContent("Upgrading to 2026.09.9");
    expect(screen.getByTestId("phase-copy")).toHaveTextContent("Building 2026.09.9.");
    expect(screen.getByTestId("phase-copy")).not.toHaveTextContent("2026.10.1");
  });

  it("a failed install returns the header to Check for updates and Install", async () => {
    const user = userEvent.setup();
    updateCheckSpy.mockResolvedValue(available());
    progressSpy.mockResolvedValueOnce({ state: "idle", reason: "", tag: "", steps: [], current_line: "", error: "" });
    progressSpy.mockResolvedValue({
      state: "failed", reason: "", tag: "2026.10.1", current_line: "", error: "boom",
      steps: [{ key: "verify", label: "Verifying release identity", status: "failed", started_at: null }],
    });
    renderPage();
    await user.click(await screen.findByRole("button", { name: /^install$/i }));
    await user.click(screen.getByRole("button", { name: /confirm install/i }));
    const msg = await screen.findByText(/install failed: boom/i);
    expect(msg.className).toContain("text-danger");
    expect(screen.getByRole("button", { name: /check for updates/i })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /^install$/i })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /confirm install/i })).toBeNull();
    expect(screen.getByRole("list", { name: /install steps/i }).querySelector("li")?.className).toContain("text-danger");
  });

  it("an automatic install shows the progress in the Status card and no Install", async () => {
    updateCheckSpy.mockResolvedValue(available());
    vi.mocked(api.autoUpdate).mockResolvedValueOnce({
      available: true, unavailable_reason: "", enabled: true, timezone: "UTC", timezone_set: true,
      host_now: "2026-11-02T10:30:00+00:00",
      check: { days: [0], start: "02:00", end: "05:00" }, install: { days: [0], start: "02:00", end: "05:00" },
      next_check_at: null, last_check_at: null, last_check_state: "", last_auto_install_tag: "2026.10.1",
      last_auto_install_at: "2026-11-02T10:00:00+00:00", last_auto_install_outcome: "started",
      last_auto_install_error: "",
    });
    progressSpy.mockResolvedValue(running());
    renderPage();
    const headline = await screen.findByTestId("progress-headline");
    expect(headline.closest("div.rounded-\\[13px\\]")?.textContent).toContain("Status");
    expect(screen.queryByRole("button", { name: /^install$/i })).toBeNull();
  });

  it("hosted shows no Install and no deploy.sh hint", async () => {
    // This block's default progress is a running install, which would resume on mount and
    // hide the Check button; the test is about hosted, so nothing is running.
    progressSpy.mockResolvedValue({ state: "idle", reason: "", tag: "", steps: [], current_line: "", error: "" });
    updateCheckSpy.mockResolvedValue({ ...available(), hosted: true });
    renderPage(settingsPath("deployment/updates"), true);
    await screen.findByRole("button", { name: /check for updates/i });
    expect(screen.queryByRole("button", { name: /install/i })).toBeNull();
    expect(screen.queryByText(/scripts\/deploy\.sh/)).toBeNull();
  });

  it("a finished install keeps the steps, all done, as Upgrade complete, with no Install or Check", async () => {
    const user = userEvent.setup();
    const reload = vi.spyOn(pageReload, "run").mockImplementation(() => {});
    installTiming.reloadMs = 60_000;
    updateCheckSpy.mockResolvedValue(available());
    progressSpy.mockResolvedValueOnce({ state: "idle", reason: "", tag: "", steps: [], current_line: "", error: "" });
    progressSpy.mockResolvedValue(running());
    healthSpy.mockResolvedValue({ status: "ok", version: "2026.10.1", git_sha: "n" });
    renderPage();
    await user.click(await screen.findByRole("button", { name: /^install$/i }));
    await user.click(screen.getByRole("button", { name: /confirm install/i }));
    expect(await screen.findByText(/^Upgrade complete$/)).toBeInTheDocument();
    for (const li of screen.getByRole("list", { name: /install steps/i }).querySelectorAll("li")) {
      expect(li).toHaveAttribute("data-status", "done");
    }
    expect(screen.queryByRole("button", { name: /install/i })).toBeNull();
    expect(screen.queryByRole("button", { name: /check for updates/i })).toBeNull();
    expect(reload).not.toHaveBeenCalled();
  });

  it("index.css defines --color-danger as an alias of the blocked status colour", () => {
    const css = readFileSync("src/index.css", "utf8");
    expect(css).toMatch(/--color-danger:\s*var\(--color-st-blocked\)/);
  });
});

describe("Updates panel layout pass (design audit)", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    vi.restoreAllMocks();
    localStorage.setItem("gb_last_project_tag", "APP");
    projectsSpy.mockResolvedValue([proj]);
    updateApplySpy.mockReset().mockImplementation(async (tag: string) => ({ ok: true, started: true, tag }));
    updateCheckSpy.mockReset();
    progressSpy.mockReset();
    healthSpy.mockReset();
    healthSpy.mockResolvedValue({ status: "ok", version: "2026.09.1", git_sha: "x" });
    progressSpy.mockResolvedValue({ state: "idle", reason: "no log", tag: "", steps: [], current_line: "", error: "" });
    running = {
      state: "running", reason: "", tag: "2026.10.1", current_line: "pulling layers",
      error: "",
      steps: [
        { key: "a", label: "Fetching", status: "done", started_at: 1 },
        { key: "b", label: "Building the images", status: "active", started_at: Math.floor(Date.now() / 1000) - 5 },
      ],
    };
    Object.assign(installTiming, { pollMs: 5, requestMs: 50, budgetMs: 5000, reloadMs: 5 });
  });

  let running: unknown;
  async function startInstall() {
    const user = userEvent.setup();
    updateCheckSpy.mockResolvedValue(available());
    renderPage();
    await user.click(await screen.findByRole("button", { name: /^install$/i }));
    progressSpy.mockResolvedValue(running);
    await user.click(screen.getByRole("button", { name: /confirm install 2026\.10\.1/i }));
  }

  it("while installing the Status card has no heading and no Check button", async () => {
    await startInstall();
    expect(await screen.findByRole("list", { name: /install steps/i })).toBeInTheDocument();
    expect(screen.queryByText(/update available/i)).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /check for updates/i })).not.toBeInTheDocument();
    expect(screen.getByText("pulling layers")).toBeInTheDocument();
    expect(screen.getByText(/how this box is deployed/i)).toBeInTheDocument();
  });

  it("the active step's elapsed time is its own right-aligned element after the label", async () => {
    await startInstall();
    const timer = await screen.findByTestId("step-timer");
    expect(timer.className).toMatch(/ml-auto/);
    expect(timer.className).toMatch(/font-mono/);
    const row = timer.closest("li")!;
    expect(row.textContent).toMatch(/Building the images.*\d+:\d\d/);
  });

  it("an available update puts the tag in the heading", async () => {
    updateCheckSpy.mockResolvedValue(available());
    renderPage();
    const h = await screen.findByRole("heading", { name: /update available/i });
    expect(h).toHaveTextContent("2026.10.1");
    expect(screen.getByText(/Running/)).toBeInTheDocument();
    expect(screen.queryByText(/install rebuilds this box/i)).not.toBeInTheDocument();
  });

  it("a failed check with unknown notes shows no release notes block", async () => {
    updateCheckSpy.mockResolvedValue(payload({
      state: "unknown", latest: null, feed_error: "GitHub did not answer within 5s",
      notes: { running: { tag: "2026.09.1", state: "unknown", body: "" }, latest: null },
    } as Partial<UpdateCheck>));
    renderPage();
    expect(await screen.findByText("Could not check")).toBeInTheDocument();
    expect(screen.getByText("GitHub did not answer within 5s.")).toBeInTheDocument();
    expect(screen.getByText("Install is disabled until a check succeeds.")).toBeInTheDocument();
    expect(screen.queryByText(/release notes/i)).not.toBeInTheDocument();
  });

  it("a self-hosted box without apply keeps the scripts/deploy.sh <tag> fallback hint", async () => {
    updateCheckSpy.mockResolvedValue({ ...available(), apply: false, via: "" });
    renderPage();
    expect(await screen.findByText(/Until then: scripts\/deploy\.sh 2026\.10\.1/)).toBeInTheDocument();
  });

  it("with Install available, the long install hint under the buttons is dropped", async () => {
    updateCheckSpy.mockResolvedValue(available());
    renderPage();
    await screen.findByRole("button", { name: /^install$/i });
    expect(screen.queryByText(/Install rebuilds this box/)).toBeNull();
    expect(screen.queryByText(/scripts\/deploy\.sh/)).toBeNull();
  });

  it("Install and Confirm are one group: Check and Cancel stay mounted, inert flips, data-confirm follows", async () => {
    updateCheckSpy.mockResolvedValue(available());
    const user = userEvent.setup();
    renderPage();
    await user.click(await screen.findByRole("button", { name: /^install$/i }));
    const group = screen.getByTestId("install-group");
    expect(group).toHaveAttribute("data-confirm", "true");
    expect(screen.getByTestId("slot-check")).toHaveAttribute("inert");
    expect(screen.getByTestId("slot-cancel")).not.toHaveAttribute("inert");
    expect(screen.getByRole("button", { name: /confirm install 2026\.10\.1/i })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Check for updates" })).toBeNull();
    await user.click(screen.getByRole("button", { name: "Cancel" }));
    expect(group).toHaveAttribute("data-confirm", "false");
    expect(screen.getByTestId("slot-check")).not.toHaveAttribute("inert");
    expect(screen.getByTestId("slot-cancel")).toHaveAttribute("inert");
    expect(screen.getByRole("button", { name: "Check for updates" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /^install$/i })).toBeInTheDocument();
  });

  it("the active step segment has a sweep child; running dot pulses, complete dot is st-done", async () => {
    updateCheckSpy.mockResolvedValue(available());
    const mk = (statuses: string[]) => ({
      state: "running", reason: "", tag: "2026.10.1", current_line: "", error: "",
      steps: statuses.map((status, i) => ({ key: `s${i}`, label: `Step ${i + 1}`, status, started_at: null })),
    });
    progressSpy.mockResolvedValue(mk(["done", "active", "pending"]));
    const user = userEvent.setup();
    progressSpy.mockResolvedValueOnce({ state: "idle", reason: "", tag: "", steps: [], current_line: "", error: "" });
    renderPage();
    await user.click(await screen.findByRole("button", { name: /^install$/i }));
    await user.click(screen.getByRole("button", { name: /confirm install/i }));
    await screen.findByText(/step 2 of 3/);
    const segs = screen.getAllByTestId("step-segment");
    expect(segs[1].querySelector(".u-sweep")).not.toBeNull();
    expect(segs[0].querySelector(".u-sweep")).toBeNull();
    expect(segs[0].className).toContain("bg-accent");
    expect(segs[2].className).toContain("bg-surface-4");
    const dot = screen.getByTestId("progress-headline").firstElementChild!;
    expect(dot.className).toContain("u-dot-live");
  });

  it("index.css defines the sweep and pulse keyframes and disables both for reduced motion", () => {
    const css = readFileSync("src/index.css", "utf8");
    expect(css).toMatch(/@keyframes u-sweep/);
    expect(css).toMatch(/@keyframes u-pulse/);
    const rm = css.slice(css.indexOf("@media (prefers-reduced-motion: reduce) {\n  .u-slot"));
    expect(rm).toMatch(/\.u-dot-live,\s*\.u-sweep\s*\{\s*animation:\s*none/);
  });

  const SINCE = [
    "Intro line.", "", "**Since 2026.11.4**",
    "- #1301 Re-delegate a bounced item without restarting the drain (GRPH-1315)",
    "- #1302 Bump graphban-fleet to 0.18.10",
    "- WIP: salvaged by gbfleet (claude) items=GRPH-1329 (#1307)", "",
    "**Verify**", "Run it.", "",
  ].join("\n");

  function withNotes(body: string) {
    updateCheckSpy.mockResolvedValue(payload({
      notes: { running: { tag: "2026.11.5", state: "present", body }, latest: null },
    }));
  }

  it("Since list renders as pill rows, not markdown bullets", async () => {
    withNotes(SINCE);
    renderPage();
    await screen.findByText("This release");
    const rows = screen.getAllByTestId("release-call");
    const r1 = rows.find((r) => r.getAttribute("data-pr") === "#1301")!;
    const pills1 = Array.from(r1.querySelectorAll(".u-pill")).map((e) => e.textContent);
    expect(pills1).toEqual(["#1301", "GRPH-1315"]);
    expect(r1).toHaveTextContent("Re-delegate a bounced item without restarting the drain");
    const r2 = rows.find((r) => r.getAttribute("data-pr") === "#1302")!;
    expect(Array.from(r2.querySelectorAll(".u-pill")).map((e) => e.textContent)).toEqual(["#1302"]);
    expect(document.querySelectorAll("li:not([data-testid=release-call])").length).toBe(0);
    expect(screen.getByText("Intro line.")).toBeInTheDocument();
    expect(screen.getByText("Run it.")).toBeInTheDocument();
    expect(screen.getByTestId("changes-pill")).toHaveTextContent("3 changes");
  });

  it("the real 2026.11.5 salvaged line drops the dangling items= once its id is a pill", async () => {
    withNotes([
      "**Since 2026.11.4**",
      "- #1307 WIP: salvaged by gbfleet (claude) items=GRPH-1329",
      "",
    ].join("\n"));
    renderPage();
    await screen.findByText("This release");
    const row = screen.getAllByTestId("release-call").find((r) => r.getAttribute("data-pr") === "#1307")!;
    expect(Array.from(row.querySelectorAll(".u-pill")).map((e) => e.textContent)).toEqual(["#1307", "GRPH-1329"]);
    const title = row.querySelector(".u-call-title")!;
    expect(title.textContent).toBe("WIP: salvaged by gbfleet (claude)");
    expect(title.textContent).not.toMatch(/items=/);
    expect(title).toHaveClass("text-faint");
  });

  it("notes with no Since list render as markdown as before", async () => {
    withNotes("Plain.\n\n- one\n- two\n");
    renderPage();
    await screen.findByText("This release");
    expect(screen.queryAllByTestId("release-call")).toHaveLength(0);
    expect(screen.getAllByRole("listitem").map((l) => l.textContent)).toEqual(["one", "two"]);
  });

  it("a view change fades the old card content out for 220ms before the new content mounts", async () => {
    const mm = vi.fn((q: string) => ({
      matches: false, media: q, onchange: null,
      addEventListener: vi.fn(), removeEventListener: vi.fn(),
      addListener: vi.fn(), removeListener: vi.fn(), dispatchEvent: vi.fn(),
    }));
    const prev = window.matchMedia;
    Object.defineProperty(window, "matchMedia", { configurable: true, writable: true, value: mm });
    try {
      await startInstall();
      // Straight after Confirm: the plain content is still mounted, fading and inert; no steps yet.
      const body = await screen.findByTestId("card-body");
      await waitFor(() => expect(body).toHaveClass("u-fading"));
      expect(body).toHaveAttribute("data-view", "plain");
      expect(body).toHaveAttribute("inert");
      expect(body.querySelector("[data-testid='install-group']")).not.toBeNull();
      expect(screen.queryByRole("list", { name: /install steps/i, hidden: true })).toBeNull();
      // After the fade the installing content mounts and the fading class is gone.
      expect(await screen.findByRole("list", { name: /install steps/i })).toBeInTheDocument();
      const after = screen.getByTestId("card-body");
      expect(after).not.toHaveClass("u-fading");
      expect(after).toHaveAttribute("data-view", "installing");
      expect(after).not.toHaveAttribute("inert");
      expect(after.querySelector("[data-testid='install-group']")).toBeNull();
    } finally {
      Object.defineProperty(window, "matchMedia", { configurable: true, writable: true, value: prev });
    }
  });

  it("index.css fades .u-swap out on .u-fading and drops the transition for reduced motion", () => {
    const css = readFileSync("src/index.css", "utf8");
    expect(css).toMatch(/\.u-swap\s*\{[^}]*opacity \.22s/);
    expect(css).toMatch(/\.u-swap\.u-fading\s*\{\s*opacity:\s*0/);
    const reduced = css.slice(css.indexOf("@media (prefers-reduced-motion: reduce)"));
    expect(reduced).toMatch(/\.u-swap[,\s]/);
  });
});
