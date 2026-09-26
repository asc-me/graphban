import { Check, Link2 } from "lucide-react";
import * as React from "react";

import { PlannerEmpty, RoadmapBoardSkeleton } from "@/components/planner/PlannerStates";
import { PlaceHeader } from "@/components/shell/PlaceHeader";
import { Button } from "@/components/ui/button";
import { useProjectCtx } from "@/features/ProjectContext";
import { copyText } from "@/lib/clipboard";
import { useRoadmap } from "@/lib/queries";

import { RoadmapBoard } from "./RoadmapBoard";

export function RoadmapView() {
  const { activeId } = useProjectCtx();
  const { data: phases = [], isLoading } = useRoadmap(activeId);
  const [copied, setCopied] = React.useState(false);

  async function copyPublic() {
    const url = `${window.location.origin}/embed/roadmap?project=${encodeURIComponent(activeId)}`;
    if (!(await copyText(url))) return;
    setCopied(true);
    setTimeout(() => setCopied(false), 1500);
  }

  return (
    <div className="flex h-full min-h-0 flex-col">
      <PlaceHeader
        viewName="Roadmap"
        purpose="MVP → Post-MVP → Later. Progress rolls up from milestones."
        action={
          <Button variant="outline" size="sm" onClick={copyPublic}>
            {copied ? <Check size={13} className="text-accent" /> : <Link2 size={13} />}
            {copied ? "Copied public link" : "Copy public link"}
          </Button>
        }
      />
      <div className="min-h-0 flex-1 overflow-y-auto p-6">
        {isLoading ? (
          <RoadmapBoardSkeleton />
        ) : phases.length === 0 ? (
          <PlannerEmpty
            title="No milestones in this project"
            description="PRDs still live under PRDs."
          />
        ) : (
          <RoadmapBoard phases={phases} />
        )}
      </div>
    </div>
  );
}
