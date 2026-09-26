import { Boxes, Network, Radar, Users } from "lucide-react";
import type { ReactNode } from "react";
import { Link } from "react-router-dom";

import { PlaceHeader } from "@/components/shell/PlaceHeader";
import { useProjectCtx } from "@/features/ProjectContext";
import { useCounts, useDashboard, useFleet } from "@/lib/queries";

/**
 * Self-host Home (GRPH-P28 D2). KPIs come from the dashboard payload and the shell
 * counts endpoint — never from fetching items/shards to call `.length` (GRPH-431).
 *
 * GRPH-925: one focal "needs attention" number; inventory totals stay visible but secondary.
 */
export function HomeView() {
  const { activeId } = useProjectCtx();
  const counts = useCounts(activeId);
  const dash = useDashboard(activeId);
  const fleet = useFleet(activeId);

  const firstLoad = (counts.isLoading && !counts.data) || (dash.isLoading && !dash.data);
  const firstFailed = (!counts.data && counts.isError) || (!dash.data && dash.isError);
  const stale = (!!counts.data && counts.isError) || (!!dash.data && dash.isError);

  if (firstLoad) {
    return (
      <div className="flex h-full min-h-0 flex-col">
        <PlaceHeader viewName="Home" purpose="Project health at a glance." />
        <div className="min-h-0 flex-1 overflow-y-auto p-6" aria-busy="true">
          <div className="h-[108px] animate-pulse rounded-[14px] border border-line-2 bg-surface-2" />
          <div className="space-y-2">
            {Array.from({ length: 3 }).map((_, i) => (
              <div key={i} className="h-[52px] animate-pulse rounded-[12px] border border-line-2 bg-surface-2" />
            ))}
          </div>
          <div className="grid grid-cols-2 gap-2 sm:grid-cols-4">
            {Array.from({ length: 4 }).map((_, i) => (
              <div key={i} className="h-[64px] animate-pulse rounded-[11px] border border-line-2 bg-surface-2" />
            ))}
          </div>
        </div>
      </div>
    );
  }

  if (firstFailed) {
    return (
      <div className="flex h-full min-h-0 flex-col">
        <PlaceHeader viewName="Home" purpose="Project health at a glance." />
        <div className="min-h-0 flex-1 overflow-y-auto p-6">
          <p className="rounded-[12px] border border-st-blocked/30 bg-st-blocked/[0.06] px-3.5 py-3 text-[13px] text-st-blocked">
            counts unavailable
          </p>
        </div>
      </div>
    );
  }

  const d = dash.data;
  const c = counts.data;
  const live = fleet.data?.online ?? 0;
  const memoryQueue = c?.review ?? 0;
  const blocked = d?.blocked_count ?? 0;
  const inFlight = d?.in_progress_count ?? c?.items_in_progress ?? 0;
  const itemReview = d?.items_by_status?.review ?? 0;
  const needsAttention = inFlight + itemReview + blocked + memoryQueue;
  const itemsTotal = d?.items_total ?? c?.items ?? 0;
  const shardTotal = d?.shard_count ?? 0;
  const prdTotal = d?.prd_count ?? 0;

  return (
    <div className="flex h-full min-h-0 flex-col">
      <PlaceHeader
        viewName="Home"
        purpose="Project health at a glance — items, memory, and who is in flight."
      />

      <div className="min-h-0 flex-1 overflow-y-auto p-6">
        {stale && (
          <div className="mb-4 rounded-[11px] border border-st-review/30 bg-st-review/[0.06] px-3 py-2 font-mono text-[11px] uppercase tracking-wide text-st-review">
            stale — last good counts
          </div>
        )}

        <div className="rounded-[14px] border border-line-2 bg-surface-2 p-5">
          <div className="text-[11.5px] font-medium uppercase tracking-wide text-muted">Needs attention</div>
          <div className="mt-1 text-[40px] font-semibold leading-none tracking-tight text-fg">{needsAttention}</div>
          <p className="mt-2 text-[12.5px] text-muted">
            In progress, review, blocked, and memory waiting — not inventory totals.
          </p>
        </div>

        <div className="mt-4 space-y-2">
          <Attention
            label="In progress"
            value={inFlight}
            empty="No items in progress right now."
            to="/tracker"
          />
          <Attention
            label="In review"
            value={itemReview}
            empty="No items waiting for review."
            to="/tracker"
          />
          <Attention
            label="Blocked"
            value={blocked}
            empty="No blocked items — that is a looked-at zero, not an unread queue."
            to="/tracker"
          />
          <Attention
            label="Memory waiting for review"
            value={memoryQueue}
            empty="No shards waiting for review."
            to="/memory-review"
          />
        </div>

        <div className="mt-6">
          <div className="mb-2 text-[11px] font-medium uppercase tracking-wide text-faint">Inventory</div>
          <div className="grid grid-cols-2 gap-2 sm:grid-cols-4">
            <Inventory label="Items" value={itemsTotal} icon={<Boxes size={13} />} />
            <Inventory label="PRDs" value={prdTotal} />
            <Inventory label="Memory shards" value={shardTotal} accent="#a78bfa" />
            <Inventory label="Live agents" value={live} accent="#5fd07a" icon={<Users size={13} />} />
          </div>
        </div>

        <div className="mt-6 grid gap-3 sm:grid-cols-3">
          <Quick to="/triage" label="Triage" icon={<Radar size={15} />} desc="What came in, beside the in-flight work it would collide with." />
          <Quick to="/fleet.v1" label="Fleet.v1" icon={<Users size={15} />} desc="Who is working here right now, what they hold, and for how long." />
          <Quick to="/code" label="Code graph" icon={<Network size={15} />} desc="The structure agents describe as they work." />
        </div>
      </div>
    </div>
  );
}

function Inventory({
  label, value, icon, accent,
}: { label: string; value: number; icon?: ReactNode; accent?: string }) {
  return (
    <div className="rounded-[11px] border border-line-2 bg-surface-2 px-3 py-2.5">
      {icon && (
        <div className="mb-1 text-faint" style={{ color: accent }}>{icon}</div>
      )}
      <div className="text-[17px] font-semibold leading-none tracking-tight text-fg-2">{value}</div>
      <div className="mt-1 text-[10.5px] text-muted">{label}</div>
    </div>
  );
}

function Attention({ label, value, empty, to }: { label: string; value: number; empty: string; to?: string }) {
  const inner = (
    <>
      <div className="flex items-baseline justify-between gap-3">
        <span className="text-[13px] font-semibold">{label}</span>
        <span className="font-mono text-[12px] text-fg-2">{value}</span>
      </div>
      {value === 0 && <p className="mt-1 text-[12px] text-muted">{empty}</p>}
    </>
  );
  if (to) {
    return (
      <Link
        to={to}
        className="block rounded-[12px] border border-line-2 bg-surface-2 px-3.5 py-2.5 transition-colors hover:border-line-hover"
      >
        {inner}
      </Link>
    );
  }
  return (
    <div className="rounded-[12px] border border-line-2 bg-surface-2 px-3.5 py-2.5">
      {inner}
    </div>
  );
}

function Quick({ to, label, icon, desc }: { to: string; label: string; icon: ReactNode; desc: string }) {
  return (
    <Link
      to={to}
      className="rounded-[13px] border border-line bg-surface-2 p-4 transition-colors hover:border-line-hover"
    >
      <div className="flex items-center gap-2.5">
        <span className="text-accent">{icon}</span>
        <span className="text-[13.5px] font-semibold">{label}</span>
      </div>
      <p className="mt-1.5 text-[11.5px] leading-relaxed text-muted">{desc}</p>
    </Link>
  );
}
