import { Boxes, Network, Server, Users } from "lucide-react";
import { Link } from "react-router-dom";

import { useOrgOverview, useOrgs } from "@/lib/queries";
import { adminPath, projectPath } from "@/lib/routes";
import type { OrgOverviewProject } from "@/lib/types";

/**
 * Screen 1 — Org overview (PRD-21 D2).
 *
 * The org's first cross-project read. Before it, an organisation with seven repos was
 * seven visits to the same single-project app, and the question an org exists to answer —
 * how are these projects doing, together — had no screen.
 *
 * **A join, not a new write path.** Every number here already exists in a table. A figure
 * with no query behind it does not belong on this page.
 *
 * The empty states are the design, not an afterthought. A brand-new org and an org whose
 * boxes have all stopped pushing are different facts, and rendering them the same way is
 * the failure this product is built to avoid — so `never` and `live` are separate words,
 * and a project that has never synced is **shown**, not filtered out. Omitting it would
 * shrink the org and hide precisely the project that needs attention.
 */
function relTime(iso: string | null): string {
  if (!iso) return "never";
  const secs = Math.max(0, (Date.now() - new Date(iso).getTime()) / 1000);
  if (secs < 90) return "just now";
  if (secs < 3600) return `${Math.round(secs / 60)}m ago`;
  if (secs < 86400) return `${Math.round(secs / 3600)}h ago`;
  return `${Math.round(secs / 86400)}d ago`;
}

function Stat({ icon, label, value, sub }: {
  icon: React.ReactNode; label: string; value: string | number; sub?: string;
}) {
  return (
    <div className="rounded-[13px] border border-line bg-surface p-4">
      <div className="flex items-center gap-2 font-mono text-[10px] uppercase tracking-[0.07em] text-faint">
        {icon}
        {label}
      </div>
      <div className="mt-2 text-[22px] font-semibold leading-none">{value}</div>
      {sub && <div className="mt-1.5 text-[11.5px] text-muted">{sub}</div>}
    </div>
  );
}

function SyncPill({ project }: { project: OrgOverviewProject }) {
  const never = project.sync === "never";
  return (
    <span
      title={never ? "no deployment has ever pushed to this project" : undefined}
      className={`inline-flex items-center gap-1.5 rounded-full border px-2 py-0.5 font-mono text-[9px] uppercase tracking-[0.06em] ${
        never
          ? "border-st-review/30 bg-st-review/[0.07] text-st-review"
          : "border-st-done/30 bg-st-done/[0.07] text-st-done"
      }`}
    >
      <span className="h-1.5 w-1.5 rounded-full bg-current" />
      {never ? "never synced" : `pushed ${relTime(project.last_push_at)}`}
    </span>
  );
}

export function OrgOverviewView() {
  const { data: orgs = [] } = useOrgs();
  const org = orgs[0] ?? null;
  const { data, isLoading } = useOrgOverview(org?.id);

  if (isLoading || !data) {
    return <div className="p-8 text-[13px] text-muted">Loading the organization…</div>;
  }

  const { projects, totals, usage, limits } = data;
  const callCap = limits.max_calls_per_month ?? 0;
  const calls = usage.calls_this_month ?? 0;

  // A brand-new org has exactly one useful thing to say, and it is not a table of zeroes.
  if (projects.length === 0) {
    return (
      <div className="max-w-[720px] px-6 pb-16 pt-5">
        <h1 className="text-[17px] font-semibold">{org?.name ?? "Your organization"}</h1>
        <div className="mt-5 rounded-[13px] border border-line bg-surface p-6">
          <h2 className="text-[14px] font-semibold">Link your first deployment</h2>
          <p className="mt-2 max-w-[62ch] text-[12.5px] leading-relaxed text-muted">
            This organization has no projects yet. A project fills up when a local
            deployment pushes its code graph here, so the next step is minting a sync
            credential and running <code className="font-mono text-[11.5px]">graphban link</code> on
            the box that holds the repo.
          </p>
          <Link
            to={adminPath("deployments")}
            className="mt-4 inline-flex items-center gap-1.5 rounded-md bg-accent px-3 py-1.5 text-[12.5px] font-medium text-black hover:bg-accent-2"
          >
            <Server size={14} /> Mint a sync credential
          </Link>
        </div>
      </div>
    );
  }

  return (
    <div className="max-w-[1180px] px-6 pb-16 pt-5">
      <div className="flex flex-wrap items-center gap-2.5">
        <h1 className="text-[17px] font-semibold">{org?.name ?? "Organization"}</h1>
        {data.plan && (
          <span className="rounded-full border border-line px-2 py-0.5 font-mono text-[9px] uppercase tracking-[0.06em] text-muted">
            {data.plan}
          </span>
        )}
      </div>

      <div className="mt-5 grid grid-cols-2 gap-3 lg:grid-cols-4">
        <Stat icon={<Boxes size={12} />} label="Projects" value={totals.projects}
              sub={totals.never_synced ? `${totals.never_synced} never synced` : "all have pushed"} />
        <Stat icon={<Users size={12} />} label="Open items" value={totals.open_items}
              sub={`${totals.claims} claimed by an agent`} />
        <Stat icon={<Network size={12} />} label="Graph nodes" value={totals.nodes} />
        <Stat icon={<Server size={12} />} label="MCP calls" value={calls}
              sub={callCap ? `of ${callCap.toLocaleString()} this month` : undefined} />
      </div>

      <h2 className="mb-2.5 mt-8 font-mono text-[11px] uppercase tracking-wide text-faint">
        Projects
      </h2>
      <div className="overflow-hidden rounded-[13px] border border-line">
        {projects.map((p, i) => (
          <div
            key={p.id}
            className={`flex flex-wrap items-center gap-3 bg-surface px-4 py-3 ${
              i ? "border-t border-line-2" : ""
            }`}
          >
            <span className="h-2 w-2 shrink-0 rounded-full" style={{ background: p.accent }} />
            <Link to={projectPath(p.tag)} className="text-[13px] font-medium hover:text-accent">
              {p.name}
            </Link>
            <span className="font-mono text-[10.5px] text-faint">{p.tag}</span>
            <SyncPill project={p} />
            <span className="ml-auto flex items-center gap-4 font-mono text-[11px] text-muted">
              <span title="open items">{p.open_items} open</span>
              <span title="items an agent is holding right now">{p.claims.length} claimed</span>
              <span title="fresh code-graph nodes">{p.nodes} nodes</span>
            </span>
          </div>
        ))}
      </div>
    </div>
  );
}
