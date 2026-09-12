import { Link } from "react-router-dom";

import { useProjectCtx } from "@/features/ProjectContext";
import { useConfig, useFleet } from "@/lib/queries";
import { projectPath } from "@/lib/routes";
import { MatrixTable } from "./matrixTable";
import { MixAllocation } from "./MixAllocation";

/**
 * Fleet.v2 — catalog and spawn mix only (GRPH-866).
 *
 * Roster, seats, and wave controls stay on Fleet.v1. Mixing those into this page
 * made the redesign look like a banner on the old UI.
 */
export function FleetV2View() {
  const { activeId, active } = useProjectCtx();
  const scope = active?.tag || active?.name || activeId;
  const { data: config } = useConfig();
  const { data, refetch } = useFleet(activeId);
  const viewHref = (view: string) =>
    config?.hosted_mode && active?.tag ? projectPath(active.tag, view) : `/${view}`;

  return (
    <div className="flex h-full min-h-0 flex-col" data-testid="fleet-v2">
      <div className="flex flex-none items-center justify-between border-b border-line px-5 py-4">
        <div>
          <h1 className="text-[18px] font-semibold tracking-tight">
            Fleet.v2 <span className="font-mono text-[13px] font-normal text-muted">{scope}</span>
          </h1>
          <p className="mt-0.5 text-[12.5px] text-muted">
            What can run, and how you allocate it. Roster and waves are Fleet.v1.
          </p>
        </div>
        <div className="flex items-center gap-3 text-[12px]">
          <Link to={viewHref("harness")} className="text-muted transition-colors hover:text-fg-2">
            Harness
          </Link>
          <Link to={viewHref("outposts")} className="text-muted transition-colors hover:text-fg-2">
            Outposts
          </Link>
          <Link to={viewHref("fleet.v1")} className="text-muted transition-colors hover:text-fg-2">
            Fleet.v1
          </Link>
        </div>
      </div>
      <div className="min-h-0 flex-1 overflow-y-auto p-6">
        <MatrixTable
          rows={data?.matrix?.rows ?? []}
          harnessHref={viewHref("harness")}
        />
        <MixAllocation
          projectId={activeId}
          profile={data?.profile ?? null}
          rows={data?.matrix?.rows ?? []}
          recent={data?.mix}
          onSaved={() => { void refetch(); }}
        />
      </div>
    </div>
  );
}
