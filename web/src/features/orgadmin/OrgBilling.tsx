import { useMutation } from "@tanstack/react-query";

import { Button } from "@/components/ui/button";
import { useBilling, useOrgs } from "@/lib/queries";
import { api } from "@/lib/api";
import type { Billing } from "@/lib/types";

const COUNTERS: { key: keyof Billing["usage"]; limit: keyof Billing["limits"]; label: string }[] = [
  { key: "projects", limit: "max_projects", label: "PROJECTS" },
  { key: "seats", limit: "max_seats", label: "SEATS" },
  { key: "shards", limit: "max_shards", label: "MEMORY SHARDS" },
  { key: "calls_this_month", limit: "max_calls_per_month", label: "MCP CALLS / MO" },
];

const SELF_SERVE_PLANS = ["pro", "team"] as const;

/**
 * Screen 12 — Billing.
 *
 * Four counters always. Self-serve (GRPH-82) is Checkout + portal when
 * `billing.self_serve` is true. False is operator-assigned plans, not a broken
 * page and not "the product has no Stripe".
 */
export function OrgBilling() {
  const { data: orgs = [] } = useOrgs();
  const org = orgs[0] ?? null;
  const { data: billing, isLoading } = useBilling(org?.id);
  const here = typeof window !== "undefined" ? window.location.href : "";

  const checkout = useMutation({
    mutationFn: (plan: string) =>
      api.orgCheckout(org!.id, { plan, success_url: here, cancel_url: here }),
    onSuccess: ({ url }) => { window.location.href = url; },
  });
  const portal = useMutation({
    mutationFn: () => api.orgPortal(org!.id, { return_url: here }),
    onSuccess: ({ url }) => { window.location.href = url; },
  });

  if (isLoading || !billing) {
    return (
      <div className="max-w-[1180px] px-6 py-8 font-mono text-[11px] text-faint-2">loading…</div>
    );
  }

  const admin = org?.role === "admin" || org?.role === "owner";
  const selfServe = Boolean(billing.self_serve);
  const upgrades = SELF_SERVE_PLANS.filter((p) => p !== billing.plan);

  return (
    <div className="max-w-[1180px] px-6 pb-16 pt-5">
      <section className="rounded-[13px] border border-line bg-surface-2 p-4">
        <div className="flex flex-wrap items-center gap-2.5">
          <h2 className="text-[15px] font-semibold capitalize">{billing.plan} plan</h2>
          <span className="rounded-full border border-accent/30 bg-accent/[0.07] px-2 py-0.5 font-mono text-[9px] uppercase tracking-[0.06em] text-accent">
            current
          </span>
        </div>
        {selfServe ? (
          <>
            <p className="mt-2 max-w-[70ch] text-[12.5px] leading-relaxed text-muted">
              Checkout upgrades Pro or Team. An operator can still assign a plan by hand.
              There is no invoice list on this page.
            </p>
            {admin ? (
              <div className="mt-3 flex flex-wrap gap-2">
                {upgrades.map((plan) => (
                  <Button
                    key={plan}
                    size="sm"
                    disabled={checkout.isPending}
                    onClick={() => checkout.mutate(plan)}
                  >
                    Upgrade to {plan}
                  </Button>
                ))}
                {billing.has_customer && (
                  <Button
                    size="sm"
                    variant="outline"
                    disabled={portal.isPending}
                    onClick={() => portal.mutate()}
                  >
                    Manage billing
                  </Button>
                )}
              </div>
            ) : (
              <p className="mt-2 text-[12px] text-faint">
                An org admin can start Checkout. This seat cannot.
              </p>
            )}
          </>
        ) : (
          <p className="mt-2 max-w-[70ch] text-[12.5px] leading-relaxed text-muted">
            Contact your operator to change plan. Plans are assigned by a platform operator on this
            deployment — self-serve is off, not missing. This screen does not pretend otherwise.
          </p>
        )}
      </section>

      <section className="mt-4 overflow-hidden rounded-[13px] border border-line bg-surface-2">
        <div className="border-b border-line bg-surface px-4 py-2.5 font-mono text-[9px] uppercase tracking-[0.07em] text-faint-2">
          LIMITS VS USAGE
        </div>
        <div className="p-4">
          {COUNTERS.map((c) => {
            const used = billing.usage[c.key];
            const limit = billing.limits[c.limit];
            const pct = limit > 0 ? Math.min(100, Math.round((used / limit) * 100)) : 0;
            const bar =
              pct >= 100 ? "bg-st-blocked" : pct >= 80 ? "bg-st-review" : "bg-st-done";
            const text =
              pct >= 100 ? "text-st-blocked" : pct >= 80 ? "text-st-review" : "text-muted";
            return (
              <div key={c.key} className="flex items-center gap-3 py-2">
                <span className="w-[130px] shrink-0 font-mono text-[9.5px] uppercase tracking-[0.06em] text-faint">
                  {c.label}
                </span>
                <span className="h-1 min-w-0 flex-1 overflow-hidden rounded-sm bg-line">
                  <span className={`block h-full ${bar}`} style={{ width: `${pct}%` }} />
                </span>
                <span className={`w-[150px] shrink-0 text-right font-mono text-[10.5px] ${text}`}>
                  {used.toLocaleString()} / {limit.toLocaleString()}
                </span>
              </div>
            );
          })}
          <p className="mt-3 text-[11px] leading-relaxed text-faint">
            A seat is a membership or a pending invite, so the seat row counts both. Enterprise
            caps are large but finite — 500 projects, 1,000 seats, 1M shards, 10M calls — and are
            shown as numbers rather than as “unlimited”.
          </p>
        </div>
      </section>
    </div>
  );
}
