import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { InviteeLinkCard } from "@/features/onboarding/InviteeLinkCard";
import { api } from "@/lib/api";

vi.mock("@/lib/api", async (orig) => {
  const m = await orig<typeof import("@/lib/api")>();
  return { ...m, api: { ...m.api, orgs: vi.fn(), projects: vi.fn() } };
});

function mount() {
  render(
    <QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}>
      <InviteeLinkCard />
    </QueryClientProvider>,
  );
}

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});

describe("GRPH-1522 invitee link card", () => {
  it("is collapsed by default", () => {
    vi.mocked(api.orgs).mockResolvedValue([{ id: "o", name: "Acme", plan: "x", role: "member" }] as never);
    vi.mocked(api.projects).mockResolvedValue([] as never);
    mount();
    expect((document.querySelector("details[data-link-setup]") as HTMLDetailsElement).open).toBe(false);
  });

  it("expanded: sign-in copy, keyless command, reachable projects", async () => {
    vi.mocked(api.orgs).mockResolvedValue([{ id: "o", name: "Acme", plan: "x", role: "member" }] as never);
    vi.mocked(api.projects).mockResolvedValue([{ id: "p", tag: "CORE", name: "Core Platform" }] as never);
    mount();
    expect(await screen.findByText("CORE — Core Platform")).toBeInTheDocument();
    expect(screen.getByText(/Your seat in Acme is what the link uses/)).toBeInTheDocument();
    expect(screen.getByText(/^graphban link --cloud-url \S+$/)).toBeInTheDocument();
    expect(screen.getByText("Prints a code to approve here, signed in as you.")).toBeInTheDocument();
    expect(document.body.textContent).not.toMatch(/--api-key/);
    expect(document.querySelector("input")).toBeNull();
  });

  it("projects unknown: says so, never an empty list", async () => {
    vi.mocked(api.orgs).mockResolvedValue([{ id: "o", name: "Acme", plan: "x", role: "member" }] as never);
    vi.mocked(api.projects).mockRejectedValue(new Error("boom"));
    mount();
    expect(await screen.findByText("Couldn't load your projects.")).toBeInTheDocument();
    expect(screen.queryByRole("list")).toBeNull();
  });
});
