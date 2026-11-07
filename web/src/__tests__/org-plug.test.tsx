import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

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
    expect(screen.getByRole("button", { name: "Finish tour" })).toBeInTheDocument();
  });

  it("hosted in an org: Invite a teammate, never Request an org", async () => {
    srv.hosted = true;
    srv.orgs = [{ id: "o", name: "Acme", plan: "pro", role: "member" }];
    const { onNavigate } = plug();
    fireEvent.click(await screen.findByRole("button", { name: /Invite a teammate/ }));
    expect(onNavigate).toHaveBeenCalledWith("/settings/members");
    expect(screen.queryByRole("button", { name: /Request/ })).toBeNull();
  });

  it("self-host: Link to an org opens Settings → Cloud / Sync; Not now is an equal button", async () => {
    const { onNavigate, onNotNow } = plug();
    fireEvent.click(await screen.findByRole("button", { name: /Link to an org/ }));
    expect(onNavigate).toHaveBeenCalledWith("/settings/deployment/sync");
    fireEvent.click(screen.getByRole("button", { name: "Not now" }));
    expect(onNotNow).toHaveBeenCalled();
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
