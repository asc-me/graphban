import { Link } from "react-router-dom";

import { useProjectCtx } from "@/features/ProjectContext";
import { cn } from "@/lib/cn";
import { useConfig, useFleet } from "@/lib/queries";
import { projectPath } from "@/lib/routes";

/**
 * The design's two Fleet tabs, as links between the shipped v1 / v2 routes.
 *
 * Roster and harness config stay on separate pages (FleetV2View's comment: mixing
 * them onto one scroll made the redesign look like a banner on the old UI). The
 * tablist is the drawing's IA without collapsing that split.
 */
export function FleetPageTabs({ active }: { active: "manage" | "harness" }) {
  const { activeId, active: project } = useProjectCtx();
  const { data: config } = useConfig();
  const { data } = useFleet(activeId);
  const href = (view: string) =>
    config?.hosted_mode && project?.tag ? projectPath(project.tag, view) : `/${view}`;

  const manageMeta = data
    ? data.total === 0
      ? "no agents"
      : `${data.online} of ${data.total} online`
    : "no agents";
  const harnessCount = new Set((data?.matrix?.rows ?? []).map((r) => r.harness)).size;
  const harnessMeta = harnessCount ? `${harnessCount} in catalog` : "not served";

  const tabs = [
    { id: "manage" as const, to: href("fleet.v1"), label: "Fleet management", meta: manageMeta },
    { id: "harness" as const, to: href("fleet.v2"), label: "Harness config", meta: harnessMeta },
  ];

  return (
    <div
      role="tablist"
      aria-label="Fleet"
      className="sticky top-0 z-[2] flex flex-none items-stretch gap-1 border-b border-line bg-surface px-5"
      data-testid="fleet-page-tabs"
    >
      {tabs.map((t) => {
        const on = t.id === active;
        return (
          <Link
            key={t.id}
            role="tab"
            aria-selected={on}
            to={t.to}
            className={cn(
              "flex items-center gap-2 border-b-2 px-3 py-2.5 text-body font-medium",
              on ? "border-accent text-fg" : "border-transparent text-muted hover:text-fg",
            )}
          >
            {t.label}
            <span className={cn("font-mono text-meta font-normal", on ? "text-muted" : "text-faint")}>
              {t.meta}
            </span>
          </Link>
        );
      })}
    </div>
  );
}
