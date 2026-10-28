import { ChevronDown } from "lucide-react";
import { useLocation } from "react-router-dom";

import { ProjectSwitcher } from "@/components/shell/ProjectSwitcher";
import { useProjectCtx } from "@/features/ProjectContext";
import { cn } from "@/lib/cn";
import { useOrgs } from "@/lib/queries";
import { consoleHref, orgPath, placeFromPath } from "@/lib/routes";

/**
 * The project bar: which project you are in, and the way out of it.
 *
 * Drilling from the org into a project is the primary motion of the hosted product, so
 * the breadcrumb runs org → project and both halves are links. Switching keeps the
 * current view — moving from one project's tracker to another's tracker is the common
 * case, and dumping you at a project home every time would make the switcher hostile.
 *
 * Switching is **navigation**, not a stored variable: the URL changes and everything
 * downstream re-derives from it. That is what makes a switch atomic rather than a state
 * change that some component might read a render too early.
 */
export function ProjectBar() {
  const { active } = useProjectCtx();
  const { pathname } = useLocation();
  // The bar only renders on a hosted deployment, where the org list is the org.
  const { data: orgs = [] } = useOrgs();

  if (!active) return null;
  const place = placeFromPath(pathname);

  return (
    <div className="relative z-30 flex flex-none items-center gap-3 border-b border-line bg-surface/60">
      <span className="w-1 self-stretch" style={{ background: active.accent }} />
      <div className="flex items-center gap-2.5 py-2 pl-2.5">
        <a
          href={consoleHref(orgPath())}
          className="font-mono text-meta uppercase tracking-[0.06em] text-faint hover:text-accent"
        >
          org
        </a>
        <span className="font-mono text-[10px] text-faint-2">/</span>
        <ProjectSwitcher
          placement="below"
          orgName={orgs[0]?.name}
          hosted
          trigger={({ ref, ...t }) => (
            <button
              ref={ref}
              type="button"
              aria-label="Switch project"
              {...t}
              className={cn(
                "flex items-center gap-2.5 rounded-lg border px-2.5 py-1 hover:border-control-hover",
                t["aria-expanded"] ? "border-control-hover bg-surface-2" : "border-control",
              )}
            >
              <span
                className="h-[7px] w-[7px] shrink-0 rounded-sm"
                style={{ background: active.accent }}
              />
              <span className="text-[13.5px] font-semibold tracking-[-0.15px]">{active.name}</span>
              <span className="rounded border border-line-2 px-1.5 py-px font-mono text-meta text-muted">
                {active.tag}
              </span>
              <ChevronDown
                size={11}
                className={cn("shrink-0 text-muted", t["aria-expanded"] && "rotate-180")}
              />
            </button>
          )}
        />
      </div>

      {place && (
        <>
          <span className="text-small text-faint-2">/</span>
          <span className="flex items-baseline gap-1.5 text-[12px] text-muted">
            <span>{place.section}</span>
            <span className="text-faint-2">·</span>
            <span>{place.page}</span>
          </span>
        </>
      )}

      <div className="min-w-0 flex-1" />
    </div>
  );
}

