import { useLocation, useSearchParams, Link } from "react-router-dom";

import { Avatar } from "@/components/ui/avatar";
import { useProjectCtx } from "@/features/ProjectContext";
import { cn } from "@/lib/cn";
import { useConfig, useLive } from "@/lib/queries";
import { projectPath } from "@/lib/routes";
import type { LiveAgent, LiveBoard, LiveFileKind, LiveFileState, LiveUser } from "@/lib/types";

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
              <UserBlock key={u.user_id ?? "unattributed"} user={u} board={data} />
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

function UserBlock({ user, board }: { user: LiveUser; board: LiveBoard }) {
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
      <div className="flex flex-col gap-1.5">
        {user.agents.map((a) => (
          <AgentRow key={a.id} agent={a} servedAt={board.served_at} />
        ))}
      </div>
    </section>
  );
}

function AgentRow({ agent: a, servedAt }: { agent: LiveAgent; servedAt: string }) {
  const offline = a.state === "offline";
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
