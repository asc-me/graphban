import { useState } from "react";

import { PlaceHeader } from "@/components/shell/PlaceHeader";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs";
import { PlannerError, TableSkeleton } from "@/components/planner/PlannerStates";
import { ChangesProbesTab } from "@/features/harness/ChangesProbesTab";
import { GuidanceTab } from "@/features/harness/GuidanceTab";
import { PerformanceTab } from "@/features/harness/PerformanceTab";
import { useProjectCtx } from "@/features/ProjectContext";
import { useHarness } from "@/lib/queries";

/**
 * PRD-47 S12 — three tabs over the harness data.
 *
 * Performance: the grid, the effort curve, the cell detail.
 * Guidance: the routing table actually served, the grading rules, the verbatim text.
 * Changes & probes: recommendations as drafts, the probe panel.
 *
 * The tab shell owns the fetch and the error/loading states so each tab receives data
 * rather than re-requesting it.
 */
export function HarnessView() {
  const { activeId } = useProjectCtx();
  const [versions, setVersions] = useState<"current" | "all">("current");
  const harnessQ = useHarness(activeId, { versions });
  const { data, isLoading } = harnessQ;

  if (harnessQ.isError && !data) {
    return (
      <div className="flex h-full min-h-0 flex-col" data-testid="harness-error">
        <PlaceHeader viewName="Harness" purpose="How each model has turned out, per capability and size band." />
        <PlannerError
          message="The catalog has not been served. That is not an empty matrix."
          onRetry={() => void harnessQ.refetch()}
        />
      </div>
    );
  }

  if (isLoading || !data) {
    return (
      <div className="flex h-full min-h-0 flex-col" data-testid="harness-loading">
        <PlaceHeader viewName="Harness" purpose="How each model has turned out, per capability and size band." />
        <div className="min-h-0 flex-1 overflow-y-auto p-5">
          <TableSkeleton rows={8} columns={5} />
        </div>
      </div>
    );
  }

  return (
    <div className="flex h-full min-h-0 flex-col" data-testid="harness-view">
      <PlaceHeader
        viewName="Harness"
        purpose={`How each model has turned out, per capability and size band, over the last ${data.window_days} days. A rate under ${data.floor} finished attempts is shown grey because it is not yet a measurement.`}
        action={
          <div className="flex items-center gap-2">
            <label className="font-mono text-[10.5px] text-faint" htmlFor="harness-versions">
              VERSIONS
            </label>
            <select
              id="harness-versions"
              aria-label="Binary versions"
              data-testid="harness-versions"
              className="rounded-[8px] border border-line-2 bg-surface-2 px-2 py-1 text-[12px]"
              value={versions}
              onChange={(e) => setVersions(e.target.value as "current" | "all")}
            >
              <option value="current">Current only</option>
              <option value="all">Every version</option>
            </select>
          </div>
        }
      />

      <Tabs defaultValue="performance" className="flex min-h-0 flex-1 flex-col">
        <div className="border-b border-line-2 px-5 pt-2">
          <TabsList>
            <TabsTrigger value="performance">Performance</TabsTrigger>
            <TabsTrigger value="guidance">Guidance</TabsTrigger>
            <TabsTrigger value="changes">Changes & probes</TabsTrigger>
          </TabsList>
        </div>

        <TabsContent value="performance" className="min-h-0 flex-1 overflow-y-auto focus:outline-none">
          <PerformanceTab data={data} versions={versions} onVersionsChange={setVersions} />
        </TabsContent>
        <TabsContent value="guidance" className="min-h-0 flex-1 overflow-y-auto focus:outline-none">
          <GuidanceTab projectId={activeId} />
        </TabsContent>
        <TabsContent value="changes" className="min-h-0 flex-1 overflow-y-auto focus:outline-none">
          <ChangesProbesTab projectId={activeId} data={data} />
        </TabsContent>
      </Tabs>
    </div>
  );
}
