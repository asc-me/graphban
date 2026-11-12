import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { OrgPlug } from "./OrgPlug";

const state = vi.hoisted(() => ({
  hosted: false,
  orgs: [] as unknown[],
  sync: { data: { linked: false, org: "", cloud_url: "" } } as { data?: unknown; isError?: boolean },
}));

vi.mock("@/lib/queries", () => ({
  useConfig: () => ({ data: { hosted_mode: state.hosted } }),
  useOrgs: () => ({ data: state.orgs }),
  useSyncStatus: () => state.sync,
  useOnboarding: () => ({ data: { onboarding: {} } }),
  useRequestOrg: () => ({ mutateAsync: vi.fn(), isPending: false, isError: false }),
}));

describe("OrgPlug pitch", () => {
  it("gives the reasons to link, keeps 'deployment', drops '(Gitops)'", () => {
    const { container } = render(
      <QueryClientProvider client={new QueryClient()}>
        <OrgPlug onNotNow={() => {}} onNavigate={() => {}} />
      </QueryClientProvider>,
    );
    for (const t of [
      "Shared house rules",
      "Memory that carries across projects",
      "One roster of everyone's agents",
      "Capacity routing across your team's entitlements",
      "Your code graph stays local-first, and any project you don't choose to sync stays local only.",
    ])
      expect(screen.getByText(t)).toBeInTheDocument();
    expect(screen.getByText("deployment")).toBeInTheDocument();
    expect(container.textContent).not.toMatch(/gitops/i);
  });
});

function view() {
  return render(
    <QueryClientProvider client={new QueryClient()}>
      <OrgPlug onNotNow={() => {}} onNavigate={() => {}} />
    </QueryClientProvider>,
  );
}

describe("OrgPlug kinds", () => {
  afterEach(() => {
    cleanup();
    state.hosted = false;
    state.orgs = [];
    state.sync = { data: { linked: false, org: "", cloud_url: "" } };
  });

  it("linked: names the org, opens its console, no invite or request", () => {
    state.sync = { data: { linked: true, org: "Acme", cloud_url: "https://acme.example.com" } };
    const { container } = view();
    expect(container.textContent).toContain("This deployment is linked to Acme.");
    expect(screen.getByRole("link", { name: /Open the org/ })).toHaveAttribute("href", "https://acme.example.com/");
    expect(screen.getByText("Not now")).toBeInTheDocument();
    expect(screen.queryByText("Request an org")).toBeNull();
    expect(screen.queryByText(/Invite a teammate/)).toBeNull();
  });

  it.each(["", "not a url", "javascript:alert(1)"])(
    "linked with a malformed cloud_url (%j): no href, Open the org goes to Settings › Cloud / Sync",
    (cloud_url) => {
      state.sync = { data: { linked: true, org: "Acme", cloud_url } };
      const onNavigate = vi.fn();
      const { container } = render(
        <QueryClientProvider client={new QueryClient()}>
          <OrgPlug onNotNow={() => {}} onNavigate={onNavigate} />
        </QueryClientProvider>,
      );
      expect(container.textContent).toContain("This deployment is linked to Acme.");
      expect(screen.queryByRole("link")).toBeNull();
      expect(container.querySelector("a[href]")).toBeNull();
      fireEvent.click(screen.getByRole("button", { name: /Open the org/ }));
      expect(onNavigate).toHaveBeenCalledWith("/settings/deployment/sync?reason=malformed");
    },
  );

  it("unknown: an unreadable link state shows neither linked nor a request", () => {
    state.sync = { data: undefined, isError: true };
    const { container } = view();
    expect(container.querySelector("[data-tour-plug=unknown]")).not.toBeNull();
    expect(screen.queryByText("Request an org")).toBeNull();
    expect(screen.queryByText(/linked to/)).toBeNull();
  });

  it("hosted: unchanged", () => {
    state.hosted = true;
    state.orgs = [{ id: "o" }];
    view();
    expect(screen.getByText(/Invite a teammate/)).toBeInTheDocument();
  });

  it("request: unchanged", () => {
    view();
    expect(screen.getByText("Request an org")).toBeInTheDocument();
  });
});
