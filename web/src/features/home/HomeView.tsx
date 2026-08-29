import { Boxes, CircleDot, Network, Radar, TriangleAlert, Users } from "lucide-react";
import type { ReactNode } from "react";
import { Link } from "react-router-dom";

import { useProjectCtx } from "@/features/ProjectContext";
import { useCounts, useDashboard, useFleet } from "@/lib/queries";

/**
 * Self-host Home (GRPH-P28 D2). KPIs come from the dashboard payload and the shell
 * counts endpoint — never from fetching items/shards to call `.length` (GRPH-431).
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
      <div className="p-6">
        <h1 className="text-[18px] font-semibold tracking-tight">Home</h1>
        <p className="mt-0.5 text-[12.5px] text-muted">Project health at a glance.</p>
        <div className="mt-6 grid grid-cols-2 gap-3 sm:grid-cols-3 lg:grid-cols-6" aria-busy="true">
          {Array.from({ length: 6 }).map((_, i) => (
            <div key={i} className="h-[88px] animate-pulse rounded-[12px] border border-line-2 bg-surface-2" />
          ))}
        </div>
      </div>
    );
  }

  if (firstFailed) {
    return (
      <div className="p-6">
        <h1 className="text-[18px] font-semibold tracking-tight">Home</h1>
        <p className="mt-4 rounded-[12px] border border-st-blocked/30 bg-st-blocked/[0.06] px-3.5 py-3 text-[13px] text-st-blocked">
          counts unavailable
        </p>
      </div>
    );
  }

  const d = dash.data;
  const c = counts.data;
  const live = fleet.data?.online ?? 0;
  const review = c?.review ?? 0;
  const blocked = d?.blocked_count ?? 0;
  const inFlight = d?.in_progress_count ?? c?.items_in_progress ?? 0;

  return (
    <div className="flex h-full min-h-0 flex-col">
      <div className="flex-none border-b border-line px-5 py-4">
        <h1 className="text-[18px] font-semibold tracking-tight">Home</h1>
        <p className="mt-0.5 text-[12.5px] text-muted">
          Project health at a glance — items, memory, and who is in flight.
        </p>
      </div>

      <div className="min-h-0 flex-1 overflow-y-auto p-6">
        {stale && (
          <div className="mb-4 rounded-[11px] border border-st-review/30 bg-st-review/[0.06] px-3 py-2 font-mono text-[11px] uppercase tracking-wide text-st-review">
            stale — last good counts
          </div>
        )}

        <div className="grid grid-cols-2 gap-3 sm:grid-cols-3 lg:grid-cols-6">
          <Kpi label="Items" value={d?.items_total ?? c?.items ?? 0} icon={<Boxes size={15} />} />
          <Kpi label="In progress" value={inFlight} icon={<CircleDot size={15} />} accent="#c6f24e" />
          <Kpi label="Blocked" value={blocked} icon={<TriangleAlert size={15} />} accent="#ff6b6b" />
          <Kpi label="PRDs" value={d?.prd_count ?? 0} />
          <Kpi label="Memory shards" value={d?.shard_count ?? 0} accent="#a78bfa" />
          <Kpi label="Live agents" value={live} accent="#5fd07a" icon={<Users size={15} />} />
        </div>

        <div className="mt-6 space-y-2">
          <Attention
            label="Blocked items"
            value={blocked}
            empty="No blocked items — that is a looked-at zero, not an unread queue."
          />
          <Attention
            label="Memory waiting for review"
            value={review}
            empty="No shards waiting for review."
          />
          <Attention
            label="Agents in flight"
            value={inFlight}
            empty="No items in progress right now."
          />
        </div>

        <div className="mt-6 grid gap-3 sm:grid-cols-3">
          <Quick to="/triage" label="Triage" icon={<Radar size={15} />} desc="What came in, beside the in-flight work it would collide with." />
          <Quick to="/fleet" label="Fleet" icon={<Users size={15} />} desc="Who is working here right now, what they hold, and for how long." />
          <Quick to="/code" label="Code graph" icon={<Network size={15} />} desc="The structure agents describe as they work." />
        </div>
      </div>
    </div>
  );
}

function Kpi({
  label, value, icon, accent,
}: { label: string; value: number; icon?: ReactNode; accent?: string }) {
  return (
    <div className="rounded-[12px] border border-line-2 bg-surface-2 p-3.5">
      {icon && (
        <div className="mb-2 text-muted" style={{ color: accent }}>{icon}</div>
      )}
      <div className="text-[22px] font-semibold leading-none tracking-tight text-fg">{value}</div>
      <div className="mt-1.5 text-[11.5px] text-muted">{label}</div>
    </div>
  );
}

function Attention({ label, value, empty }: { label: string; value: number; empty: string }) {
  return (
    <div className="rounded-[12px] border border-line-2 bg-surface-2 px-3.5 py-2.5">
      <div className="flex items-baseline justify-between gap-3">
        <span className="text-[13px] font-semibold">{label}</span>
        <span className="font-mono text-[12px] text-fg-2">{value}</span>
      </div>
      {value === 0 && <p className="mt-1 text-[12px] text-muted">{empty}</p>}
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
