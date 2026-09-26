import { Link } from "react-router-dom";

import { PlaceHeader } from "@/components/shell/PlaceHeader";
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
      <PlaceHeader
        viewName={`Fleet.v2 ${scope}`}
        purpose="What can run, and how you allocate it. Roster and waves are Fleet.v1."
        action={
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
        }
      />
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
