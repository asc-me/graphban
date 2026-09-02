import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { describe, expect, it, vi } from "vitest";

import type { Billing, Org } from "@/lib/types";

const mocks = vi.hoisted(() => ({
  checkout: vi.fn(async () => ({ url: "https://checkout.stripe.com/c/x" })),
  portal: vi.fn(async () => ({ url: "https://billing.stripe.com/p/x" })),
  orgs: [{ id: "org_acme", name: "Acme", plan: "free", role: "admin" }] as Org[],
  billing: {
    plan: "free",
    self_serve: false,
    has_customer: false,
    usage: { projects: 1, seats: 1, shards: 0, calls_this_month: 0 },
    limits: { max_projects: 50, max_seats: 100, max_shards: 100000, max_calls_per_month: 1000000 },
  } as Billing,
}));

vi.mock("@/lib/api", () => ({
  api: {
    orgCheckout: mocks.checkout,
    orgPortal: mocks.portal,
  },
}));

vi.mock("@/lib/queries", () => ({
  useOrgs: () => ({ data: mocks.orgs }),
  useBilling: () => ({ data: mocks.billing, isLoading: false }),
}));

const { OrgBilling } = await import("./OrgBilling");

function draw() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={qc}>
      <MemoryRouter>
        <OrgBilling />
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

describe("Org billing self-serve (GRPH-660)", () => {
  it("unset Stripe keeps operator-assigned copy, not Checkout", () => {
    mocks.billing.self_serve = false;
    mocks.orgs[0].role = "admin";
    draw();
    expect(screen.getByText(/self-serve is off, not missing/i)).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /Upgrade to/i })).not.toBeInTheDocument();
  });

  it("self_serve offers Checkout to an org admin", async () => {
    mocks.billing.self_serve = true;
    mocks.billing.has_customer = false;
    mocks.billing.plan = "free";
    mocks.orgs[0].role = "admin";
    mocks.checkout.mockClear();
    draw();
    expect(screen.getByText(/Checkout upgrades Pro or Team/i)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /Upgrade to pro/i })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /Manage billing/i })).not.toBeInTheDocument();
  });

  it("a member sees that self-serve is on, not Checkout buttons", () => {
    mocks.billing.self_serve = true;
    mocks.orgs[0].role = "member";
    draw();
    expect(screen.getByText(/org admin can start Checkout/i)).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /Upgrade to/i })).not.toBeInTheDocument();
  });

  it("enterprise is not Checkout", () => {
    mocks.billing.self_serve = true;
    mocks.billing.has_customer = false;
    mocks.billing.plan = "enterprise";
    mocks.orgs[0].role = "admin";
    draw();
    expect(screen.getByText(/Enterprise is operator-assigned/i)).toBeInTheDocument();
    expect(screen.getByText(/Checkout is Pro\/Team only/i)).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /Upgrade to/i })).not.toBeInTheDocument();
  });

  it("enterprise with a customer can still manage billing", () => {
    mocks.billing.self_serve = true;
    mocks.billing.has_customer = true;
    mocks.billing.plan = "enterprise";
    mocks.orgs[0].role = "admin";
    draw();
    expect(screen.getByRole("button", { name: /Manage billing/i })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /Upgrade to/i })).not.toBeInTheDocument();
  });

  it("Manage billing only when a customer exists", () => {
    mocks.billing.self_serve = true;
    mocks.billing.has_customer = true;
    mocks.billing.plan = "pro";
    mocks.orgs[0].role = "owner";
    draw();
    expect(screen.getByRole("button", { name: /Manage billing/i })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /Upgrade to pro/i })).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: /Upgrade to team/i })).toBeInTheDocument();
  });
});
