import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import { OrgPlug } from "./OrgPlug";

vi.mock("@/lib/queries", () => ({
  useConfig: () => ({ data: { hosted_mode: false } }),
  useOrgs: () => ({ data: [] }),
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
      "Your code graph stays local-first",
    ])
      expect(screen.getByText(t)).toBeInTheDocument();
    expect(screen.getByText("deployment")).toBeInTheDocument();
    expect(container.textContent).not.toMatch(/gitops/i);
  });
});
