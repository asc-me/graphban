import * as React from "react";
import { Link } from "react-router-dom";

import { Avatar } from "@/components/ui/avatar";
import { useProjectCtx } from "@/features/ProjectContext";
import { cn } from "@/lib/cn";
import { useConfig, useLiveFeed } from "@/lib/queries";
import { projectPath } from "@/lib/routes";
import type { LiveAgent, LiveBoard, LiveFeedRow, LiveUser } from "@/lib/types";

import {
  ageLabel,
  collapseRuns,
  delegationCopy,
  durationLabel,
  fileKindCopy,
  fileStateCopy,
} from "./liveUtils";

type OfflineKind = "held" | "orphan" | "quiet";

function offlineKind(a: LiveAgent): OfflineKind {
  const d = a.delegations;
  if (a.holdings.length > 0 || a.files.length > 0 || (!!d && d.open + d.expired > 0)) {
    return "held";
  }
  return a.branch_orphaned ? "orphan" : "quiet";
}

function Collapsed({
  agents, label, row,
}: { agents: LiveAgent[]; label: string; row: (a: LiveAgent) => React.ReactNode }) {
  const [show, setShow] = React.useState(false);
  if (agents.length === 0) return null;
  return (
    <div className="rounded-[11px] border border-dashed border-line-2 px-3.5 py-2 text-[12px]">
      <button
        type="button"
        aria-expanded={show}
        onClick={() => setShow((v) => !v)}
        className="text-muted hover:text-fg-2"
      >
        {agents.length} offline {agents.length === 1 ? "agent" : "agents"} {label}
        <span className="ml-1.5 font-mono text-[10px] text-faint">{show ? "hide" : "show"}</span>
      </button>
      {show && <div className="mt-1.5 flex flex-col gap-1.5">{agents.map(row)}</div>}
    </div>
  );
}

function UserBlock({ user, board, projectId }: { user: LiveUser; board: LiveBoard; projectId?: string }) {
  const shown = user.agents.filter((a) => a.state !== "offline" || offlineKind(a) === "held");
  const orphans = user.agents.filter((a) => a.state === "offline" && offlineKind(a) === "orphan");
  const quiet = user.agents.filter((a) => a.state === "offline" && offlineKind(a) === "quiet");
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
        <Avatar initials={user.initials || "?"} color={user.color || "#8b949e"} size={22} />
        <h2 className="text-[13.5px] font-semibold">{user.label}</h2>
        <span className="font-mono text-[10px] text-faint">
          {user.online}/{user.total}
        </span>
      </div>
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
        <Collapsed agents={orphans} label="with only an orphaned branch" row={row} />
        <Collapsed agents={quiet} label="holding nothing" row={row} />
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
    <div className={cn("rounded-[11px] border border-line-2 bg-surface-2 px-3.5 py-2.5", offline && "opacity-55")}>
      <div className="flex items-start gap-2.5">
        <span
          className={cn("mt-1.5 h-1.5 w-1.5 flex-none rounded-full", offline ? "bg-faint" : "bg-st-done hold-pulse")}
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
            {a.parent_agent_id && <span className="font-mono text-[10px] text-faint">child</span>}
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
            <AgentFeed agentId={a.id} projectId={projectId} intervalMs={intervalMs} servedAt={servedAt} />
          )}
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
              <div className="font-mono text-[9.5px] uppercase tracking-wide text-faint">Recorded PRs</div>
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

function Delegations({ agent: a }: { agent: LiveAgent }) {
  const d = a.delegations;
  const trackerTo = useTrackerTo();
  if (!d) {
    return <div className="mt-1.5 text-[11.5px] text-faint">no delegations</div>;
  }
  const total = d.open + d.claimed + d.finished + d.expired + d.closed;
  const parts: string[] = [];
  if (d.claimed) parts.push(`${d.claimed} claimed`);
  if (d.open) {
    parts.push(`${d.open} open${d.oldest_open_seconds != null ? ` (oldest ${durationLabel(d.oldest_open_seconds)})` : ""}`);
  }
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
  if (r.source === "reported") return f === "writes";
  return f === "writes" ? r.write : !r.write;
}

function useTrackerTo(): string {
  const { active } = useProjectCtx();
  const { data: config } = useConfig();
  return config?.hosted_mode && active?.tag ? projectPath(active.tag, "tracker") : "/tracker";
}

function AgentFeed({
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
              filter === f ? "border-control-hover bg-surface-3 text-fg" : "border-control text-muted hover:text-fg-2",
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
    <li className={cn("flex flex-wrap items-baseline gap-x-2 text-[11.5px]", !r.ok && "text-[color:var(--color-st-blocked)]")}>
      <span className="font-mono">{r.tool}</span>
      {r.target && (
        !r.ok && ITEM_ID.test(r.target) ? (
          <Link to={trackerTo} className="text-muted underline decoration-dotted" title="open the tracker">
            {r.target}
          </Link>
        ) : (
          <span className="text-muted">{r.target}</span>
        )
      )}
      {count > 1 && (
        <span className="font-mono text-[10px] text-faint" aria-label={`${count} calls`}>×{count}</span>
      )}
      {!r.ok && r.error_code && <span className="font-mono text-[10px]">{r.error_code}</span>}
      <span className="text-faint">{ageLabel(r.at, servedAt)}</span>
    </li>
  );
}

export function AgentBoard({
  users,
  board,
  projectId,
}: {
  users: LiveUser[];
  board: LiveBoard;
  projectId?: string;
}) {
  return (
    <div className="mx-auto mt-5 flex max-w-5xl flex-col gap-5" aria-label="Agent board">
      {users.map((u) => (
        <UserBlock key={u.user_id ?? "unattributed"} user={u} board={board} projectId={projectId} />
      ))}
    </div>
  );
}
