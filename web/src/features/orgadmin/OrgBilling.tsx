import { useBilling, useOrgs } from "@/lib/queries";
import type { Billing } from "@/lib/types";

const COUNTERS: { key: keyof Billing["usage"]; limit: keyof Billing["limits"]; label: string }[] = [
  { key: "projects", limit: "max_projects", label: "PROJECTS" },
  { key: "seats", limit: "max_seats", label: "SEATS" },
  { key: "shards", limit: "max_shards", label: "MEMORY SHARDS" },
  { key: "calls_this_month", limit: "max_calls_per_month", label: "MCP CALLS / MO" },
];

/**
 * Screen 12 — Billing. Display only, and that is the whole design.
 *
 * Four rows because four counters exist. No payment method, no invoice list, no
 * self-serve upgrade — there is no Stripe and plans are operator-assigned. No usage chart
 * either: `OrgUsage` holds one row per period, so a chart would be drawing a time series
 * that does not exist.
 */
export function OrgBilling() {
  const { data: orgs = [] } = useOrgs();
  const org = orgs[0] ?? null;
  const { data: billing, isLoading } = useBilling(org?.id);

  if (isLoading || !billing) {
    return (
      <div className="max-w-[1180px] px-6 py-8 font-mono text-[11px] text-faint-2">loading…</div>
    );
  }

  return (
    <div className="max-w-[1180px] px-6 pb-16 pt-5">
      <section className="rounded-[13px] border border-line bg-surface-2 p-4">
        <div className="flex flex-wrap items-center gap-2.5">
          <h2 className="text-[15px] font-semibold capitalize">{billing.plan} plan</h2>
          <span className="rounded-full border border-accent/30 bg-accent/[0.07] px-2 py-0.5 font-mono text-[9px] uppercase tracking-[0.06em] text-accent">
            current
          </span>
        </div>
        <p className="mt-2 max-w-[70ch] text-[12.5px] leading-relaxed text-muted">
          Contact your operator to change plan. Plans are assigned by a platform operator on this
          deployment — there is no self-serve upgrade, and this screen does not pretend otherwise.
        </p>
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
            // Explicit class strings: Tailwind scans source text, so `bg-${tone}` would
            // compile to nothing and the bar would render invisible.
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
