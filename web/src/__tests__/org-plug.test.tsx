import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { onOrgWindowRequested } from "@/features/settings/CloudOrgLinkDialog";
import { OrgPlug, canRequestAgain } from "@/features/tour/OrgPlug";
import { onTourRequested, requestTour } from "@/features/tour/replay";
import { StartupPromptRows } from "@/features/onboarding/StartupPrompts";

/**
 * GRPH-1364 — the tour's closing org card. The server is a mock that stores
 * `org_requested_at` the way the real one does, so "the card shows the stored date after
 * reload" is a fresh render against that stored state.
 */

const srv = vi.hoisted(() => ({
  hosted: false,
  orgs: [] as { id: string; name: string; plan: string; role: string }[],
  onboarding: null as { org_requested_at?: string } | null,
  fail: false,
  sent: [] as unknown[],
}));

vi.mock("@/lib/api", () => ({
  hasSession: () => true,
  api: {
    config: vi.fn(async () => ({ hosted_mode: srv.hosted })),
    orgs: vi.fn(async () => srv.orgs),
    syncStatus: vi.fn(async () => ({ linked: false, org: "", cloud_url: "" })),
    onboarding: vi.fn(async () => ({ onboarding: srv.onboarding })),
    requestOrg: vi.fn(async (body: unknown) => {
      if (srv.fail) throw new Error("no operator address");
      srv.sent.push(body);
      srv.onboarding = { ...(srv.onboarding ?? {}), org_requested_at: new Date().toISOString() };
      return { onboarding: srv.onboarding };
    }),
  },
}));

function plug() {
  const onNotNow = vi.fn();
  const onNavigate = vi.fn();
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  render(
    <QueryClientProvider client={qc}>
      <MemoryRouter>
        <OrgPlug onNotNow={onNotNow} onNavigate={onNavigate} />
      </MemoryRouter>
    </QueryClientProvider>,
  );
  return { onNotNow, onNavigate };
}

beforeEach(() => {
  srv.hosted = false;
  srv.orgs = [];
  srv.onboarding = null;
  srv.fail = false;
  srv.sent = [];
});
afterEach(cleanup);

describe("GRPH-1364 org plug", () => {
  it("self-host: Request an org sends name, email and note, then shows the stored date after a reload", async () => {
    const user = userEvent.setup();
    plug();
    await user.click(await screen.findByRole("button", { name: "Request an org" }));
    const send = screen.getByRole("button", { name: "Send" });
    expect(send).toBeDisabled(); // email is required
    await user.type(screen.getByLabelText("Name"), "Alex");
    await user.type(screen.getByLabelText("Email"), "alex@example.com");
    await user.type(screen.getByLabelText(/Note/), "four of us");
    await user.click(send);
    expect(await screen.findByText(/Requested on/)).toBeInTheDocument();
    expect(srv.sent).toEqual([{ name: "Alex", email: "alex@example.com", note: "four of us" }]);
    expect(screen.queryByRole("button", { name: "Request again" })).toBeNull();

    cleanup();
    plug(); // a reload: the date comes from the stored document
    expect(await screen.findByText(/Requested on/)).toBeInTheDocument();
  });

  it("a failed relay shows the error and never the date", async () => {
    srv.fail = true;
    const user = userEvent.setup();
    plug();
    await user.click(await screen.findByRole("button", { name: "Request an org" }));
    await user.type(screen.getByLabelText("Name"), "Alex");
    await user.type(screen.getByLabelText("Email"), "alex@example.com");
    await user.click(screen.getByRole("button", { name: "Send" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("no operator address");
    expect(screen.queryByText(/Requested on/)).toBeNull();
  });

  it("Request again appears only once the stored date is 14 days old", async () => {
    const now = Date.parse("2026-11-07T00:00:00Z");
    expect(canRequestAgain("2026-10-24T00:00:00Z", now)).toBe(true);
    expect(canRequestAgain("2026-10-25T00:00:00Z", now)).toBe(false);
    srv.onboarding = { org_requested_at: new Date(Date.now() - 15 * 86_400_000).toISOString() };
    plug();
    expect(await screen.findByRole("button", { name: "Request again" })).toBeInTheDocument();
  });

  it("a recent request offers no second one", async () => {
    srv.onboarding = { org_requested_at: new Date().toISOString() };
    plug();
    expect(await screen.findByText(/Requested on/)).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /Request/ })).toBeNull();
    // Self-host still leads with Link to an org →, so Not now stays its equal.
    expect(screen.getByRole("button", { name: "Link to an org →" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Not now" })).toBeInTheDocument();
  });

  it("hosted in an org: Invite a teammate, never Request an org", async () => {
    srv.hosted = true;
    srv.orgs = [{ id: "o", name: "Acme", plan: "pro", role: "member" }];
    const { onNavigate } = plug();
    fireEvent.click(await screen.findByRole("button", { name: /Invite a teammate/ }));
    expect(onNavigate).toHaveBeenCalledWith("/settings/members");
    expect(screen.queryByRole("button", { name: /Request/ })).toBeNull();
  });

  it("self-host, not linked: the primary is Link to an org →, which ends the tour and opens the Organization window (L5)", async () => {
    const opened = vi.fn();
    const off = onOrgWindowRequested(opened);
    const { onNavigate, onNotNow } = plug();
    const primary = await screen.findByRole("button", { name: "Link to an org →" });
    // The primary sits in the action pair, before Not now — never the request.
    const pair = primary.parentElement!;
    expect(Array.from(pair.querySelectorAll("button")).map((b) => b.textContent)).toEqual(["Link to an org →", "Not now"]);
    expect(screen.getByText("Opens Organization → Link to an org. You'll sign in with a code.")).toBeInTheDocument();
    fireEvent.click(primary);
    expect(onNotNow).toHaveBeenCalled();
    expect(opened).toHaveBeenCalledTimes(1);
    expect(onNavigate).not.toHaveBeenCalled();
    off();
  });

  it("self-host: Request an org stays as the secondary action, outside the pair", async () => {
    plug();
    const req = await screen.findByRole("button", { name: "Request an org" });
    const primary = screen.getByRole("button", { name: "Link to an org →" });
    expect(req.parentElement).not.toBe(primary.parentElement);
    fireEvent.click(screen.getByRole("button", { name: "Not now" }));
  });

  it("the pitch ends with local-only for unchosen projects", async () => {
    plug();
    expect(
      await screen.findByText("Your code graph stays local-first, and any project you don't choose to sync stays local only."),
    ).toBeInTheDocument();
  });
});

describe("GRPH-1364 replay and prompts", () => {
  it("requestTour reaches the shell's listener", () => {
    const fn = vi.fn();
    const off = onTourRequested(fn);
    requestTour();
    off();
    requestTour();
    expect(fn).toHaveBeenCalledTimes(1);
  });

  it("lists the four start-up prompts with their skills", () => {
    render(<StartupPromptRows />);
    expect(screen.getAllByRole("listitem")).toHaveLength(4);
    expect(screen.getByLabelText("Copy /graphban-prd and /graphban-grill")).toBeInTheDocument();
    void waitFor;
  });
});
