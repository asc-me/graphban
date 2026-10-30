import {
  Activity,
  BarChart3,
  BookMarked,
  Bot,
  Inbox,
  ListChecks,
  Network,
  Radar,
  Users,
  X,
} from "lucide-react";
import type { ReactNode } from "react";
import { useState } from "react";
import { Link, useLocation } from "react-router-dom";

import {
  FETCH_FAILED,
  KpiGridSkeleton,
  PlannerEmpty,
  PlannerError,
} from "@/components/planner/PlannerStates";
import { useProjectCtx } from "@/features/ProjectContext";
import { STATUS_META, STATUS_ORDER, TYPE_META } from "@/lib/meta";
import {
  useCodeMap,
  useCounts,
  useDashboard,
  useGalaxy,
  useOrgs,
} from "@/lib/queries";
import { consoleHref, orgPath, projectPath, tagFromPath } from "@/lib/routes";
import type { DashboardData, GalaxyEdge, RequestType, ShellCounts } from "@/lib/types";

/**
 * Project home for both deployments (GRPH-1067).
 *
 * Hosted `/p/:tag` and self-host `/home` render this one view. The route decides the
 * two things that actually differ: link shape (`projectPath` vs a flat path) and the
 * galaxy, which a self-host build has no org to draw. Self-host drops the "Needs
 * attention" focal number on purpose — that reversal of GRPH-925 was decided
 * 2026-10-22, and it is the layout, not a regression.
 *
 * Counts come from `useDashboard` and `useCounts`, plus the code-map `node_count` and
 * the fleet `online` integer. None of them is a list `.length` (GRPH-431). A read that
 * has not answered is `—`, never `0`.
 */

const AGENT_INFO_DISMISSED_KEY = "al.dashboard.agentLoopInfoDismissed";

type Read<T> = {
  isPending: boolean;
  isError: boolean;
  data: T | undefined;
  refetch: () => unknown;
};

function waiting(q: { isPending: boolean; data: unknown }): boolean {
  return q.isPending && q.data === undefined;
}

function missed(q: { isError: boolean; data: unknown }): boolean {
  return q.isError && q.data === undefined;
}

/** `data && isError`: TanStack Query keeps the last payload and flags the refresh. */
function lastGood(q: { isError: boolean; data: unknown }): boolean {
  return Boolean(q.data) && q.isError;
}

/** A successful numeric field. Missing, failed, or non-numeric is unknown — not zero. */
function num(ok: boolean, value: unknown): number | null {
  if (!ok || typeof value !== "number" || !Number.isFinite(value)) return null;
  return value;
}

function firstNum(...values: Array<number | null>): number | null {
  for (const value of values) if (value !== null) return value;
  return null;
}

export function ProjectHome() {
  const { pathname } = useLocation();
  const { active } = useProjectCtx();
  const projectId = active?.id ?? "";
  const hosted = tagFromPath(pathname) !== null && !!active?.tag;
  const to = (sub: string) =>
    hosted && active?.tag ? projectPath(active.tag, sub) : `/${sub.replace(/^\/+/, "")}`;

  const dashQ = useDashboard(projectId) as Read<DashboardData>;
  const countsQ = useCounts(projectId) as Read<ShellCounts>;
  const mapQ = useCodeMap(projectId) as Read<{ node_count?: number }>;
  const orgsQ = useOrgs(hosted);
  const galaxyQ = useGalaxy(hosted ? orgsQ.data?.[0]?.id : undefined);

  if (!active) return null;

  const reads = [dashQ, countsQ, mapQ];
  const settling = reads.some(waiting);
  const failed = reads.filter(missed);
  // A failed refresh after a good read. The numbers below are that read, not a new one.
  const stale = reads.some(lastGood);
  const dash = dashQ.data;
  const counts = countsQ.data;
  const dashOk = dash !== undefined;
  const countsOk = counts !== undefined;
  const mapOk = mapQ.data !== undefined;

  // Dashboard wins the figure. The shell count is the fallback when that field was
  // not in a successful payload, and it is also its own measurement: a dashboard 0
  // must not hide a non-zero shell count inside the empty-project check.
  const dashItems = num(dashOk, dash?.items_total);
  const countItems = num(countsOk, counts?.items);
  const items = firstNum(dashItems, countItems);
  const dashInFlight = num(dashOk, dash?.in_progress_count);
  const countInFlight = num(countsOk, counts?.items_in_progress);
  const inFlight = firstNum(dashInFlight, countInFlight);
  const blocked = num(dashOk, dash?.blocked_count);
  const shards = num(dashOk, dash?.shard_count);
  const nodes = num(mapOk, mapQ.data?.node_count);
  // The server's one "live" definition, the same field the sidebar badge reads.
  const online = num(countsOk, counts?.live_agents);
  const requests = num(dashOk, dash?.requests_total);
  const prds = num(dashOk, dash?.prd_count);
  const triageNew = num(countsOk, counts?.triage_new);
  const lessons = num(countsOk, counts?.lessons_published);
  const review = num(countsOk, counts?.review);

  // Page-wide empty only when every read answered and every one of them is zero.
  // A hole in that set is "not measured", which must not wear the empty project's clothes.
  // mcp_calls stays out: the dashboard fills it from the deployment's total, so one
  // call anywhere would hide an empty project. It is not a measurement of this one.
  const measured = [
    dashItems, countItems, dashInFlight, countInFlight,
    blocked, shards, nodes, online, requests, prds, triageNew, lessons, review,
  ];
  const fresh =
    measured.every((n) => n === 0);

  const retryFailed = () => {
    failed.forEach((q) => void q.refetch());
  };

  return (
    <div className="max-w-[1180px] px-6 pb-16 pt-6">
      <div className="flex flex-wrap items-center gap-2.5">
        <span className="h-2.5 w-2.5 rounded-[3px]" style={{ background: active.accent }} />
        <h1 className="text-[19px] font-semibold tracking-[-0.3px]">{active.name}</h1>
        <span className="rounded border border-line-2 px-1.5 py-px font-mono text-[10px] text-muted">
          {active.tag}
        </span>
      </div>
      {active.description && (
        <p className="mt-1.5 max-w-[76ch] text-body leading-relaxed text-muted">{active.description}</p>
      )}

      {stale && (
        <div className="mt-4 rounded-[11px] border border-st-review/30 bg-st-review/[0.06] px-3 py-2 font-mono text-small uppercase tracking-wide text-st-review">
          stale — last good counts
        </div>
      )}

      {settling ? (
        <div className="mt-4">
          <KpiGridSkeleton />
          <JumpTo to={to} pending />
        </div>
      ) : fresh ? (
        <>
          <AgentLoopInfo to={to} />
          <PlannerEmpty
            title="Nothing to summarise yet"
            description="Every count that was read came back zero. That is an empty project, not a measured one — file the first item and the counts start."
          />
        </>
      ) : (
        <>
          <StatGrid
            items={items}
            inFlight={inFlight}
            blocked={blocked}
            shards={shards}
            nodes={nodes}
            online={online}
          />

          {failed.length > 0 && (
            <PlannerError
              message={`${FETCH_FAILED} A count showing — was not read, and is not a zero.`}
              onRetry={retryFailed}
            />
          )}

          {mapOk && nodes === 0 && <NoGraphYet tag={active.tag} />}

          <AgentLoopInfo to={to} />

          <div className="grid grid-cols-1 gap-6 lg:grid-cols-[1.4fr_1fr]">
            <div className="space-y-6">
              <Section title="Items" subtitle="Where the stream stands.">
                <CountOr
                  value={num(dashOk, dash?.items_total)}
                  zero={
                    <EmptyLine
                      text="No items yet."
                      action="Create the first one in Tracker →"
                      to={to("tracker")}
                    />
                  }
                  ready={<StatusBar data={dash!} />}
                />
              </Section>
              <Section title="Requests" subtitle="From the public form, by type.">
                <CountOr
                  value={requests}
                  zero={
                    <EmptyLine
                      text="No requests yet."
                      action="Add the feedback widget →"
                      to={to("feedback-kit")}
                    />
                  }
                  ready={<TypeBars data={dash!} />}
                />
              </Section>
            </div>
            <Section title="Recent activity" subtitle="Latest moves in the stream.">
              <CountOr
                value={dash ? dash.recent_items.length : null}
                zero={<p className="text-body leading-relaxed text-muted">Nothing has happened here yet.</p>}
                ready={dash ? <ActivityList items={dash.recent_items} href={to("tracker")} /> : null}
              />
            </Section>
          </div>

          <JumpTo
            to={to}
            inFlight={inFlight}
            triageNew={triageNew}
            prds={prds}
            nodes={nodes}
            review={review}
            lessons={lessons}
            online={online}
          />

          {hosted && (
            <Dependencies
              dependsOn={(galaxyQ.data?.edges ?? []).filter((e) => e.src === projectId)}
              dependedBy={(galaxyQ.data?.edges ?? []).filter((e) => e.dst === projectId)}
              nameOf={(id) => galaxyQ.data?.nodes.find((n) => n.id === id)?.tag ?? id}
              knowsGalaxy={galaxyQ.data !== undefined}
              failed={galaxyQ.isError && galaxyQ.data === undefined}
              onRetry={() => void galaxyQ.refetch()}
            />
          )}
        </>
      )}
    </div>
  );
}

function toneFor(kind: "flight" | "blocked" | "live", value: number | null): string | undefined {
  if (value === null || value <= 0) return undefined;
  if (kind === "flight") return "text-accent";
  if (kind === "blocked") return "text-st-blocked";
  return "text-st-done";
}

function Figure({ value, tone }: { value: number | null; tone?: string }) {
  const unknown = value === null;
  return (
    <div
      className={`mt-2 text-[22px] font-semibold leading-none tracking-tight ${unknown ? "text-faint" : (tone ?? "text-fg")}`}
      title={unknown ? "Not read — the request failed" : undefined}
    >
      {unknown ? "—" : value.toLocaleString()}
    </div>
  );
}

function StatGrid({
  items, inFlight, blocked, shards, nodes, online,
}: {
  items: number | null;
  inFlight: number | null;
  blocked: number | null;
  shards: number | null;
  nodes: number | null;
  online: number | null;
}) {
  const cells: { label: string; value: number | null; tone?: string }[] = [
    { label: "Items", value: items },
    { label: "In flight", value: inFlight, tone: toneFor("flight", inFlight) },
    { label: "Blocked", value: blocked, tone: toneFor("blocked", blocked) },
    { label: "Memory shards", value: shards },
    { label: "Graph nodes", value: nodes },
    { label: "Agents live", value: online, tone: toneFor("live", online) },
  ];
  return (
    <div className="mt-4 grid grid-cols-2 gap-px overflow-hidden rounded-[13px] border border-line bg-line sm:grid-cols-3 lg:grid-cols-6">
      {cells.map((cell) => (
        <div key={cell.label} className="bg-surface px-3.5 py-3">
          <span className="block font-mono text-[10px] uppercase tracking-[0.07em] text-faint-2">
            {cell.label}
          </span>
          <Figure value={cell.value} tone={cell.tone} />
        </div>
      ))}
    </div>
  );
}

function CountOr({ value, zero, ready }: { value: number | null; zero: ReactNode; ready: ReactNode }) {
  if (value === null) {
    return (
      <p className="text-body text-faint" title="Not read — the request failed">
        —
      </p>
    );
  }
  if (value === 0) return zero;
  return ready;
}

function EmptyLine({ text, action, to }: { text: string; action: string; to: string }) {
  return (
    <p className="text-body leading-relaxed text-muted">
      {text}{" "}
      <Link to={to} className="text-st-next transition-colors hover:text-fg">
        {action}
      </Link>
    </p>
  );
}

function ActivityList({
  items,
  href,
}: {
  items: DashboardData["recent_items"];
  href: string;
}) {
  return (
    <div className="divide-y divide-line-2">
      {items.map((it) => (
        <Link
          key={it.id}
          to={href}
          className="flex items-center gap-2.5 px-1 py-2 transition-colors first:pt-0 last:pb-0 hover:bg-surface-2"
        >
          <span
            className="h-1.5 w-1.5 flex-none rounded-full"
            style={{ background: STATUS_META[it.status].color }}
          />
          <span className="w-[46px] flex-none font-mono text-[10px] text-faint">{it.id}</span>
          <span className="min-w-0 flex-1 truncate text-body text-fg-2">{it.title}</span>
          <span className="flex-none font-mono text-[10px] text-faint-2">{it.date}</span>
        </Link>
      ))}
    </div>
  );
}

function SectionHead({ title, subtitle }: { title: string; subtitle: string }) {
  return (
    <div className="mb-2.5 flex flex-wrap items-baseline gap-x-1.5">
      <h2 className="text-[13.5px] font-semibold tracking-[-0.2px]">{title}</h2>
      <span aria-hidden className="text-faint">·</span>
      <p className="text-[12px] text-muted">{subtitle}</p>
    </div>
  );
}

/** The heading sits outside the card; the card holds only the section's content. */
function Section({ title, subtitle, children }: { title: string; subtitle: string; children: ReactNode }) {
  return (
    <section>
      <SectionHead title={title} subtitle={subtitle} />
      <div className="rounded-[14px] border border-line-2 bg-surface/40 p-4">{children}</div>
    </section>
  );
}

function StatusBar({ data }: { data: DashboardData }) {
  const total = Math.max(1, data.items_total);
  const present = STATUS_ORDER.filter((s) => (data.items_by_status[s] ?? 0) > 0);
  return (
    <div>
      <div className="flex h-5 gap-[2px]">
        {present.map((s) => {
          const count = data.items_by_status[s] ?? 0;
          return (
            <div
              key={s}
              className="h-full rounded-[3px]"
              style={{ background: STATUS_META[s].color, width: `${(count / total) * 100}%`, minWidth: 8 }}
              title={`${STATUS_META[s].label}: ${count}`}
            />
          );
        })}
      </div>
      <div className="mt-3 flex flex-wrap gap-x-5 gap-y-1.5">
        {STATUS_ORDER.map((s) => (
          <div key={s} className="flex items-center gap-1.5 text-[12px]">
            <span className="h-2 w-2 rounded-[2px]" style={{ background: STATUS_META[s].color }} />
            <span className="text-muted">{STATUS_META[s].label}</span>
            <span className="font-mono text-small text-fg-2">{data.items_by_status[s] ?? 0}</span>
          </div>
        ))}
      </div>
    </div>
  );
}

function titleCase(label: string): string {
  return label.charAt(0).toUpperCase() + label.slice(1).toLowerCase();
}

function TypeBars({ data }: { data: DashboardData }) {
  const max = Math.max(1, ...Object.values(data.requests_by_type));
  const types = Object.keys(TYPE_META) as RequestType[];
  return (
    <div className="space-y-2">
      {types.map((t) => {
        const count = data.requests_by_type[t] ?? 0;
        return (
          <div key={t} className="flex items-center gap-3">
            <span
              className="w-[92px] flex-none text-[12px] text-fg-2"
            >
              {titleCase(TYPE_META[t].label)}
            </span>
            <div data-testid="request-track" className="h-2.5 flex-1 rounded-[3px] bg-surface-2">
              <div
                className="h-full rounded-[3px]"
                style={{
                  background: TYPE_META[t].color,
                  width: `${(count / max) * 100}%`,
                  minWidth: count ? 6 : 0,
                }}
              />
            </div>
            <span className="w-6 flex-none text-right font-mono text-small text-fg-2">{count}</span>
          </div>
        );
      })}
    </div>
  );
}

type JumpProps = {
  to: (sub: string) => string;
  pending?: boolean;
  inFlight?: number | null;
  triageNew?: number | null;
  prds?: number | null;
  nodes?: number | null;
  review?: number | null;
  lessons?: number | null;
  online?: number | null;
};

function isLive(value: number | null | undefined): boolean {
  return typeof value === "number" && value > 0;
}

function metaText(value: number | null | undefined, phrase: (n: number) => string): ReactNode {
  if (value === null || value === undefined) return "—";
  return phrase(value);
}

function JumpTo({
  to, pending, inFlight, triageNew, prds, nodes, review, lessons, online,
}: JumpProps) {
  const tiles: { sub: string; label: string; icon: ReactNode; meta: ReactNode; live: boolean }[] = [
    { sub: "tracker", icon: <ListChecks size={14} />, label: "Tracker", meta: metaText(inFlight ?? null, (n) => `${n} in flight`), live: isLive(inFlight) },
    { sub: "triage", icon: <Radar size={14} />, label: "Triage", meta: metaText(triageNew ?? null, (n) => `${n} new`), live: isLive(triageNew) },
    { sub: "prds", icon: <BarChart3 size={14} />, label: "PRDs", meta: metaText(prds ?? null, (n) => `${n} docs`), live: isLive(prds) },
    {
      sub: "code",
      icon: <Network size={14} />,
      label: "Code graph",
      meta: nodes === 0
        ? <span className="text-st-review">not pushed</span>
        : metaText(nodes ?? null, (n) => `${n} nodes`),
      live: isLive(nodes),
    },
    { sub: "memory", icon: <Inbox size={14} />, label: "Memory", meta: metaText(review ?? null, (n) => `${n} to review`), live: isLive(review) },
    { sub: "lessons", icon: <BookMarked size={14} />, label: "Lessons", meta: metaText(lessons ?? null, (n) => `${n} published`), live: isLive(lessons) },
    // One server integer. Filtering `agents` here would be a list length (GRPH-431).
    { sub: "fleet", icon: <Users size={14} />, label: "Fleet", meta: metaText(online ?? null, (n) => `${n} online`), live: isLive(online) },
    { sub: "live", icon: <Activity size={14} />, label: "Live", meta: metaText(online ?? null, (n) => `${n} on now`), live: isLive(online) },
  ];
  return (
    <section className="mt-6" aria-label="Jump to">
      <SectionHead title="Jump to" subtitle="Live counts from each surface." />
      <div className="grid grid-cols-2 gap-2 sm:grid-cols-3 lg:grid-cols-5">
        {tiles.map((tile) => (
          <Link
            key={tile.sub}
            to={to(tile.sub)}
            className="flex items-center gap-2 rounded-[11px] border border-line bg-surface-2 px-3 py-2 transition-colors hover:border-line-hover"
          >
            <span className="flex-none text-muted">{tile.icon}</span>
            <span className="min-w-0 flex-1 truncate text-[13px] font-semibold text-fg">{tile.label}</span>
            {!pending && (
              <span className={`flex-none font-mono text-[10.5px] ${tile.live ? "text-accent" : "text-muted"}`}>{tile.meta}</span>
            )}
          </Link>
        ))}
      </div>
    </section>
  );
}

function AgentLoopInfo({ to }: { to: (sub: string) => string }) {
  const [dismissed, setDismissed] = useState(() => localStorage.getItem(AGENT_INFO_DISMISSED_KEY) === "1");
  if (dismissed) return null;
  const tools = to("mcp-tools");
  return (
    <div className="relative mb-6 mt-4 rounded-[14px] border border-purple/30 bg-purple/10 p-4">
      <button
        type="button"
        aria-label="Dismiss"
        className="absolute right-3 top-3 text-faint transition-colors hover:text-fg-2"
        onClick={() => {
          localStorage.setItem(AGENT_INFO_DISMISSED_KEY, "1");
          setDismissed(true);
        }}
      >
        <X size={14} />
      </button>
      <div className="flex items-start gap-3">
        <span className="mt-0.5 flex-none text-purple">
          <Bot size={16} />
        </span>
        <div className="min-w-0">
          <div className="text-[13px] font-semibold text-fg">Put agents on the backlog</div>
          <p className="mt-1 text-body leading-relaxed text-muted">
            Any MCP-connected agent can work the tracker as a queue:{" "}
            <code className="font-mono text-small text-fg-2">claim_cluster → work → update_item → review</code>. Claims
            are atomic and a cluster reserves the files it touches, so parallel agents never collide. Finished work goes
            to <code className="font-mono text-small text-fg-2">review</code>, and any other agent picks it up with{" "}
            <code className="font-mono text-small text-fg-2">claim_review</code> — no agent can sign off its own.
          </p>
          <div className="mt-2 flex flex-wrap gap-x-4 gap-y-1 text-[12px]">
            <Link to="/settings" className="text-purple-2 transition-colors hover:text-fg">
              Connect an agent →
            </Link>
            <Link to={tools} className="text-purple-2 transition-colors hover:text-fg">
              Browse MCP tools →
            </Link>
          </div>
        </div>
      </div>
    </div>
  );
}

function NoGraphYet({ tag }: { tag: string }) {
  return (
    <div className="mt-4 rounded-[13px] border border-st-review/30 bg-st-review/[0.06] px-3.5 py-3">
      <div className="text-[13px] font-semibold text-st-review">
        No deployment has pushed a code graph to this project
      </div>
      <p className="mt-1 max-w-[72ch] text-[12px] leading-relaxed text-st-review/80">
        Empty because nothing has arrived, not because{" "}
        <span className="font-mono text-fg-2">{tag}</span> has no structure. A linked box
        builds the graph locally and pushes summaries up — run{" "}
        <code className="font-mono text-[11.5px]">graphban link</code> on the machine with
        the checkout.
      </p>
    </div>
  );
}

/**
 * Hosted only. Three answers, and they are not interchangeable: the read failed, the
 * read has not happened, and the read happened and found nothing.
 */
function Dependencies({
  dependsOn,
  dependedBy,
  nameOf,
  knowsGalaxy,
  failed,
  onRetry,
}: {
  dependsOn: GalaxyEdge[];
  dependedBy: GalaxyEdge[];
  nameOf: (id: string) => string;
  knowsGalaxy: boolean;
  failed: boolean;
  onRetry: () => void;
}) {
  if (failed) {
    return (
      <section className="mt-5 rounded-[13px] border border-line bg-surface-2 px-4 pb-1 pt-4">
        <h2 className="text-lead font-semibold">Cross-project dependencies</h2>
        <PlannerError
          message={`${FETCH_FAILED} What depends on this project was not read, so this panel has no answer — not even "nothing".`}
          onRetry={onRetry}
        />
      </section>
    );
  }

  if (!knowsGalaxy) return null;

  return (
    <section className="mt-5 rounded-[13px] border border-line bg-surface-2 p-4">
      <div className="flex items-center gap-2.5">
        <h2 className="flex-1 text-lead font-semibold">Cross-project dependencies</h2>
        <a href={consoleHref(orgPath("galaxy"))} className="text-[12px] text-st-next transition-colors hover:text-fg">
          Open in galaxy →
        </a>
      </div>

      <div className="mt-3 grid gap-4 sm:grid-cols-2">
        <Direction
          title="DEPENDS ON"
          edges={dependsOn}
          other={(e) => nameOf(e.dst)}
          empty="Nothing internal — every dependency resolved to an external package."
        />
        <Direction
          title="DEPENDED ON BY"
          edges={dependedBy}
          other={(e) => nameOf(e.src)}
          empty="No sibling repo declares this one. Changes here reach nothing else in the org."
        />
      </div>
    </section>
  );
}

function Direction({
  title,
  edges,
  other,
  empty,
}: {
  title: string;
  edges: GalaxyEdge[];
  other: (e: GalaxyEdge) => string;
  empty: string;
}) {
  return (
    <div>
      <div className="font-mono text-[9px] uppercase tracking-[0.07em] text-faint-2">{title}</div>
      {edges.length === 0 ? (
        <p className="mt-1.5 text-[11.5px] leading-relaxed text-muted">{empty}</p>
      ) : (
        <div className="mt-1.5 flex flex-col gap-1.5">
          {edges.map((e) => (
            <div
              key={e.id}
              className={`flex items-center gap-2 rounded-[7px] border px-2 py-1.5 ${
                e.fresh ? "border-line" : "border-dashed border-line opacity-60"
              }`}
            >
              <span className="font-mono text-small text-fg-2">{other(e)}</span>
              <span className="font-mono text-meta text-faint">
                {e.evidence.length} file{e.evidence.length === 1 ? "" : "s"}
              </span>
              {!e.fresh && (
                <span className="font-mono text-[9px] uppercase tracking-[0.05em] text-st-review">stale</span>
              )}
            </div>
          ))}
        </div>
      )}
    </div>
  );
}
