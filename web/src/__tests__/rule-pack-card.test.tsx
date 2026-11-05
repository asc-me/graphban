import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { RulePackCard } from "@/features/settings/RulePackCard";
import { RulesView } from "@/features/rules/RulesView";
import { settingsPath } from "@/lib/routes";
import type { RulePackAvailable, RulePackOffer, RulePackStatus } from "@/lib/types";

const m = vi.hoisted(() => ({
  config: vi.fn(),
  status: vi.fn(),
  avail: vi.fn(),
  offer: vi.fn(),
  accept: vi.fn(),
}));

vi.mock("@/lib/api", () => ({
  api: {
    config: m.config,
    rulePackStatus: m.status,
    rulePackAvailable: m.avail,
    offerRulePack: m.offer,
    acceptRulePack: m.accept,
    rulesFlow: vi.fn(async () => new Promise(() => {})),
    gitops: vi.fn(async () => ({ fields: {}, control: { state: "local", writable: true, message: "" } })),
    syncStatus: vi.fn(async () => ({ linked: false, url: "", projects: [] })),
  },
}));
vi.mock("@/features/ProjectContext", () => ({
  useProjectCtx: () => ({ activeId: "core", active: { id: "core", tag: "CORE", name: "Core" }, projects: [], setActiveId: () => {} }),
}));

const installed: RulePackStatus = {
  installed: true, loaded: true, name: "gb-rules", version: "1.0.0", sha256: "aaa111",
  installed_at: "2026-11-01T00:00:00Z", installed_by: "u1", reason: "",
};
const none: RulePackStatus = { ...installed, installed: false, loaded: false, version: null, sha256: null, reason: "none" };
const availState = (over: Partial<RulePackAvailable>): RulePackAvailable => ({
  state: "current", installed_version: "1.0.0", version: null, notes: null, feed_error: null, ...over,
});
const offer: RulePackOffer = {
  from: "1.0.0", to: "1.1.0", sha256: "bbb222",
  rules: { added: ["r.new"], removed: ["r.old"], changed: { "r.mod": { title: { from: "a", to: "b" } } } },
  profiles: { added: [], removed: [], changed: {} },
  newly_refused: { "r.bad": "needs primitive x" },
};

function wrap(ui: React.ReactNode) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={client}><MemoryRouter>{ui}</MemoryRouter></QueryClientProvider>,
  );
}

beforeEach(() => {
  vi.clearAllMocks();
  m.config.mockResolvedValue({ hosted_mode: false, signup_mode: "closed" });
  m.status.mockResolvedValue(installed);
  m.avail.mockResolvedValue(availState({}));
});

describe("rule pack card states", () => {
  it("none installed", async () => {
    m.status.mockResolvedValue(none);
    m.avail.mockResolvedValue(availState({ installed_version: null }));
    wrap(<RulePackCard />);
    expect(await screen.findByText("No rule pack installed")).toBeTruthy();
  });

  it("current", async () => {
    wrap(<RulePackCard />);
    expect(await screen.findByText("On the latest rule pack")).toBeTruthy();
    expect(screen.getByTestId("rulepack-installed").textContent).toContain("1.0.0");
    expect(screen.getByTestId("rulepack-installed").textContent).toContain("aaa111");
  });

  it("available, with notes", async () => {
    m.avail.mockResolvedValue(availState({ state: "available", version: "1.1.0", notes: "Adds **r.new**" }));
    wrap(<RulePackCard />);
    expect(await screen.findByText("1.1.0")).toBeTruthy();
    expect(screen.getByTestId("rulepack-notes").textContent).toContain("Adds");
    expect(screen.queryByText("On the latest rule pack")).toBeNull();
  });

  it("could not check is never current", async () => {
    m.avail.mockResolvedValue(availState({ state: "unknown", feed_error: "HTTP 503" }));
    wrap(<RulePackCard />);
    expect(await screen.findByText(/Could not check for a newer rule pack: HTTP 503/)).toBeTruthy();
    expect(screen.queryByText("On the latest rule pack")).toBeNull();
    expect(screen.getByTestId("rulepack-state").getAttribute("data-state")).toBe("unknown");
  });

  it("installed but not loaded", async () => {
    m.status.mockResolvedValue({ ...installed, loaded: false, reason: "signature no longer verifies", warning: "signature no longer verifies" });
    wrap(<RulePackCard />);
    expect((await screen.findByTestId("rulepack-not-loaded")).textContent).toContain("signature no longer verifies");
  });

  it("shows a pack_compat warning from update-check, but not when compatible", async () => {
    const { unmount } = wrap(<RulePackCard packCompat={{ state: "refused", reason: "needs server 2027" }} />);
    expect((await screen.findByTestId("rulepack-compat")).textContent).toContain("needs server 2027");
    unmount();
    wrap(<RulePackCard packCompat={{ state: "compatible", reason: "" }} />);
    await screen.findByText("On the latest rule pack");
    expect(screen.queryByTestId("rulepack-compat")).toBeNull();
  });

  it("hosted non-admin sees it read-only", async () => {
    m.config.mockResolvedValue({ hosted_mode: true, signup_mode: "closed" });
    m.avail.mockRejectedValue(Object.assign(new Error("{}"), { status: 403 }));
    wrap(<RulePackCard />);
    expect(await screen.findByText(/Only a platform admin/)).toBeTruthy();
    expect(screen.queryByRole("button", { name: "Preview" })).toBeNull();
    expect(screen.queryByRole("button", { name: "Install" })).toBeNull();
  });
});

describe("preview then install", () => {
  beforeEach(() => {
    m.avail.mockResolvedValue(availState({ state: "available", version: "1.1.0", notes: "n" }));
    m.offer.mockResolvedValue(offer);
  });

  it("disables Install until a preview, then sends the preview's sha256", async () => {
    m.accept.mockResolvedValue({ ...installed, version: "1.1.0" });
    wrap(<RulePackCard />);
    const install = await screen.findByRole("button", { name: "Install" });
    expect((install as HTMLButtonElement).disabled).toBe(true);
    await userEvent.click(screen.getByRole("button", { name: "Preview" }));
    expect(m.offer).toHaveBeenCalledWith("1.1.0");
    expect((await screen.findByTestId("diff-rules")).textContent).toContain("r.new");
    expect(screen.getByTestId("diff-refused").textContent).toContain("r.bad");
    expect((screen.getByRole("button", { name: "Install" }) as HTMLButtonElement).disabled).toBe(false);
    await userEvent.click(screen.getByRole("button", { name: "Install" }));
    await waitFor(() => expect(m.accept).toHaveBeenCalledTimes(1));
    expect(m.accept).toHaveBeenCalledWith("1.1.0", "bbb222");
  });

  it("a 409 asks for a re-preview and does not retry", async () => {
    m.accept.mockRejectedValue(Object.assign(new Error("sha mismatch"), { status: 409 }));
    wrap(<RulePackCard />);
    await userEvent.click(await screen.findByRole("button", { name: "Preview" }));
    await screen.findByTestId("rulepack-diff");
    await userEvent.click(screen.getByRole("button", { name: "Install" }));
    expect(await screen.findByTestId("rulepack-stale")).toBeTruthy();
    expect(m.accept).toHaveBeenCalledTimes(1);
    expect((screen.getByRole("button", { name: "Install" }) as HTMLButtonElement).disabled).toBe(true);
    expect(screen.getByRole("button", { name: "Preview again" })).toBeTruthy();
    await new Promise((r) => setTimeout(r, 50));
    expect(m.accept).toHaveBeenCalledTimes(1);
  });
});

describe("Rules page pill", () => {
  const renderRules = () => wrap(<RulesView />);

  it("shows an update pill linking to the Updates route when available", async () => {
    m.avail.mockResolvedValue(availState({ state: "available", version: "1.1.0" }));
    renderRules();
    const pill = await screen.findByTestId("rules-pack-update");
    expect(pill.getAttribute("href")).toBe(settingsPath("deployment/updates"));
  });

  it("shows a warning pill when the pack is not loaded", async () => {
    m.status.mockResolvedValue({ ...installed, loaded: false });
    renderRules();
    const pill = await screen.findByTestId("rules-pack-not-loaded");
    expect(pill.getAttribute("href")).toBe(settingsPath("deployment/updates"));
  });

  it.each([
    ["current", availState({})],
    ["unknown", availState({ state: "unknown", feed_error: "down" })],
  ])("shows nothing when %s", async (_n, a) => {
    m.avail.mockResolvedValue(a);
    renderRules();
    await waitFor(() => expect(m.avail).toHaveBeenCalled());
    await waitFor(() => expect(m.status).toHaveBeenCalled());
    await new Promise((r) => setTimeout(r, 30));
    expect(screen.queryByTestId("rules-pack-update")).toBeNull();
    expect(screen.queryByTestId("rules-pack-not-loaded")).toBeNull();
  });
});
