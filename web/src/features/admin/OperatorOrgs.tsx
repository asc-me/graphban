import * as React from "react";

import { useAdminOrgs, useSetOrgPlan } from "@/lib/queries";
import type { AdminOrg } from "@/lib/types";

import {
  COUNTERS,
  Empty,
  LEVEL_TEXT,
  Meter,
  PageHead,
  PLANS,
  PLAN_TONE,
  Pill,
  ROLE_TONE,
  Table,
  Th,
  compact,
  level,
  planLabel,
  relTime,
  shortDate,
  tintFor,
} from "./parts";

/**
 * Screen 20 — every tenant, with the counters they are measured against.
 *
 * Assigning a plan is the only write on this screen, and the drawer says so where the
 * buttons are. An operator can see inside an org and cannot act inside one: members are
 * listed but not editable, because roles and removal belong to the org's own admins.
 */
export function OperatorOrgs() {
  const { data: orgs = [], isLoading } = useAdminOrgs();
  const [open, setOpen] = React.useState<string | null>(null);

  return (
    <div className="max-w-[1300px] px-5 pb-16 pt-6">
      <PageHead
        title="Orgs"
        lede={
          <>
            Every tenant, with the counters they are measured against.{" "}
            <span className="text-op-fg-2">Assigning a plan is the only write on this screen</span> —
            you can see inside an org, but not act inside one.
          </>
        }
      />

      {isLoading ? (
        <div className="rounded-[13px] border border-op-line bg-op-card px-5 py-8 text-center font-mono text-[11px] text-op-faint-2">
          loading…
        </div>
      ) : orgs.length === 0 ? (
        <Empty
          title="No organizations yet"
          body="Nobody has founded a tenant here. A platform invite is what starts one — this table fills in when the first is redeemed."
        />
      ) : (
        <Table minWidth={960}>
          <div className="flex items-center gap-3 border-b border-op-line-2 bg-op-inset px-3.5 py-2.5 text-op-faint-2">
            <Th className="w-[140px] shrink-0">ORG</Th>
            <Th className="min-w-0 flex-1">OWNER</Th>
            <Th className="w-[80px] shrink-0">PLAN</Th>
            <Th className="w-[74px] shrink-0 text-right">SEATS</Th>
            <Th className="w-[68px] shrink-0 text-right">PROJECTS</Th>
            <Th className="w-[150px] shrink-0 text-right">MCP VS QUOTA</Th>
            <Th className="w-[84px] shrink-0 text-right">CREATED</Th>
            <span className="w-[58px] shrink-0" />
          </div>
          {orgs.map((org) => (
            <OrgRow
              key={org.id}
              org={org}
              open={open === org.id}
              onToggle={() => setOpen((cur) => (cur === org.id ? null : org.id))}
            />
          ))}
        </Table>
      )}
    </div>
  );
}

function OrgRow({ org, open, onToggle }: { org: AdminOrg; open: boolean; onToggle: () => void }) {
  const mcp = level(org.usage.calls_this_month, org.limits.max_calls_per_month);
  const seats = level(org.usage.seats, org.limits.max_seats);
  const accent = tintFor(org.id);

  return (
    <div className="border-b border-op-line-3">
      <div className="flex items-center gap-3 px-3.5 py-2.5 hover:bg-[#0e1218]">
        <span className="flex w-[140px] min-w-0 shrink-0 items-center gap-2">
          <span className="h-1.5 w-1.5 shrink-0 rounded-sm" style={{ background: accent }} />
          <span className="truncate font-mono text-[12px] text-op-fg">{org.name}</span>
        </span>
        <span className="flex min-w-0 flex-1 items-baseline gap-2">
          <span className="shrink-0 font-mono text-[11px] text-st-next">
            {org.owner_handle ? `@${org.owner_handle}` : "no owner"}
          </span>
          <span className="min-w-0 flex-1 truncate font-mono text-[10.5px] text-op-faint">
            {org.owner_email ?? ""}
          </span>
        </span>
        <span className="w-[80px] shrink-0">
          <Pill tone={PLAN_TONE[org.plan] ?? PLAN_TONE.free} label={planLabel(org.plan)} />
        </span>
        <span className={`w-[74px] shrink-0 text-right font-mono text-[11px] ${LEVEL_TEXT[seats]}`}>
          {org.usage.seats} / {compact(org.limits.max_seats)}
        </span>
        <span className="w-[68px] shrink-0 text-right font-mono text-[11px] text-op-muted-2">
          {org.usage.projects}
        </span>
        <span className="flex w-[150px] shrink-0 items-center justify-end gap-2">
          <Meter
            used={org.usage.calls_this_month}
            limit={org.limits.max_calls_per_month}
            className="w-[52px] shrink-0"
          />
          <span className={`font-mono text-[10.5px] ${LEVEL_TEXT[mcp]}`}>
            {compact(org.usage.calls_this_month)} / {compact(org.limits.max_calls_per_month)}
          </span>
        </span>
        <span className="w-[84px] shrink-0 text-right font-mono text-[10.5px] text-op-faint-2">
          {shortDate(org.created_at)}
        </span>
        <span className="flex w-[58px] shrink-0 justify-end">
          <button
            onClick={onToggle}
            aria-expanded={open}
            className={`h-[23px] rounded-md border border-op-line px-2 font-mono text-[9px] tracking-[0.05em] hover:border-st-next/40 hover:text-st-next ${
              open ? "text-st-next" : "text-op-muted"
            }`}
          >
            {open ? "CLOSE" : "OPEN"}
          </button>
        </span>
      </div>

      {open && <OrgDrawer org={org} />}
    </div>
  );
}

function OrgDrawer({ org }: { org: AdminOrg }) {
  const setPlan = useSetOrgPlan();
  const [assigned, setAssigned] = React.useState<string | null>(null);
  const atCap = COUNTERS.filter((c) => level(org.usage[c.key], org.limits[c.limit]) === "at");
  // `quotas.seat_count` = memberships + pending invites. Without this the drawer shows a
  // member list that quietly disagrees with the seat bar directly beside it.
  const reserved = Math.max(0, org.usage.seats - org.members.length);

  return (
    <div className="animate-fade border-t border-op-line-2 bg-[#090c11] px-3.5 py-3.5 pl-8">
      <div className="grid gap-4 md:grid-cols-2">
        <div>
          <div className="mb-2 font-mono text-[9px] tracking-[0.06em] text-op-faint-2">
            MEMBERS · ALL {org.members.length}
          </div>
          {org.members.length === 0 ? (
            <p className="text-[11.5px] leading-relaxed text-op-muted-2">
              This org has no memberships at all — not even an owner. That is a broken
              tenant rather than an empty one, and it is worth looking at.
            </p>
          ) : (
            <div className="flex flex-col gap-1">
              {org.members.map((m) => (
                <div
                  key={m.handle}
                  className="flex items-center gap-2.5 rounded-[7px] border border-op-line-2 bg-op-card px-2.5 py-1.5"
                >
                  <span className="w-[92px] shrink-0 truncate font-mono text-[10.5px] text-op-fg-2">
                    @{m.handle}
                  </span>
                  <span
                    className={`min-w-0 flex-1 font-mono text-[9.5px] uppercase tracking-[0.04em] ${
                      ROLE_TONE[m.role] ?? "text-op-muted-2"
                    }`}
                  >
                    {m.role}
                  </span>
                  <span className="font-mono text-[9.5px] text-op-faint-2">
                    joined {relTime(m.joined_at) ?? "—"}
                  </span>
                </div>
              ))}
            </div>
          )}
          {reserved > 0 && (
            <p className="mt-2 text-[11px] leading-relaxed text-st-review/80">
              The seat counter reads {org.usage.seats}, not {org.members.length}: a
              still-pending invite holds a seat so an org cannot out-invite its cap and
              only discover it when everyone accepts. {reserved} {reserved === 1 ? "is" : "are"}{" "}
              reserved that way and nobody is in them yet.
            </p>
          )}
          <p className="mt-2 text-[11px] leading-relaxed text-op-faint">
            Every member, listed but not editable. Roles and removal belong to the org's
            own admins.
          </p>
        </div>

        <div>
          <div className="mb-2 font-mono text-[9px] tracking-[0.06em] text-op-faint-2">
            USAGE VS PLAN
          </div>
          <div className="flex flex-col gap-1.5">
            {COUNTERS.map((c) => {
              const used = org.usage[c.key];
              const limit = org.limits[c.limit];
              return (
                <div key={c.key} className="flex items-center gap-2.5">
                  <span className="w-[96px] shrink-0 font-mono text-[9.5px] tracking-[0.05em] text-op-faint">
                    {c.label}
                  </span>
                  <Meter used={used} limit={limit} className="min-w-0 flex-1" />
                  <span
                    className={`w-[100px] shrink-0 text-right font-mono text-[10px] ${
                      LEVEL_TEXT[level(used, limit)]
                    }`}
                  >
                    {used.toLocaleString()} / {limit.toLocaleString()}
                  </span>
                </div>
              );
            })}
          </div>

          <div className="mt-3.5 border-t border-op-line-2 pt-3">
            <div className="mb-2 font-mono text-[9px] tracking-[0.06em] text-op-faint-2">
              ASSIGN PLAN <span className="text-op-faint-3">— THE ONLY WRITE HERE</span>
            </div>
            <div className="flex gap-1">
              {PLANS.map((p) => (
                <button
                  key={p}
                  disabled={setPlan.isPending}
                  onClick={() => {
                    setAssigned(p);
                    setPlan.mutate({ orgId: org.id, plan: p });
                  }}
                  className={`h-[27px] flex-1 rounded-[7px] border font-mono text-[9.5px] tracking-[0.04em] disabled:opacity-60 ${
                    org.plan === p ? PLAN_TONE[p] : "border-op-line bg-op-bg text-op-faint"
                  }`}
                >
                  {planLabel(p)}
                </button>
              ))}
            </div>
            <p
              className={`mt-2 text-[11px] leading-relaxed ${
                assigned && assigned === org.plan
                  ? "text-st-next"
                  : atCap.length
                    ? "text-st-review/80"
                    : "text-op-faint"
              }`}
            >
              {assigned && assigned === org.plan
                ? `Plan changed to ${org.plan}. Their caps move immediately; nothing already stored is deleted if the new plan is smaller — they simply cannot add more.`
                : atCap.length
                  ? `At a cap on the current plan (${atCap
                      .map((c) => c.label.toLowerCase())
                      .join(", ")}). A larger plan is the only lever here — individual counters cannot be raised.`
                  : "Takes effect immediately. Downgrading never deletes data; it only stops further growth past the new cap."}
            </p>
          </div>
        </div>
      </div>
    </div>
  );
}
