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
