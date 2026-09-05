import * as React from "react";
import { useLocation, useSearchParams, Link } from "react-router-dom";

import { Avatar } from "@/components/ui/avatar";
import { useProjectCtx } from "@/features/ProjectContext";
import { cn } from "@/lib/cn";
import { useConfig, useLive, useLiveFeed } from "@/lib/queries";
import { projectPath } from "@/lib/routes";
import type {
  LiveAgent,
  LiveBoard,
  LiveDelegationRow,
  LiveFeedRow,
  LiveFileKind,
  LiveFileState,
  LiveUser,
} from "@/lib/types";

/** Same mapping as Fleet: role colour is the status that role produces. */
const ROLE_TONE: Record<string, string> = {
  planner: "text-[#b794f6] border-[#b794f6]/40",
  worker: "text-[color:var(--color-st-in_progress)] border-[color:var(--color-st-in_progress)]/40",
  reviewer: "text-[color:var(--color-st-review)] border-[color:var(--color-st-review)]/40",
  "all-in-one": "text-muted border-line-2",
};

/**
 * Observe Live (PRD-33): who is on this project right now.
 *
 * Reads GET /live. Does not join Fleet + presence + items on the client. A missing
 * measurement is a named third state — unreserved is not idle, unrecorded is not
 * "no PRs", Unattributed is not dropped.
 */
export function LiveView() {
  const { activeId, active } = useProjectCtx();
  const { data: config } = useConfig();
  const { pathname } = useLocation();
  const [params] = useSearchParams();
  const user = params.get("user");
  const { data, isLoading, isError } = useLive(activeId, user);
  const fleetTo = config?.hosted_mode && active?.tag
    ? projectPath(active.tag, "fleet")
    : "/fleet";

  if (isError) {
    return (
      <div className="flex h-full items-center justify-center text-[13px] text-muted">
        The live board could not be loaded.
      </div>
    );
  }
  if (isLoading || !data) {
    return (
      <div className="flex h-full items-center justify-center text-[13px] text-muted">
        Loading…
      </div>
    );
  }

  const payloadAgents = data.users.reduce((n, u) => n + u.agents.length, 0);
  const censusTotal = data.user_counts.reduce((n, c) => n + c.total, 0);
  const emptyProject = data.users.length === 0 && !user;
  const emptyFilter = data.users.length === 0 && !!user;

  return (
    <div className="flex h-full min-h-0 flex-col">
      <div className="flex flex-none items-center gap-4 border-b border-line px-5 py-4">
        <div>
          <h1 className="text-[18px] font-semibold tracking-tight">Live</h1>
          <p className="mt-0.5 text-[12.5px] text-muted">
            Who is on this project right now, what they hold, and whether a PR was recorded.
          </p>
        </div>
        <div className="ml-auto flex items-center gap-3">
          <RoleCounts byRole={data.by_role ?? {}} roles={data.roles ?? []} />
          <Link to={fleetTo} className="text-[12.5px] text-muted hover:text-fg-2">
            Fleet
          </Link>
        </div>
      </div>

      {data.truncated && (
        <div
          role="status"
          className="flex-none border-b border-st-review/30 bg-st-review/[0.06] px-5 py-2 font-mono text-[11px] text-st-review"
        >
          Showing {payloadAgents} of {data.total_agents} agents
        </div>
      )}

      <div className="flex flex-none flex-wrap items-center gap-1.5 border-b border-line px-5 py-2.5">
        <Chip
          to={pathname}
          active={!user}
          label="All"
          count={censusTotal}
        />
        {data.user_counts.map((c) => {
          const id = c.user_id ?? "unattributed";
          return (
            <Chip
              key={id}
              to={`${pathname}?user=${encodeURIComponent(id)}`}
              active={user === id}
              label={c.label}
              count={c.total}
            />
          );
        })}
      </div>

      <div className="min-h-0 flex-1 overflow-y-auto p-5">
        {emptyProject ? (
          <div className="mt-16 text-center text-[13px] text-muted">
            No agents have registered on this project.
          </div>
        ) : emptyFilter ? (
          <div className="mt-16 text-center text-[13px] text-muted">
            No agents for this person on this project.
          </div>
        ) : (
          <div className="mx-auto flex max-w-3xl flex-col gap-5">
            {data.users.map((u) => (
              <UserBlock key={u.user_id ?? "unattributed"} user={u} board={data} projectId={activeId} />
            ))}
          </div>
        )}
      </div>
    </div>
  );
}

function Chip({
  to, active, label, count,
}: { to: string; active: boolean; label: string; count: number }) {
  return (
    <Link
      to={to}
      aria-pressed={active}
      className={cn(
        "inline-flex items-center gap-1.5 rounded-lg border px-2.5 py-1 text-[12px] transition-colors",
        active
          ? "border-line-hover bg-surface-3 text-fg"
          : "border-line-2 bg-surface-2 text-muted hover:border-line-3 hover:text-fg-2",
      )}
    >
      {label}
      <span className="font-mono text-[10px] text-faint">{count}</span>
    </Link>
  );
}

/** PRD-33 D12 says fade, never hide. An offline agent that still HOLDS something — a
 *  holding, a file lease, an orphaned branch, a delegation nobody claimed — is the row that
 *  rule exists for, and it stays in view. An offline agent holding nothing is a dead process
 *  with nothing to show, and 130 of them buried the rows that matter on the deployed board
 *  (GRPH-708). Those collapse under a header that STATES the count; nothing is dropped. */
function holdsSomething(a: LiveAgent): boolean {
  const d = a.delegations;
  return a.holdings.length > 0 || a.files.length > 0 || a.branch_orphaned
    || (!!d && d.open + d.expired > 0);
}

function UserBlock({ user, board, projectId }: { user: LiveUser; board: LiveBoard; projectId?: string }) {
  const [showQuiet, setShowQuiet] = React.useState(false);
  const shown = user.agents.filter((a) => a.state !== "offline" || holdsSomething(a));
  const quiet = user.agents.filter((a) => a.state === "offline" && !holdsSomething(a));
  const row = (a: LiveAgent) => (
    <AgentRow
      key={a.id}
      agent={a}
      servedAt={board.served_at}
      projectId={projectId}
      intervalMs={board.heartbeat_interval_seconds * 1000}
    />
  );
  return (
    <section>
      <div className="mb-2 flex items-center gap-2.5">
        <Avatar
          initials={user.initials || "?"}
          color={user.color || "#8b949e"}
          size={22}
        />
        <h2 className="text-[13.5px] font-semibold">{user.label}</h2>
        <span className="font-mono text-[10px] text-faint">
          {user.online}/{user.total}
        </span>
      </div>
      {/* PRD-34 D15: calls that named no agent are counted on the credential, by key name,
          so the operator knows which harness to fix. Never assigned to an agent. */}
      {(user.unattributed_by_key ?? []).map((k) => (
        <div
          key={k.key}
          role="note"
          className="mb-1.5 rounded-[9px] border border-dashed border-line-2 px-3 py-1.5 text-[11.5px] text-muted"
        >
          {k.calls} {k.calls === 1 ? "call" : "calls"} on credential{" "}
          <span className="font-mono text-fg-2">{k.key}</span> not attributable to an agent
        </div>
      ))}
      <div className="flex flex-col gap-1.5">
        {shown.map(row)}
        {quiet.length > 0 && (
          <div className="rounded-[11px] border border-dashed border-line-2 px-3.5 py-2 text-[12px]">
            <button
              type="button"
              aria-expanded={showQuiet}
              onClick={() => setShowQuiet((v) => !v)}
              className="text-muted hover:text-fg-2"
            >
              {quiet.length} offline {quiet.length === 1 ? "agent" : "agents"} holding nothing
              <span className="ml-1.5 font-mono text-[10px] text-faint">{showQuiet ? "hide" : "show"}</span>
            </button>
            {showQuiet && <div className="mt-1.5 flex flex-col gap-1.5">{quiet.map(row)}</div>}
          </div>
        )}
      </div>
    </section>
  );
}

function AgentRow({
  agent: a, servedAt, projectId, intervalMs,
}: { agent: LiveAgent; servedAt: string; projectId?: string; intervalMs: number }) {
  const offline = a.state === "offline";
  const [open, setOpen] = React.useState(false);
  return (
    <div
      className={cn(
        "rounded-[11px] border border-line-2 bg-surface-2 px-3.5 py-2.5",
        // Offline fades rather than vanishes (D12). Dropping a dead agent holding
        // a branch is the quieter board this page exists to refuse.
        offline && "opacity-55",
      )}
    >
      <div className="flex items-start gap-2.5">
        <span
          className={cn(
            "mt-1.5 h-1.5 w-1.5 flex-none rounded-full",
            offline ? "bg-faint" : "bg-st-done hold-pulse",
          )}
          aria-hidden
        />
        <div className="min-w-0 flex-1">
          <div className="flex flex-wrap items-center gap-2">
            <span className="truncate text-[13px] text-fg-2">{a.label || "unnamed agent"}</span>
            {a.role && (
              <span className="rounded-md border border-line-2 px-1.5 py-0.5 font-mono text-[10px] text-muted">
                {a.role}
              </span>
            )}
            {a.parent_agent_id && (
              <span className="font-mono text-[10px] text-faint">child</span>
            )}
            <span className="font-mono text-[10px] text-faint">{a.state}</span>
          </div>
          <div className="mt-0.5 font-mono text-[10.5px] text-faint">
            {a.worktree || "no worktree"}
            {a.branch ? ` · ${a.branch}` : ""}
            {a.branch_orphaned && (
              <span className="ml-2 text-[color:var(--color-st-blocked)]">branch orphaned</span>
            )}
            <span className="ml-2" title={a.last_seen_at ?? undefined}>
              {ageLabel(a.last_seen_at, servedAt)}
            </span>
          </div>
          {/* PRD-34: what it is doing. Two named sources; silence is a word with an age. */}
          <div className="mt-1.5 flex flex-wrap items-baseline gap-x-3 gap-y-0.5 text-[11.5px]">
            <button
              type="button"
              onClick={() => setOpen((o) => !o)}
              aria-expanded={open}
              className="text-left text-fg-2 hover:text-fg"
            >
              <CallSummary agent={a} servedAt={servedAt} />
            </button>
            <StatusSummary agent={a} servedAt={servedAt} />
          </div>
          {open && (
            <Feed agentId={a.id} projectId={projectId} intervalMs={intervalMs} servedAt={servedAt} />
          )}
          {/* PRD-35 D11: what this agent handed to children. null is a word; expired is the
              third state — a spawn that never claimed. Absent on a server behind this build. */}
          {a.delegations !== undefined && <Delegations agent={a} />}
          <div className="mt-1.5 text-[12px] text-fg-2">{fileStateCopy(a.file_state)}</div>
          {a.files.length > 0 && (
            <ul className="mt-1 space-y-0.5 font-mono text-[10.5px] text-muted">
              {a.files.map((f, i) => (
                <li key={`${f.area}-${i}`}>
                  {f.area}
                  <span className="ml-1.5 text-faint">{fileKindCopy(f.kind)}{f.reason ? ` · ${f.reason}` : ""}</span>
                </li>
              ))}
            </ul>
          )}
          {a.holdings.length > 0 && (
            <div className="mt-2">
              <div className="font-mono text-[9.5px] uppercase tracking-wide text-faint">
                Recorded PRs
              </div>
              <ul className="mt-0.5 space-y-0.5 text-[12px]">
                {a.holdings.map((h) => (
                  <li key={h.id} className="flex flex-wrap items-baseline gap-2">
                    <span className="font-mono text-[11px] text-muted">{h.id}</span>
                    <span className="truncate text-fg-2">{h.title}</span>
                    {h.pr.state === "recorded" && h.pr.url ? (
                      <a href={h.pr.url} className="truncate font-mono text-[11px]" target="_blank" rel="noreferrer">
                        {h.pr.url}
                      </a>
                    ) : (
                      <span className="font-mono text-[11px] text-muted">unrecorded</span>
                    )}
                  </li>
                ))}
              </ul>
            </div>
          )}
        </div>
      </div>
    </div>
  );
}

/** PRD-35 D11: per-delegator counts and rows. Every state is a word; `expired, nothing
 *  claimed` is the one this block exists for. Tier text names requested and declared
 *  side by side and never hides a mismatch (D8). */
function Delegations({ agent: a }: { agent: LiveAgent }) {
  const d = a.delegations;
  const trackerTo = useTrackerTo();
  if (!d) {
    return <div className="mt-1.5 text-[11.5px] text-faint">no delegations</div>;
  }
  const total = d.open + d.claimed + d.finished + d.expired + d.closed;
  const parts: string[] = [];
  if (d.claimed) parts.push(`${d.claimed} claimed`);
  if (d.open) parts.push(`${d.open} open${d.oldest_open_seconds != null ? ` (oldest ${durationLabel(d.oldest_open_seconds)})` : ""}`);
  if (d.expired) parts.push(`${d.expired} expired`);
  if (d.finished) parts.push(`${d.finished} finished`);
  if (d.closed) parts.push(`${d.closed} closed`);
  return (
    <div className="mt-1.5 text-[11.5px]">
      <div className="text-fg-2">
        <span className="font-mono text-[10px] uppercase tracking-wide text-faint">delegated</span>{" "}
        {total}: {parts.join(", ")}
      </div>
      {d.rows.length > 0 && (
        <ul className="mt-0.5 space-y-0.5 font-mono text-[10.5px] text-muted">
          {d.rows.map((r) => (
            <li key={r.id} className={cn(r.state === "expired" && "text-[color:var(--color-st-blocked)]")}>
              <Link to={trackerTo} className="text-fg-2 underline decoration-dotted" title="open the tracker">
                {r.item}
              </Link>
              <span className="ml-1.5">{r.lane}</span>
              <span className="ml-1.5">{delegationCopy(r)}</span>
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}

function delegationCopy(r: LiveDelegationRow): string {
  const age = r.age_seconds != null ? ` · ${durationLabel(r.age_seconds)} ago` : "";
  switch (r.state) {
    case "open":
      return `open, requested ${r.requested_tier}${age}`;
    case "expired":
      return `expired, nothing claimed · requested ${r.requested_tier}${age}`;
    case "claimed":
      return `claimed by ${r.agent_id ?? "?"} (${tierCopy(r)})${age}`;
    case "finished":
      return `${outcomeCopy(r.outcome)} · ${r.agent_id ?? "?"} (${tierCopy(r)})`;
    case "closed":
      return r.closed_reason === "superseded"
        ? `superseded by ${r.closed_by ?? "another agent"}`
        : "withdrawn";
  }
}

/** Requested and declared, side by side. A mismatch is stated, an undeclared tier is a
 *  word — neither reads as satisfied (D8). */
function tierCopy(r: LiveDelegationRow): string {
  const model = r.declared_model ? `, ${r.declared_model}` : "";
  if (r.declared_tier === "undeclared" || !r.declared_tier) {
    return `requested ${r.requested_tier}, undeclared${model}`;
  }
  if (r.mismatch) {
    return `requested ${r.requested_tier}, declared ${r.declared_tier}${model}`;
  }
  return `${r.declared_tier}${model}`;
}

function outcomeCopy(o: LiveDelegationRow["outcome"]): string {
  switch (o) {
    case "signed_off": return "signed off";
    case "bounced": return "bounced";
    case "blocked": return "blocked";
    case "released": return "released";
    default: return "finished";
  }
}

/** Observed: what Graphban measured. `never` / `quiet` are words, not blanks (D7, D11). */
function CallSummary({ agent: a, servedAt }: { agent: LiveAgent; servedAt: string }) {
  if (a.call_state === "never" || !a.last_call) {
    return <span className="text-muted">no calls recorded</span>;
  }
  const age = ageLabel(a.last_call.at, servedAt);
  return (
    <span>
      <span className="font-mono text-[10px] uppercase tracking-wide text-faint">observed</span>{" "}
      <span className={cn("font-mono", !a.last_call.ok && "text-[color:var(--color-st-blocked)]")}>
        {a.last_call.tool}
      </span>
      {a.last_call.target && <span className="ml-1 text-muted">{a.last_call.target}</span>}
      <span className="ml-1.5 text-faint">{age}</span>
      {a.call_state === "quiet" && a.silence_seconds != null && (
        <span className="ml-1.5 text-faint">· no calls for {durationLabel(a.silence_seconds)}</span>
      )}
    </span>
  );
}

/** Reported: what the agent SAID, with its age. Never rendered in the observed style (D1). */
function StatusSummary({ agent: a, servedAt }: { agent: LiveAgent; servedAt: string }) {
  if (a.status_state === "unreported" || !a.status) {
    return <span className="text-faint">no status reported</span>;
  }
  return (
    <span>
      <span className="rounded border border-line-2 px-1 font-mono text-[9.5px] uppercase tracking-wide text-faint">
        reported
      </span>{" "}
      <span className={cn("text-fg-2", a.status.stale && "text-faint line-through decoration-faint")}>
        {a.status.text}
      </span>
      <span className="ml-1.5 text-faint">{ageLabel(a.status.at, servedAt)}</span>
      {a.status_state === "stale" && (
        <span className="ml-1.5 text-[color:var(--color-st-review)]">stale</span>
      )}
    </span>
  );
}

type FeedFilter = "all" | "reads" | "writes" | "failures";
const FEED_FILTERS: FeedFilter[] = ["all", "reads", "writes", "failures"];

function matchesFilter(r: LiveFeedRow, f: FeedFilter): boolean {
  if (f === "all") return true;
  if (f === "failures") return !r.ok;
  if (r.source === "reported") return f === "writes"; // a report is something the agent did
  return f === "writes" ? r.write : !r.write;
}

/** A run of identical consecutive observed rows, folded for display (PRD-34 D16). The table
 *  stays one row per call; only the view folds, and only on (tool, target, ok, source). */
interface FeedRun { row: LiveFeedRow; count: number; last: LiveFeedRow }

function collapseRuns(rows: LiveFeedRow[]): FeedRun[] {
  const out: FeedRun[] = [];
  for (const r of rows) {
    const prev = out[out.length - 1];
    if (
      prev
      && r.source === "observed"
      && prev.row.source === "observed"
      && prev.row.tool === r.tool
      && prev.row.target === r.target
      && prev.row.ok === r.ok
    ) {
      prev.count += 1;
      prev.last = r;
      continue;
    }
    out.push({ row: r, count: 1, last: r });
  }
  return out;
}

/** The tracker has no per-item URL yet, so an item id links to the page, not the row. */
function useTrackerTo(): string {
  const { active } = useProjectCtx();
  const { data: config } = useConfig();
  return config?.hosted_mode && active?.tag ? projectPath(active.tag, "tracker") : "/tracker";
}

function Feed({
  agentId, projectId, intervalMs, servedAt,
}: { agentId: string; projectId?: string; intervalMs: number; servedAt: string }) {
  const { data, isLoading, isError } = useLiveFeed(projectId, agentId, intervalMs, true);
  const [filter, setFilter] = React.useState<FeedFilter>("all");
  const trackerTo = useTrackerTo();
  if (isError) {
    return <div className="mt-1.5 text-[11.5px] text-muted">The feed could not be loaded.</div>;
  }
  if (isLoading || !data) {
    return <div className="mt-1.5 text-[11.5px] text-muted">Loading…</div>;
  }
  if (data.state === "never") {
    return (
      <div className="mt-1.5 text-[11.5px] text-muted">
        No calls recorded in the last {data.retention_days} days.
      </div>
    );
  }
  const shown = data.rows.filter((r) => matchesFilter(r, filter));
  const runs = collapseRuns(shown);
  return (
    <div className="mt-1.5">
      <div className="mb-1 flex items-center gap-1" role="group" aria-label="feed filter">
        {FEED_FILTERS.map((f) => (
          <button
            key={f}
            type="button"
            aria-pressed={filter === f}
            onClick={() => setFilter(f)}
            className={cn(
              "rounded border px-1.5 py-0.5 font-mono text-[10px]",
              filter === f ? "border-line-hover bg-surface-3 text-fg" : "border-line-2 text-muted hover:text-fg-2",
            )}
          >
            {f}
          </button>
        ))}
      </div>
      {runs.length === 0 ? (
        <div className="text-[11.5px] text-muted">No {filter} in this feed.</div>
      ) : (
        <ul className="space-y-0.5 border-l border-line-2 pl-2.5" aria-label="feed">
          {runs.map((run) => (
            <FeedRowView
              key={run.row.id}
              row={run.row}
              count={run.count}
              servedAt={data.served_at || servedAt}
              trackerTo={trackerTo}
            />
          ))}
        </ul>
      )}
    </div>
  );
}

const ITEM_ID = /^[A-Z][A-Z0-9]*-\d+$/;

function FeedRowView({
  row: r, count, servedAt, trackerTo,
}: { row: LiveFeedRow; count: number; servedAt: string; trackerTo: string }) {
  if (r.source === "reported") {
    return (
      <li className="flex flex-wrap items-baseline gap-x-2 text-[11.5px]">
        <span className="rounded border border-line-2 px-1 font-mono text-[9.5px] uppercase tracking-wide text-faint">
          reported
        </span>
        <span className="text-fg-2">{r.status}</span>
        {(r.files ?? []).length > 0 && (
          <span className="font-mono text-[10.5px] text-muted">{(r.files ?? []).join(", ")}</span>
        )}
        <span className="text-faint">{ageLabel(r.at, servedAt)}</span>
      </li>
    );
  }
  return (
    <li
      className={cn(
        "flex flex-wrap items-baseline gap-x-2 text-[11.5px]",
        !r.ok && "text-[color:var(--color-st-blocked)]",
      )}
    >
      <span className="font-mono">{r.tool}</span>
      {r.target && (
        // A failed call on an item links to the tracker (the tracker has no per-item URL
        // yet, so this is the page, not the row).
        !r.ok && ITEM_ID.test(r.target) ? (
          <Link to={trackerTo} className="text-muted underline decoration-dotted" title="open the tracker">
            {r.target}
          </Link>
        ) : (
          <span className="text-muted">{r.target}</span>
        )
      )}
      {count > 1 && <span className="font-mono text-[10px] text-faint" aria-label={`${count} calls`}>×{count}</span>}
      {!r.ok && r.error_code && <span className="font-mono text-[10px]">{r.error_code}</span>}
      <span className="text-faint">{ageLabel(r.at, servedAt)}</span>
    </li>
  );
}

function durationLabel(s: number): string {
  if (s < 60) return `${s}s`;
  if (s < 3600) return `${Math.floor(s / 60)}m`;
  if (s < 86400) return `${Math.floor(s / 3600)}h`;
  return `${Math.floor(s / 86400)}d`;
}

function RoleCounts({ byRole, roles }: { byRole: Record<string, number>; roles: string[] }) {
  const shown = [...roles, "all-in-one"].filter((r) => byRole[r]);
  if (shown.length === 0) return null;
  return (
    <span className="flex items-center gap-1.5">
      {shown.map((r) => (
        <span
          key={r}
          className={cn(
            "rounded-md border px-1.5 py-0.5 font-mono text-[10px]",
            ROLE_TONE[r] ?? "text-muted border-line-2",
          )}
        >
          {byRole[r]} {r}
        </span>
      ))}
    </span>
  );
}

function fileStateCopy(state: LiveFileState): string {
  if (state === "unreserved") return "holds work with no area lease";
  return state;
}

function fileKindCopy(kind: LiveFileKind): string {
  if (kind === "declared") return "declared on item, not reserved";
  if (kind === "reported") return "reported by agent, not reserved";
  return kind;
}

/** Age against the payload clock (D6), not the browser clock. */
function ageLabel(at: string | null, servedAt: string): string {
  if (!at) return "no heartbeat yet";
  const then = Date.parse(/(Z|[+-]\d{2}:?\d{2})$/.test(at) ? at : `${at}Z`);
  const now = Date.parse(/(Z|[+-]\d{2}:?\d{2})$/.test(servedAt) ? servedAt : `${servedAt}Z`);
  if (Number.isNaN(then) || Number.isNaN(now)) return "no heartbeat yet";
  const s = Math.max(0, Math.floor((now - then) / 1000));
  if (s < 60) return `${s}s ago`;
  if (s < 3600) return `${Math.floor(s / 60)}m ago`;
  if (s < 86400) return `${Math.floor(s / 3600)}h ago`;
  return `${Math.floor(s / 86400)}d ago`;
}
