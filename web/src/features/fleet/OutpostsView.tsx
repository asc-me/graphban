import { Server } from "lucide-react";
import { Link } from "react-router-dom";

import { useProjectCtx } from "@/features/ProjectContext";
import { useConfig, useFleet } from "@/lib/queries";
import { projectPath } from "@/lib/routes";
import { cn } from "@/lib/cn";
import { groupOutposts } from "./outposts";

/**
 * Connected gban/gbfleet hosts and what they last declared (GRPH-866).
 *
 * There is no outpost table: a host is whoever registered an agent with a `host`
 * capability or a `label` of the form `model @ host`. Unspecified is a real group,
 * never silently called localhost.
 */
export function OutpostsView() {
  const { activeId, active } = useProjectCtx();
  const scope = active?.tag || active?.name || activeId;
  const { data: config } = useConfig();
  const { data, isLoading } = useFleet(activeId);
  const fleetHref = config?.hosted_mode && active?.tag
    ? projectPath(active.tag, "fleet")
    : "/fleet";

  const outposts = groupOutposts(data?.agents ?? []);

  return (
    <div className="flex h-full min-h-0 flex-col" data-testid="outposts-view">
      <div className="flex flex-none items-center justify-between border-b border-line px-5 py-4">
        <div>
          <h1 className="text-[18px] font-semibold tracking-tight">
            Outposts <span className="font-mono text-[13px] font-normal text-muted">{scope}</span>
          </h1>
          <p className="mt-0.5 text-[12.5px] text-muted">
            Machines that have registered a gban/gbfleet agent on this project, and the
            harness they declared.
          </p>
        </div>
        <Link to={fleetHref} className="text-[12px] text-muted transition-colors hover:text-fg-2">
          Fleet catalog
        </Link>
      </div>
      <div className="min-h-0 flex-1 overflow-y-auto p-5">
        {isLoading || !data ? (
          <p className="text-[13px] text-muted">Loading…</p>
        ) : outposts.length === 0 ? (
          <div
            className="mx-auto mt-16 max-w-md text-center text-[13px] text-muted"
            data-testid="outposts-empty"
          >
            <Server size={16} className="mx-auto mb-2 opacity-50" />
            No host has registered on this project. That is not a list of zero outposts —
            nothing has checked in.
          </div>
        ) : (
          <div className="mx-auto flex max-w-3xl flex-col gap-3">
            {outposts.map((post) => (
              <section
                key={post.host}
                className="rounded-[11px] border border-line-2 p-3"
                data-testid="outpost-card"
              >
                <div className="mb-2 flex items-center justify-between gap-2">
                  <h2 className="font-mono text-[13px] font-medium">
                    {post.specified ? post.host : "unspecified host"}
                  </h2>
                  <span className="font-mono text-[11px] text-faint">
                    {post.online} of {post.agents.length} online
                  </span>
                </div>
                <div className="space-y-1.5">
                  {post.agents.map((agent) => (
                    <div
                      key={agent.id}
                      className={cn(
                        "rounded-[9px] border border-line-2 bg-surface-2 px-3 py-2",
                        agent.state === "offline" && "opacity-60",
                      )}
                    >
                      <div className="flex flex-wrap items-center gap-2 text-[12px]">
                        <span className="font-mono text-[11px]">{agent.key}</span>
                        <span className="text-muted">{agent.label || "no label"}</span>
                        <span className="ml-auto font-mono text-[10px] uppercase text-faint">
                          {agent.state}
                        </span>
                      </div>
                      <dl className="mt-1.5 grid grid-cols-2 gap-x-3 gap-y-0.5 font-mono text-[11px] text-muted sm:grid-cols-3">
                        <div><dt className="inline text-faint">vendor </dt><dd className="inline">{agent.vendor || "—"}</dd></div>
                        <div><dt className="inline text-faint">model </dt><dd className="inline">{agent.model || "—"}</dd></div>
                        <div><dt className="inline text-faint">tier </dt><dd className="inline">{agent.tier || "—"}</dd></div>
                        <div><dt className="inline text-faint">os </dt><dd className="inline">{agent.os || "not declared"}</dd></div>
                        <div className="col-span-2 truncate sm:col-span-2">
                          <dt className="inline text-faint">worktree </dt>
                          <dd className="inline">{agent.worktree || "—"}</dd>
                        </div>
                        <div className="truncate">
                          <dt className="inline text-faint">branch </dt>
                          <dd className="inline">{agent.branch || "—"}</dd>
                        </div>
                      </dl>
                    </div>
                  ))}
                </div>
              </section>
            ))}
          </div>
        )}
      </div>
    </div>
  );
}
