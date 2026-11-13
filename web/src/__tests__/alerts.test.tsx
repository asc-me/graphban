import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { AlertsBell } from "@/components/shell/AlertsBell";

const state: { feed: unknown; fail: boolean; push: unknown } = { feed: null, fail: false, push: null };
const subscribed: unknown[] = [];
vi.mock("@/lib/api", () => ({
  api: {
    myAlerts: async () => {
      if (state.fail) throw new Error("boom");
      return state.feed;
    },
    readAlert: async () => state.feed,
    projects: async () => [{ id: "prj_other", tag: "OTH" }],
    config: async () => ({ hosted_mode: true }),
    pushStatus: async () => state.push,
    pushSubscribe: async (sub: unknown) => {
      subscribed.push(sub);
      return state.push;
    },
  },
}));

function mount() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={qc}>
      <MemoryRouter>
        <AlertsBell />
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

describe("AlertsBell", () => {
  it("renders the panel outside the top bar's stacking context (GRPH-1559)", async () => {
    state.feed = { unread: 0, alerts: [] };
    const { container } = mount();
    await waitFor(() => expect(screen.getByTestId("alerts-count")).toHaveTextContent("0"));
    await userEvent.click(screen.getByRole("button", { name: /alerts/i }));
    const panel = screen.getByRole("dialog", { name: "Alerts" });
    expect(container.contains(panel)).toBe(false);
    expect(panel.parentElement).toBe(document.body);
    expect(panel.className).toContain("fixed");
    expect(panel.className).toContain("z-50");
  });

  beforeEach(() => {
    state.fail = false;
  });
  afterEach(() => cleanup());

  it("shows the unread count and lists alerts", async () => {
    state.feed = {
      unread: 2,
      alerts: [
        { id: "al_1", kind: "response_needed", project: "prj_other", question: "Which store?", body: "", wait: "GRPH-2",
          ticket: "GRPH-1", prd: "GRPH-P75", asked_by: "GRPH-A3", created_at: null, read: false },
      ],
    };
    mount();
    await waitFor(() => expect(screen.getByTestId("alerts-count")).toHaveTextContent("2"));
    await userEvent.click(screen.getByRole("button", { name: /alerts/i }));
    expect(screen.getByText("Which store?")).toBeInTheDocument();
    // The link is to the ALERT's project's Triage row, not whichever project is open.
    await waitFor(() =>
      expect(screen.getByText("Which store?").closest("a")).toHaveAttribute("href", "/p/OTH/triage?item=GRPH-1"),
    );
  });

  describe("row content (GRPH-1607)", () => {
    const row = (over: Record<string, unknown>) => ({
      id: "al_9", kind: "response_needed", project: "prj_other", project_tag: "OTH", title: "V9-A2 Build the Audience pane",
      question: "What belongs in settings?", body: "", wait: "GRPH-2", ticket: null, prd: null, asked_by: null,
      created_at: null, read: false, impact: "question", drain: "not_live", ...over,
    });
    const open = async (alert: unknown) => {
      state.feed = { unread: 1, alerts: [alert] };
      mount();
      await userEvent.click(await screen.findByRole("button", { name: /alerts/i }));
    };

    it("the unread headline sets an explicit foreground, not the link accent", async () => {
      await open(row({}));
      const h = await screen.findByTestId("alert-headline");
      expect(h.className).toContain("text-fg");
      expect(h.className).not.toContain("accent");
    });

    it("the headline is the item title and the full question is secondary", async () => {
      await open(row({}));
      expect(screen.getByTestId("alert-headline")).toHaveTextContent("V9-A2 Build the Audience pane");
      expect(screen.getByText("What belongs in settings?")).toBeInTheDocument();
    });

    it.each([
      ["drain_waiting", "live", "GRPH-1", "Drain waiting"],
      ["blocks", "not_live", "GRPH-1", "Blocks GRPH-1"],
      ["question", "not_live", null, "Question"],
      ["question", "unknown", null, "Question"],
    ])("impact %s (drain %s, ticket %s) shows %s", async (impact, drain, ticket, text) => {
      await open(row({ impact, drain, ticket }));
      expect(await screen.findByTestId("alert-impact")).toHaveTextContent(text as string);
    });
  });

  it("a failed read says could not load alerts and never renders 0", async () => {
    state.fail = true;
    mount();
    await userEvent.click(await screen.findByRole("button", { name: /could not load alerts/i }));
    expect(await screen.findByText(/could not load alerts/i)).toBeInTheDocument();
    expect(screen.getByTestId("alerts-count")).not.toHaveTextContent("0");
  });

  describe("browser push (GRPH-1528)", () => {
    const requestPermission = vi.fn(async () => "granted");
    const subscribe = vi.fn(async () => ({
      endpoint: "https://push.test/e",
      toJSON: () => ({ keys: { p256dh: "k", auth: "a" } }),
    }));

    beforeEach(() => {
      requestPermission.mockClear();
      subscribed.length = 0;
      state.feed = { unread: 0, alerts: [] };
      vi.stubGlobal("Notification", Object.assign(function () {}, { permission: "default", requestPermission }));
      Object.defineProperty(window, "PushManager", { value: function () {}, configurable: true });
      Object.defineProperty(navigator, "serviceWorker", {
        configurable: true,
        value: { register: vi.fn(async () => ({})), ready: Promise.resolve({ pushManager: { subscribe } }) },
      });
    });
    afterEach(() => vi.unstubAllGlobals());

    it("never asks for permission on load, only on the click", async () => {
      state.push = { configured: true, public_key: "AQAB" };
      mount();
      await userEvent.click(await screen.findByRole("button", { name: /alerts/i }));
      const btn = await screen.findByRole("button", { name: /notify me in this browser/i });
      expect(requestPermission).not.toHaveBeenCalled();
      await userEvent.click(btn);
      await waitFor(() => expect(requestPermission).toHaveBeenCalledTimes(1));
      await waitFor(() => expect(subscribed).toHaveLength(1));
      expect(subscribed[0]).toEqual({ endpoint: "https://push.test/e", keys: { p256dh: "k", auth: "a" } });
    });

    it("with no VAPID keys says so, offers no button and never prompts", async () => {
      state.push = { configured: false, public_key: null };
      mount();
      await userEvent.click(await screen.findByRole("button", { name: /alerts/i }));
      expect(await screen.findByText(/not set up on this deployment/i)).toBeInTheDocument();
      expect(screen.queryByRole("button", { name: /notify me in this browser/i })).toBeNull();
      expect(requestPermission).not.toHaveBeenCalled();
    });

    it("shows denied permission as such", async () => {
      state.push = { configured: true, public_key: "AQAB" };
      (Notification as unknown as { permission: string }).permission = "denied";
      mount();
      await userEvent.click(await screen.findByRole("button", { name: /alerts/i }));
      expect(await screen.findByText(/blocked/i)).toBeInTheDocument();
      expect(screen.queryByRole("button", { name: /notify me in this browser/i })).toBeNull();
    });
  });
});
