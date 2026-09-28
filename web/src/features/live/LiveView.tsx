import * as React from "react";
import { Link, useLocation, useSearchParams } from "react-router-dom";

import { LiveBoardSkeleton, PlannerEmpty, PlannerError } from "@/components/planner/PlannerStates";
import { PlaceHeader } from "@/components/shell/PlaceHeader";
import { useProjectCtx } from "@/features/ProjectContext";
import { useConfig, useFleet, useLive, useLiveFeed } from "@/lib/queries";
import { projectPath } from "@/lib/routes";
import type { LiveBoard } from "@/lib/types";

import { AgentBoard } from "./AgentBoard";
import { CensusChips } from "./CensusChips";
import { KpiRow } from "./KpiRow";
import { LiveStreamPanel } from "./LiveStream";
import { PlannerDesk } from "./PlannerDesk";
import { PresenceStrip } from "./PresenceStrip";
import { RoleCounts } from "./RoleCounts";
import { TicketLanes } from "./TicketLanes";
import { buildTicketLanes, computeKpis, flattenAgents } from "./liveUtils";

/**
 * Observe Live (PRD-47 S11): presence strip, planner desk, ticket lanes, and a stream
 * that honours the focused agent — on top of the per-agent board shipped in PRD-33/34/35.
 */
export function LiveView() {
  const { activeId, active } = useProjectCtx();
  const { data: config } = useConfig();
  const { pathname } = useLocation();
  const [params] = useSearchParams();
  const user = params.get("user");
  const { data, isLoading, isError, refetch } = useLive(activeId, user);
  const { data: fleet } = useFleet(activeId);
  const fleetTo = config?.hosted_mode && active?.tag
    ? projectPath(active.tag, "fleet.v1")
    : "/fleet.v1";
  const trackerTo = config?.hosted_mode && active?.tag ? projectPath(active.tag, "tracker") : "/tracker";

  const [paused, setPaused] = React.useState(false);
  const [snapshot, setSnapshot] = React.useState<LiveBoard | null>(null);
  const [focusId, setFocusId] = React.useState<string | null>(null);
  const board = paused && snapshot ? snapshot : data;
  const flat = React.useMemo(() => flattenAgents(board?.users ?? []), [board?.users]);
  const focusAgent = focusId ? flat.find(({ agent }) => agent.id === focusId)?.agent : null;
  const emptyProject = !!board && board.users.length === 0 && !user;
  const emptyFilter = !!board && board.users.length === 0 && !!user;

  const intervalMs = (board?.heartbeat_interval_seconds ?? 50) * 1000;
  const streamEnabled = !paused && !!focusId;
  const { data: focusFeed } = useLiveFeed(activeId, focusId ?? "", intervalMs, streamEnabled);
  const { data: projectFeed } = useLiveFeed(activeId, flat[0]?.agent.id ?? "", intervalMs, !paused && !focusId && flat.length > 0);

  const feed = focusId ? focusFeed : projectFeed;
  const lanes = React.useMemo(
    () => buildTicketLanes(flat, board?.served_at ?? "", feed?.rows ?? []),
    [flat, board?.served_at, feed?.rows],
  );
  const kpis = board ? computeKpis(board, flat) : null;
  const planners = flat.filter(({ agent }) => agent.role === "planner").map(({ agent }) => agent);

  const payloadAgents = board?.users.reduce((n, u) => n + u.agents.length, 0) ?? 0;
  const censusTotal = board?.user_counts.reduce((n, c) => n + c.total, 0) ?? 0;

  function togglePause() {
    if (!paused && data) setSnapshot(data);
    setPaused((p) => !p);
  }

  return (
    <div className="flex h-full min-h-0 flex-col">
      <PlaceHeader
        viewName="Live"
        purpose="Who is on this project right now, what they hold, and whether a PR was recorded."
        action={
          <div className="flex items-center gap-3">
            {board ? (
              <RoleCounts byRole={board.by_role ?? {}} roles={board.roles ?? []} />
            ) : (
              <div className="h-5 w-24 animate-pulse rounded-md bg-surface-3" aria-hidden />
            )}
            <button
              type="button"
              onClick={togglePause}
              className="rounded-lg border border-line-2 px-2.5 py-1 text-[12px] text-muted hover:border-line-hover hover:text-fg-2"
            >
              {paused ? "Resume" : "Pause"}
            </button>
            <Link to={fleetTo} className="text-[12.5px] text-muted hover:text-fg-2">
              Fleet.v1
            </Link>
          </div>
        }
      />

      {board?.truncated && (
        <div
          role="status"
          className="flex-none border-b border-st-review/30 bg-st-review/[0.06] px-5 py-2 font-mono text-[11px] text-st-review"
        >
          Showing {payloadAgents} of {board.total_agents} agents
        </div>
      )}

      <CensusChips board={board} pathname={pathname} user={user} loading={isLoading && !board} />

      <div className="min-h-0 flex-1 overflow-y-auto">
        {isLoading && !board ? (
          <LiveBoardSkeleton />
        ) : isError || !board ? (
          <PlannerError message="The live board could not be loaded." onRetry={() => refetch()} />
        ) : emptyProject ? (
          <PlannerEmpty
            title="No agents on this project yet"
            description="Live shows who is working here right now — what they hold, what they called, and whether a PR was recorded. An agent appears here the moment it registers."
            action={
              <Link
                to={fleetTo}
                className="inline-flex items-center gap-1.5 rounded-lg border border-line px-3 py-1.5 text-[12px] text-muted transition-colors hover:border-line-hover hover:text-fg-2"
              >
                Open Fleet.v1
              </Link>
            }
          />
        ) : emptyFilter ? (
          <PlannerEmpty
            title="No agents for this person"
            description="This person has credentials on the project but no agent has registered under their name yet."
          />
        ) : (
          <>
            {kpis && <KpiRow kpis={kpis} />}
            <PresenceStrip
              agents={flat}
              servedAt={board.served_at}
              focusId={focusId}
              onFocus={setFocusId}
            />
            <div className="p-5">
              <div className="mx-auto grid max-w-5xl gap-4 lg:grid-cols-2">
                <PlannerDesk planners={planners} fleet={fleet} trackerTo={trackerTo} />
                <TicketLanes lanes={lanes} servedAt={board.served_at} />
              </div>
              <AgentBoard users={board.users} board={board} projectId={activeId} />
              <section className="mx-auto mt-4 max-w-5xl rounded-[11px] border border-line-2 bg-surface-2 p-3.5">
                <h2 className="text-[13px] font-semibold">Stream</h2>
                <p className="mt-0.5 text-[11.5px] text-muted">
                  {focusId
                    ? "Filtered to the focused agent."
                    : "Showing the first agent's feed — focus an agent above to switch."}
                </p>
                <div className="mt-2">
                  <LiveStreamPanel
                    feed={feed}
                    servedAt={board.served_at}
                    trackerTo={trackerTo}
                    focusId={focusId}
                    focusLabel={focusAgent?.label ?? focusId}
                  />
                </div>
              </section>
              {!user && censusTotal > 0 && (
                <div className="mx-auto mt-3 max-w-5xl font-mono text-[10px] text-faint">
                  {censusTotal} agents across {board.user_counts.length} people
                </div>
              )}
            </div>
          </>
        )}
      </div>
    </div>
  );
}
