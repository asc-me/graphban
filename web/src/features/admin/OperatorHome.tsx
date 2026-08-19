import { AlertTriangle, Building2, History } from "lucide-react";
import { Link } from "react-router-dom";

import { useAdminActivity, useAdminOrgs, useAdminUsers } from "@/lib/queries";
import type { AdminActivity, AdminOrg } from "@/lib/types";

import {
  COUNTERS,
  Card,
  CardHead,
  Callout,
  Empty,
  PageHead,
  PLANS,
  compact,
  level,
  relTime,
  worstLevel,
} from "./parts";

/**
 * Screen 19 — the operator's home. Every tenant on this deployment, at a glance.
 *
 * Every number here is summed from the same four counters an org sees on its own
 * billing screen. There is no separate platform metric, deliberately: a figure only
 * the operator can see is a figure nobody can reconcile with the tenant's own view.
 */
export function OperatorHome() {
  const { data: orgs = [], isLoading } = useAdminOrgs();
  const { data: users = [] } = useAdminUsers();

  if (isLoading) return <Skeleton />;

  if (orgs.length === 0) {
    return (
      <div className="max-w-[1180px] px-5 py-6">
        <Head />
        <Empty
          title="No organizations yet"
          body={
            <>
              Nobody has founded a tenant on this deployment. That is a fresh install, not
              a fault — issue a platform invite from{" "}
              <Link to="/admin/licensing" className="text-st-next">
                Licensing
              </Link>{" "}
              and the first org appears here when it is redeemed.
            </>
          }
        />
      </div>
    );
  }

  // A seat is a member OR a still-pending invite (`quotas.seat_count`), so the seat
  // total legitimately runs ahead of the membership total. Showing only the first
  // number would leave an operator unable to explain their own quota bar.
  const seats = orgs.reduce((n, o) => n + o.usage.seats, 0);
  const memberships = orgs.reduce((n, o) => n + o.members.length, 0);
  const calls = orgs.reduce((n, o) => n + o.usage.calls_this_month, 0);
  const heldMemberships = users.reduce((n, u) => n + u.org_count, 0);
  const orgless = users.filter((u) => u.org_count === 0).length;
  const recent = orgs.filter(
    (o) => o.created_at && Date.now() - new Date(o.created_at).getTime() < 30 * 86400_000,
  ).length;

  const atCap = orgs.filter((o) => worstLevel(o.usage, o.limits) === "at");
  const nearCap = orgs.filter((o) => worstLevel(o.usage, o.limits) === "near");

  return (
    <div className="max-w-[1180px] px-5 pb-16 pt-6">
      <Head />

      <div className="mb-5 grid grid-cols-2 gap-3 lg:grid-cols-4">
        <Stat label="ORGS" value={String(orgs.length)} dot="bg-st-next"
              foot={recent === 1 ? "1 created in the last 30 days" : `${recent} created in the last 30 days`} />
        <Stat
          label="SEATS TAKEN"
          value={String(seats)}
          dot="bg-st-done"
          foot={
            seats === memberships
              ? "all held by accepted memberships"
              : `${memberships} accepted · ${seats - memberships} reserved by pending invites`
          }
        />
        <Stat
          label="USERS"
          value={String(users.length)}
          dot="bg-purple"
          foot={`holding ${heldMemberships} membership${heldMemberships === 1 ? "" : "s"} · ${
            orgless === 0 ? "everyone is in an org" : `${orgless} in no org at all`
          }`}
        />
        {atCap.length > 0 ? (
          <Stat
            label="ORGS AT A CAP"
            value={String(atCap.length)}
            suffix={`of ${orgs.length}`}
            dot="bg-st-review"
            tone="text-st-review"
            footTone="text-st-review/80"
            foot={atCap.map((o) => o.name).join(", ")}
          />
        ) : (
          <Stat
            label="MCP CALLS / MO"
            value={compact(calls)}
            dot="bg-st-done"
            foot={nearCap.length === 0 ? "no tenant near a cap" : `${nearCap.length} tenant(s) near a cap`}
          />
        )}
      </div>

      {atCap.length > 0 && (
        <div className="mb-5">
          <Callout tone="warn" icon={<AlertTriangle size={16} className="text-st-review" />}>
            <div className="text-[13px] font-semibold text-st-review">
              {atCap.map((o) => o.name).join(", ")} {atCap.length === 1 ? "is" : "are"} at a cap
            </div>
            <div className="mt-1">
              {atCap.map((o) => (
                <div key={o.id}>
                  <span className="font-mono text-op-fg-2">{o.name}</span> —{" "}
                  {COUNTERS.filter((c) => level(o.usage[c.key], o.limits[c.limit]) === "at")
                    .map((c) => c.label.toLowerCase())
                    .join(", ")}
                </div>
              ))}
              <div className="mt-1.5">
                The only lever from this plane is assigning a larger plan; individual
                counters cannot be raised.{" "}
                <Link to="/admin/orgs" className="text-st-review">
                  Open orgs →
                </Link>
              </div>
            </div>
          </Callout>
        </div>
      )}

      <div className="grid gap-4 lg:grid-cols-[1.3fr_1fr]">
        <PlanMix orgs={orgs} />
        <OperatorLedger orgs={orgs} />
      </div>
    </div>
  );
}

function Head() {
  return (
    <PageHead
      title="Platform"
      chip={
        <span className="rounded-full border border-st-next/30 bg-st-next/[0.07] px-2 py-0.5 font-mono text-[9.5px] tracking-[0.06em] text-st-next">
          OPERATOR
        </span>
      }
      lede="Every tenant on this deployment. Counts are summed from the same four counters each org sees on its own billing screen — there is no separate platform metric."
    />
  );
}

function Stat({
  label,
  value,
  suffix,
  dot,
  foot,
  tone = "text-op-fg",
  footTone = "text-op-faint",
}: {
  label: string;
  value: string;
  suffix?: string;
  dot: string;
  foot: string;
  tone?: string;
  footTone?: string;
}) {
  return (
    <div className="rounded-[13px] border border-op-line bg-op-card p-3.5">
      <div className="flex items-center gap-2">
        <span className="font-mono text-[9.5px] uppercase tracking-[0.07em] text-op-faint">{label}</span>
        <span className={`h-[5px] w-[5px] shrink-0 rounded-full ${dot}`} />
      </div>
      <div className="mt-2 flex items-baseline gap-1.5">
        <span className={`font-mono text-[23px] font-medium tracking-[-0.5px] ${tone}`}>{value}</span>
        {suffix && <span className="font-mono text-[11px] text-op-faint-2">{suffix}</span>}
      </div>
      <div className={`mt-2 text-[11px] ${footTone}`}>{foot}</div>
    </div>
  );
}

function PlanMix({ orgs }: { orgs: AdminOrg[] }) {
  const counts = PLANS.map((p) => ({ plan: p, n: orgs.filter((o) => o.plan === p).length }));
  const tint: Record<string, string> = {
    free: "bg-op-muted",
    pro: "bg-st-next",
    team: "bg-accent",
    enterprise: "bg-purple",
  };
  return (
    <Card>
      <CardHead
        icon={<Building2 size={15} className="text-st-next" />}
        title="Plan mix"
        right={
          <span className="font-mono text-[9px] tracking-[0.05em] text-op-faint-2">
            {orgs.length} ORGS
          </span>
        }
      />
      <div className="px-4 pb-3.5 pt-1.5">
        {counts.map(({ plan, n }) => (
          <div key={plan} className="flex items-center gap-3 border-b border-op-line-3 py-2.5">
            <span className="flex w-[86px] shrink-0 items-center gap-2">
              <span className={`h-[5px] w-[5px] shrink-0 rounded-full ${tint[plan]}`} />
              <span className="text-[12.5px] capitalize text-op-fg-2">{plan}</span>
            </span>
            <span className="min-w-0 flex-1">
              <span className="block h-[5px] overflow-hidden rounded-sm bg-op-line-2">
                <span
                  className={`block h-full ${tint[plan]}`}
                  style={{ width: `${orgs.length ? Math.round((n / orgs.length) * 100) : 0}%` }}
                />
              </span>
            </span>
            <span className="w-[70px] shrink-0 text-right font-mono text-[11px] text-op-muted-2">
              {n} org{n === 1 ? "" : "s"}
            </span>
          </div>
        ))}
        <p className="mt-2.5 text-[11px] leading-relaxed text-op-faint">
          Plans are operator-assigned — no tenant can change its own, so this mix only
          moves when someone here moves it.
        </p>
      </div>
    </Card>
  );
}

/**
 * The operator ledger.
 *
 * Named for what it is rather than "platform activity": it carries the four actions
 * that can be taken from this plane and nothing a tenant does, because tenant events
 * are project-scoped and stay there. An operator reading six rows and concluding "the
 * platform was quiet" would be reading an absence as a result, so the panel says which
 * absence an empty list is.
 */
function OperatorLedger({ orgs }: { orgs: AdminOrg[] }) {
  const { data: rows = [], isLoading } = useAdminActivity();
  // A plan change records the org id. Resolving it against the tenants already on this
  // page costs nothing and is the difference between a row you can act on and one that
  // makes you go looking — `org_23549e9a14` names nothing to a reader.
  const nameOf = (id: string) => orgs.find((o) => o.id === id)?.name ?? id;
  return (
    <Card>
      <CardHead
        icon={<History size={15} className="text-purple" />}
        title="Operator ledger"
        right={
          <span className="font-mono text-[9px] tracking-[0.05em] text-op-faint-2">THIS PLANE ONLY</span>
        }
      />
      <div className="px-4 pb-3 pt-1">
        {isLoading ? (
          <div className="py-6 text-center font-mono text-[11px] text-op-faint-2">loading…</div>
        ) : rows.length === 0 ? (
          <p className="py-5 text-[12px] leading-relaxed text-op-muted-2">
            No operator has done anything on this deployment yet. This is not a quiet
            platform — tenant activity never appears here, so an empty ledger only says
            that no plan was assigned and no invite was issued.
          </p>
        ) : (
          rows.map((e, i) => <LedgerRow key={`${e.ts}-${i}`} e={e} nameOf={nameOf} />)
        )}
      </div>
      <div className="border-t border-op-line-2 px-4 py-2.5 text-[11px] leading-relaxed text-op-faint">
        Operator actions only. What happens inside a tenant is project-scoped and is not
        readable from this plane.
      </div>
    </Card>
  );
}

const LEDGER_TONE: Record<string, string> = {
  create_platform_invite: "bg-st-next",
  revoke_platform_invite: "bg-st-blocked",
  decide_org_request: "bg-st-done",
  set_org_plan: "bg-purple",
};

function LedgerRow({ e, nameOf }: { e: AdminActivity; nameOf: (id: string) => string }) {
  const meta = (e.meta ?? {}) as Record<string, unknown>;
  const str = (k: string) => (meta[k] == null ? "" : String(meta[k]));
  const text =
    e.action === "create_platform_invite"
      ? `platform invite issued to ${str("email")}${str("plan") ? ` at the ${str("plan")} plan` : ""}`
      : e.action === "revoke_platform_invite"
        ? `platform invite to ${str("email")} revoked`
        : e.action === "decide_org_request"
          ? `additional-org request ${str("status") || "decided"}`
          : e.action === "set_org_plan"
            ? `${nameOf(e.target_id)} assigned the ${str("plan")} plan`
            : e.action;

  return (
    <div className="flex gap-2.5 border-b border-op-line-3 py-2.5">
      <span
        className={`mt-1.5 h-1.5 w-1.5 shrink-0 rounded-full ${LEDGER_TONE[e.action] ?? "bg-op-faint"}`}
      />
      <div className="min-w-0 flex-1">
        <div className="text-[12px] leading-snug text-op-fg-2">{text}</div>
        <div className="mt-1 font-mono text-[9.5px] text-op-faint-2">
          {relTime(e.ts) ?? "just now"} · {e.actor_label || "an operator"}
        </div>
      </div>
    </div>
  );
}

function Skeleton() {
  return (
    <div className="max-w-[1180px] px-5 pt-6">
      <div className="mb-5 h-6 w-40 rounded bg-op-card" />
      <div className="grid grid-cols-2 gap-3 lg:grid-cols-4">
        {[0, 1, 2, 3].map((i) => (
          <div key={i} className="h-[104px] rounded-[13px] border border-op-line bg-op-card" />
        ))}
      </div>
    </div>
  );
}
