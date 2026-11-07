/**
 * The quick-start dialog (GRPH-1361, GRPH-P67 §4).
 *
 * Mounted through the real AppFrame, TopBar and dialog, against a mocked API that behaves
 * like the server: the setup checklist is the only source of a step's mark, and a confirm
 * returns the checklist the server would. Shell parts the dialog does not touch are stubbed.
 *
 * The claims with teeth:
 * - a new user (onboarding null) lands in the dialog; a backfilled user does not;
 * - clicking Next never turns a step ✓ — the rail redraws from what setup returns;
 * - a step the server reports `unknown` is never drawn ✓ (the sabotage);
 * - the chip reopens on the first unfinished step and is gone after Finish;
 * - hosted never shows step ⓪.
 */
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { AppFrame } from "@/components/shell/AppFrame";
import type { Onboarding, ProjectSetup, SetupStatus } from "@/lib/types";

import {
  chipCount,
  deriveMarks,
  firstUnfinished,
  inferWhere,
  stepsFor,
  storedWhere,
} from "./quickStartProgress";

// ── A server, in miniature ───────────────────────────────────────────────────────────

const srv = vi.hoisted(() => ({
  hosted: false,
  onboarding: null as Onboarding | null,
  projects: [] as { id: string; tag: string; name: string; accent: string }[],
  statuses: {} as Record<number, string>,
  details: {} as Record<number, string>,
  reasons: {} as Record<number, string>,
  offline: false,
  // When true, confirm stores nothing: proves the rail reads the server, not the click.
  confirmIsNoop: false,
  confirms: [] as unknown[],
  marks: [] as string[],
  minted: [] as string[],
  setupFails: false,
}));

function setupBody(projectId: string): ProjectSetup {
  return {
    project_id: projectId,
    offline: srv.offline,
    onboarding: srv.onboarding,
    steps: [0, 1, 2, 3, 4, 5].map((n) => ({
      step: n,
      name: `step ${n}`,
      status: (srv.statuses[n] ?? "pending") as SetupStatus,
      detail: srv.details[n] ?? (n >= 2 && n <= 4 ? "0 thing(s)." : "Not answered yet."),
      how: "",
      ...(srv.reasons[n] ? { reason: srv.reasons[n] } : {}),
    })),
  };
}

vi.mock("@/lib/api", () => ({
  setActiveProjectId: vi.fn(),
  api: {
    config: vi.fn(async () => ({ hosted_mode: srv.hosted, signup_mode: "open" })),
    orgs: vi.fn(async () => [{ id: "o1", name: "Org" }]),
    projects: vi.fn(async () => srv.projects),
    onboarding: vi.fn(async () => ({ onboarding: srv.onboarding })),
    markOnboarding: vi.fn(async (mark: string) => {
      srv.marks.push(mark);
      srv.onboarding = { ...(srv.onboarding ?? {}), [`${mark}_at`]: "2026-11-06T00:00:00Z" };
      return { onboarding: srv.onboarding };
    }),
    projectSetup: vi.fn(async (id: string) => {
      if (srv.setupFails) throw new Error("503 setup down");
      return setupBody(id);
    }),
    createApiKey: vi.fn(async (_n: string, _p: string, _e: unknown, _s: unknown, _t: unknown, origin: string) => {
      srv.minted.push(origin);
      return { id: "k1", name: "k", prefix: "gb_sk_ab12", plaintext: "gb_sk_secret", scopes: [], revoked: false };
    }),
    confirmSetup: vi.fn(async (id: string, body: { decision: string; where?: string; address?: string }) => {
      srv.confirms.push(body);
      if (!srv.confirmIsNoop) {
        if (body.decision === "workspace") srv.statuses[1] = "done";
        if (body.decision === "offline") srv.offline = true;
        if (body.decision === "server") {
          srv.statuses[0] = "done";
          srv.details[0] = body.where === "dedicated_server" ? `dedicated_server at ${body.address}` : "this_computer";
        }
      }
      return setupBody(id);
    }),
    credentials: vi.fn(async () => ({ credentials: [] })),
    createProject: vi.fn(async (body: { name: string }) => {
      const p = { id: "p-new", tag: "NEW", name: body.name, accent: "#c6f24e" };
      srv.projects = [p];
      return p;
    }),
    tagSuggestion: vi.fn(async () => ({ tag: "NEW" })),
    tagCheck: vi.fn(async (tag: string) => ({ tag, available: true, reason: "" })),
    apiKeys: vi.fn(async () => []),
    mcpTools: vi.fn(async () => ({ live: 0 })),
  },
}));

vi.mock("@/features/auth/AuthContext", () => ({
  useAuth: () => ({ user: null, logout: vi.fn() }),
}));
vi.mock("@/components/shell/LeftNav", () => ({
  LeftNav: () => <nav aria-label="stub nav" />,
  readSidebarCollapsed: () => false,
}));
vi.mock("@/components/shell/AgentSidebar", () => ({ AgentSidebar: () => null }));
vi.mock("@/components/shell/CommandPalette", () => ({ CommandPalette: () => null }));
vi.mock("@/components/shell/ProjectBar", () => ({ ProjectBar: () => null }));
vi.mock("@/features/docs/DocsReader", () => ({ DocsReader: () => null }));
vi.mock("@/features/reports/ReportIssueDialog", () => ({ ReportIssueDialog: () => null }));

function renderFrame() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={qc}>
      <MemoryRouter initialEntries={["/tracker"]}>
        <AppFrame />
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

const CORE = { id: "core", tag: "CORE", name: "Core Platform", accent: "#c6f24e" };

function rail() {
  return screen.getByRole("list", { name: "Setup steps" });
}
function railItem(step: string) {
  return rail().querySelector(`[data-step="${step}"]`) as HTMLElement;
}

beforeEach(() => {
  srv.hosted = false;
  srv.onboarding = null;
  srv.projects = [];
  srv.statuses = {};
  srv.details = {};
  srv.reasons = {};
  srv.offline = false;
  srv.confirmIsNoop = false;
  srv.confirms = [];
  srv.marks = [];
  srv.minted = [];
  srv.setupFails = false;
});

// ── Trigger (D6) ─────────────────────────────────────────────────────────────────────

describe("who lands in the dialog", () => {
  it("a new user with no project lands in the quick start, not on the create form alone", async () => {
    renderFrame();
    const dialog = await screen.findByRole("dialog");
    expect(within(dialog).getByText("Quick start")).toBeInTheDocument();
    expect(within(dialog).getByRole("heading", { name: "Where is Graphban running?" })).toBeInTheDocument();
  });

  it("a backfilled user does not: no dialog and no chip", async () => {
    srv.onboarding = { quickstart_finished_at: "2026-11-01T00:00:00Z" };
    srv.projects = [CORE];
    renderFrame();
    expect(await screen.findByLabelText("Jump to")).toBeInTheDocument();
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /Finish setup/ })).not.toBeInTheDocument();
  });

  it("a user who chose Finish later is not ambushed again, but keeps the chip", async () => {
    srv.onboarding = { quickstart_dismissed_at: "2026-11-01T00:00:00Z" };
    srv.projects = [CORE];
    renderFrame();
    expect(await screen.findByRole("button", { name: /Finish setup/ })).toBeInTheDocument();
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
  });
});

// ── Marks are server state ───────────────────────────────────────────────────────────

describe("every mark comes from GET …/setup", () => {
  it("a new user walks ⓪ → ① creating the project, and the answers are stored by confirm", async () => {
    const user = userEvent.setup();
    renderFrame();
    await screen.findByRole("dialog");
    // jsdom serves from localhost, so ⓪ pre-selects "this computer" and names its evidence.
    expect(screen.getByRole("radio", { name: /On this computer/ })).toBeChecked();
    expect(screen.getByText(/Pre-selected: you opened Graphban on localhost/)).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Next →" }));

    // No project yet: ⓪ is held, not stored, so the rail must not claim it.
    expect(await screen.findByRole("heading", { name: "Create your project" })).toBeInTheDocument();
    expect(railItem("where").dataset.status).toBe("pending");

    await user.type(within(screen.getByRole("dialog")).getByLabelText("Project name"), "Core Platform");
    const next = screen.getByRole("button", { name: "Next →" });
    expect(next).toBeDisabled();
    expect(screen.getByText("Tick the confirmation first.")).toBeInTheDocument();
    await user.click(screen.getByRole("checkbox", { name: /This is the workspace I want agents writing to/ }));
    await user.click(next);

    expect(await screen.findByRole("heading", { name: "Pick a model" })).toBeInTheDocument();
    expect(srv.confirms).toEqual([
      { decision: "workspace" },
      { decision: "server", where: "this_computer", address: "" },
    ]);
    await waitFor(() => expect(railItem("where").dataset.status).toBe("done"));
    expect(railItem("project").dataset.status).toBe("done");
  });

  it("clicking Next does not turn a step ✓ when the server did not store it", async () => {
    srv.projects = [CORE];
    srv.confirmIsNoop = true;
    const user = userEvent.setup();
    renderFrame();
    await screen.findByRole("heading", { name: "Where is Graphban running?" });
    await user.click(screen.getByRole("button", { name: "Next →" }));
    await screen.findByRole("heading", { name: "Confirm your project" });
    expect(srv.confirms).toHaveLength(1);
    expect(railItem("where").dataset.status).toBe("pending");
    expect(within(railItem("where")).queryByText("✓")).not.toBeInTheDocument();
  });

  it("re-opening shows real progress", async () => {
    srv.projects = [CORE];
    srv.onboarding = { quickstart_dismissed_at: "2026-11-01T00:00:00Z" };
    srv.statuses = { 0: "done", 1: "done" };
    srv.details = { 0: "this_computer" };
    srv.offline = true;
    const user = userEvent.setup();
    renderFrame();
    const chip = await screen.findByRole("button", { name: /Finish setup/ });
    await waitFor(() => expect(chip).toHaveTextContent("Finish setup 2/4"));
    await user.click(chip);
    // First unfinished: ③ Connect.
    expect(await screen.findByRole("heading", { name: "Connect your agent" })).toBeInTheDocument();
    expect(railItem("where").dataset.status).toBe("done");
    expect(railItem("project").dataset.status).toBe("done");
    expect(railItem("model").dataset.status).toBe("done");
    expect(railItem("connect").dataset.status).toBe("pending");
    expect(railItem("connect")).toHaveAttribute("aria-current", "step");
  });

  it("SABOTAGE: a step the server reports unknown is never drawn ✓", async () => {
    srv.projects = [CORE];
    srv.statuses = { 0: "done", 1: "unknown", 2: "unknown" };
    srv.reasons = { 1: "could not check: OperationalError", 2: "could not check: Timeout" };
    renderFrame();
    await screen.findByRole("dialog");
    await waitFor(() => expect(railItem("project").dataset.status).toBe("unknown"));
    expect(within(railItem("project")).queryByText("✓")).not.toBeInTheDocument();
    expect(within(railItem("project")).getByText("?")).toBeInTheDocument();
    expect(railItem("first_job").dataset.status).toBe("unknown");
    // The reason is shown, not swallowed.
    expect(screen.getByText(/OperationalError/)).toBeInTheDocument();
    expect(railItem("where").dataset.status).toBe("done");
  });
});

// ── Chip and Finish (D8) ─────────────────────────────────────────────────────────────

describe("the chip and Finish", () => {
  it("Esc is Finish later: closes, records dismissed, and leaves the chip", async () => {
    srv.projects = [CORE];
    const user = userEvent.setup();
    renderFrame();
    await screen.findByRole("dialog");
    await user.keyboard("{Escape}");
    await waitFor(() => expect(screen.queryByRole("dialog")).not.toBeInTheDocument());
    expect(srv.marks).toEqual(["quickstart_dismissed"]);
    expect(screen.getByRole("button", { name: /Finish setup/ })).toBeInTheDocument();
  });

  it("the chip reopens on the first unfinished step, and disappears after Finish", async () => {
    srv.projects = [CORE];
    srv.onboarding = { quickstart_dismissed_at: "2026-11-01T00:00:00Z" };
    srv.statuses = { 0: "done" };
    srv.details = { 0: "this_computer" };
    const user = userEvent.setup();
    renderFrame();
    const chip = await screen.findByRole("button", { name: /Finish setup/ });
    await user.click(chip);
    expect(await screen.findByRole("heading", { name: "Confirm your project" })).toBeInTheDocument();

    // Walk to Finish without doing anything else: Finish is always reachable.
    await user.click(screen.getByRole("checkbox", { name: /workspace I want agents/ }));
    await user.click(screen.getByRole("button", { name: "Next →" }));
    await screen.findByRole("heading", { name: "Pick a model" });
    await user.click(screen.getByRole("radio", { name: /Local · Ollama/ }));
    await user.click(screen.getByRole("button", { name: "Next →" }));
    await screen.findByRole("heading", { name: "Connect your agent" });
    await user.click(screen.getByRole("button", { name: "Next →" }));
    await screen.findByRole("heading", { name: "Give it a first job" });
    await user.click(screen.getByRole("button", { name: "Finish" }));

    const finish = await screen.findByRole("heading", { name: "You're set up" });
    const open = within(finish.parentElement as HTMLElement).getByRole("list", { name: "Steps still open" });
    expect(within(open).getByText(/Model — pending/)).toBeInTheDocument();
    expect(within(open).getByText(/First job — pending/)).toBeInTheDocument();
    // Walking past ③ proved nothing: Finish says so, naming the route.
    expect(within(open).getByText("Agent connection unverified — gban CLI")).toBeInTheDocument();

    // With a project the frame offers the app tour (GRPH-1363); skipping it still finishes.
    expect(screen.getByRole("button", { name: "Take the 2-minute tour" })).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Skip for now" }));
    await waitFor(() => expect(screen.queryByRole("dialog")).not.toBeInTheDocument());
    expect(srv.marks).toContain("quickstart_finished");
    await waitFor(() => expect(screen.queryByRole("button", { name: /Finish setup/ })).not.toBeInTheDocument());
  });
});

describe("hosted", () => {
  it("never shows step ⓪", async () => {
    srv.hosted = true;
    srv.projects = [CORE];
    renderFrame();
    expect(await screen.findByRole("heading", { name: "Confirm your project" })).toBeInTheDocument();
    expect(rail().querySelector('[data-step="where"]')).toBeNull();
    expect(screen.queryByText("Where is Graphban running?")).not.toBeInTheDocument();
  });
});

describe("step ⓪ on a dedicated server", () => {
  it("stores the address and says only the browser was proven", async () => {
    srv.projects = [CORE];
    const user = userEvent.setup();
    renderFrame();
    await screen.findByRole("heading", { name: "Where is Graphban running?" });
    await user.click(screen.getByRole("radio", { name: /On a dedicated server/ }));
    const addr = screen.getByLabelText("Server address");
    await user.type(addr, "http://ubuntu-srv:8080/");
    expect(screen.getByText(/Unknown — not checked/)).toBeInTheDocument();
    expect(screen.getByText(/proves the browser can reach it, not your dev machine/)).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Next →" }));
    await screen.findByRole("heading", { name: "Confirm your project" });
    expect(srv.confirms).toEqual([{ decision: "server", where: "dedicated_server", address: "http://ubuntu-srv:8080" }]);
  });
});

// ── The derivation on its own ────────────────────────────────────────────────────────

describe("③ Connect your agent", () => {
  function atConnect() {
    srv.projects = [CORE];
    srv.onboarding = { quickstart_dismissed_at: "2026-11-01T00:00:00Z" };
    srv.statuses = { 0: "done", 1: "done" };
    srv.details = { 0: "this_computer" };
    srv.offline = true;
  }
  async function open(user: ReturnType<typeof userEvent.setup>) {
    renderFrame();
    await user.click(await screen.findByRole("button", { name: /Finish setup/ }));
    await screen.findByRole("heading", { name: "Connect your agent" });
  }

  it("the gban route shows three commands, Next is enabled, and ③ stays PENDING through First job", async () => {
    atConnect();
    const user = userEvent.setup();
    await open(user);
    expect(screen.getByText("uv tool install graphban-cli")).toBeInTheDocument();
    expect(screen.getByText(/gban setup --project core/)).toBeInTheDocument();
    expect(screen.getByText(/then restart your agent/)).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Next →" }));
    await screen.findByRole("heading", { name: "Give it a first job" });
    expect(railItem("connect").dataset.status).toBe("pending");
    expect(within(railItem("connect")).queryByText("✓")).not.toBeInTheDocument();
  });

  it("an agent the CLI does not configure is switched to paste-a-key", async () => {
    atConnect();
    const user = userEvent.setup();
    await open(user);
    await user.selectOptions(screen.getByLabelText("Which agent?"), "cursor");
    expect(screen.getByRole("radio", { name: "gban CLI" })).toBeDisabled();
    expect(screen.getByRole("radio", { name: "Paste a key" })).toHaveAttribute("aria-checked", "true");
    expect(screen.queryByText("uv tool install graphban-cli")).not.toBeInTheDocument();
  });

  it("paste route mints a quickstart key, polls, and ticks only when the server says so", async () => {
    atConnect();
    const user = userEvent.setup();
    await open(user);
    await user.click(screen.getByRole("radio", { name: "Paste a key" }));
    await user.click(screen.getByRole("button", { name: /Create a key for Claude Code/ }));
    expect(await screen.findByText("Waiting for your agent's first call…")).toBeInTheDocument();
    expect(srv.minted).toEqual(["quickstart"]);
    expect(railItem("connect").dataset.status).toBe("pending");
    srv.statuses[5] = "done";
    srv.details[5] = "First call from key 'alex-mbp' at 13:42.";
    await waitFor(() => expect(railItem("connect").dataset.status).toBe("done"), { timeout: 5000 });
    expect(screen.getByText(/First call from key 'alex-mbp'/)).toBeInTheDocument();
  }, 10000);

  it("SABOTAGE: an API that errors mid-poll shows ③ unknown, not a stale ✓", async () => {
    atConnect();
    srv.statuses[5] = "done";
    srv.details[5] = "First call from key 'alex-mbp'.";
    const user = userEvent.setup();
    renderFrame();
    await user.click(await screen.findByRole("button", { name: /Finish setup/ }));
    await waitFor(() => expect(railItem("connect").dataset.status).toBe("done"));
    srv.setupFails = true;
    await waitFor(() => expect(railItem("connect").dataset.status).toBe("unknown"), { timeout: 5000 });
    expect(within(railItem("connect")).queryByText("✓")).not.toBeInTheDocument();
  }, 10000);
});

describe("deriveMarks", () => {
  const base = (over: Partial<ProjectSetup> = {}): ProjectSetup => ({
    project_id: "p",
    offline: false,
    onboarding: null,
    steps: [0, 1, 2, 3, 4, 5].map((n) => ({ step: n, name: "", status: "pending", detail: "0 x.", how: "" })),
    ...over,
  });

  it("a failed setup read is unknown everywhere, never pending or done", () => {
    const m = deriveMarks({ projectId: "p", setupError: new Error("503") });
    for (const s of Object.values(m)) {
      expect(s.status).toBe("unknown");
      expect(s.reason).toMatch(/503/);
    }
  });

  it("an unexpected status is not a ✓", () => {
    const setup = base();
    (setup.steps[1] as { status: string }).status = "action_required";
    expect(deriveMarks({ projectId: "p", setup }).project.status).toBe("pending");
  });

  it("a missing server step is unknown, not pending", () => {
    const setup = base({ steps: [] });
    expect(deriveMarks({ projectId: "p", setup }).where.status).toBe("unknown");
  });

  it("model: offline decision or a valid chat credential for this project; an unverified one is unknown", () => {
    expect(deriveMarks({ projectId: "p", setup: base({ offline: true }) }).model.status).toBe("done");
    const cred = {
      id: "c", kind: "anthropic", label: "Claude", base_url: "", model: "", key_set: true,
      state: "pending_validation" as const, last_error: "", serves: "chat", used_by: ["p"],
      falling_back: [], is_default: false, is_fallback: false, is_embed: false, is_decider: false,
    };
    expect(deriveMarks({ projectId: "p", setup: base(), credentials: [cred] }).model.status).toBe("unknown");
    expect(
      deriveMarks({ projectId: "p", setup: base(), credentials: [{ ...cred, state: "valid" }] }).model.status,
    ).toBe("done");
    expect(deriveMarks({ projectId: "p", setup: base(), credentials: [] }).model.status).toBe("pending");
  });

  it("first job is done only when all three counts are", () => {
    const setup = base();
    setup.steps[2].status = "done";
    setup.steps[3].status = "done";
    expect(deriveMarks({ projectId: "p", setup }).first_job.status).toBe("pending");
    setup.steps[4].status = "done";
    expect(deriveMarks({ projectId: "p", setup }).first_job.status).toBe("done");
  });

  it("chip counts numbered steps only, and reopens on the first not done", () => {
    const steps = stepsFor(false);
    const m = deriveMarks({ projectId: "p", setup: base({ offline: true }) });
    expect(chipCount(steps, m)).toEqual({ done: 1, total: 4 });
    expect(firstUnfinished(steps, m)).toBe("where");
    expect(stepsFor(true)).not.toContain("where");
  });

  it("⓪ inference names its evidence; stored answers read back", () => {
    expect(inferWhere("http://localhost:8080").where).toBe("this_computer");
    expect(inferWhere("http://127.0.0.1:5173").where).toBe("this_computer");
    const srvGuess = inferWhere("http://ubuntu-srv:8080");
    expect(srvGuess).toEqual({
      where: "dedicated_server",
      evidence: "you opened Graphban at ubuntu-srv:8080",
      address: "http://ubuntu-srv:8080",
    });
    expect(storedWhere({ step: 0, name: "", status: "done", detail: "dedicated_server at http://x", how: "" })).toBe(
      "dedicated_server",
    );
    expect(storedWhere({ step: 0, name: "", status: "pending", detail: "this_computer", how: "" })).toBeNull();
  });
});
