import { useEffect, useRef } from "react";
import { useSearchParams } from "react-router-dom";

import { useProjectCtx } from "@/features/ProjectContext";
import { cn } from "@/lib/cn";
import { useFleet } from "@/lib/queries";

/**
 * Each tab renders a different view, so choosing one remounts the tablist. A keyboard
 * move sets this so the new instance puts focus back on the tab the user just chose.
 */
let focusAfterSwitch: FleetTab | null = null;

export type FleetTab = "manage" | "harness";

/** The selected tab lives in the URL (`?tab=manage|harness`); manage is the default. */
export function fleetTabFromSearch(params: URLSearchParams): FleetTab {
  return params.get("tab") === "harness" ? "harness" : "manage";
}

/**
 * The design's two Fleet tabs: one page, the tab in `?tab=`.
 *
 * Roster and harness config keep their own bodies (FleetV2View's comment: mixing
 * them onto one scroll made the redesign look like a banner on the old UI); this
 * only merges the IA. Arrow keys, Home and End move between tabs.
 */
export function FleetPageTabs({ active }: { active: FleetTab }) {
  const { activeId } = useProjectCtx();
  const { data } = useFleet(activeId);
  const [, setSearchParams] = useSearchParams();
  const refs = useRef<Record<string, HTMLButtonElement | null>>({});
  useEffect(() => {
    if (focusAfterSwitch === active) refs.current[active]?.focus();
    focusAfterSwitch = null;
  }, [active]);
  const select = (id: FleetTab) =>
    setSearchParams(
      (prev) => {
        const next = new URLSearchParams(prev);
        next.set("tab", id);
        return next;
      },
      { replace: true },
    );

  const manageMeta = data
    ? data.total === 0
      ? "no agents"
      : `${data.online} of ${data.total} online`
    : "no agents";
  const harnessCount = new Set((data?.matrix?.rows ?? []).map((r) => r.harness)).size;
  const harnessMeta = harnessCount ? `${harnessCount} in catalog` : "not served";

  const tabs = [
    { id: "manage" as const, label: "Fleet management", meta: manageMeta },
    { id: "harness" as const, label: "Harness config", meta: harnessMeta },
  ];

  return (
    <div
      role="tablist"
      aria-label="Fleet"
      className="sticky top-0 z-[2] flex flex-none items-stretch gap-1 border-b border-line bg-surface px-5"
      data-testid="fleet-page-tabs"
    >
      {tabs.map((t, i) => {
        const on = t.id === active;
        const move = (to: number) => {
          const target = tabs[(to + tabs.length) % tabs.length];
          if (target.id === active) return;
          focusAfterSwitch = target.id;
          select(target.id);
        };
        return (
          <button
            key={t.id}
            ref={(el) => {
              refs.current[t.id] = el;
            }}
            type="button"
            role="tab"
            aria-selected={on}
            tabIndex={on ? 0 : -1}
            onClick={() => select(t.id)}
            onKeyDown={(e) => {
              if (e.key === "ArrowRight") move(i + 1);
              else if (e.key === "ArrowLeft") move(i - 1);
              else if (e.key === "Home") move(0);
              else if (e.key === "End") move(tabs.length - 1);
              else return;
              e.preventDefault();
            }}
            className={cn(
              "flex items-center gap-2 border-b-2 px-3 py-2.5 text-body font-medium",
              on ? "border-accent text-fg" : "border-transparent text-muted hover:text-fg",
            )}
          >
            {t.label}
            <span className={cn("font-mono text-meta font-normal", on ? "text-muted" : "text-faint")}>
              {t.meta}
            </span>
          </button>
        );
      })}
    </div>
  );
}
