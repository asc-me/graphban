import type { ReactNode } from "react";

import { Collapsible } from "@/components/planner/Collapsible";
import { useProjectCtx } from "@/features/ProjectContext";

/**
 * The place header: H1 view name with the project tag beside it, purpose, one primary action.
 *
 * Views opt in by passing their name, a one-line purpose, and at most one action.
 * The project tag is read from context so the project is always visible (GRPH-941).
 *
 * The hairline border-b is a true split between page chrome and content. Do not wrap
 * this in another border-line-2 card.
 */
export function PlaceHeader({
  viewName,
  purpose,
  action,
  condensed = false,
}: {
  viewName: string;
  purpose: ReactNode;
  action?: ReactNode;
  /** Collapse the header (GRPH-1147). */
  condensed?: boolean;
}) {
  const { active } = useProjectCtx();
  const projectTag = active?.tag ?? "";

  return (
    <Collapsible collapsed={condensed}>
    <div className="flex flex-wrap items-center gap-x-4 gap-y-2 border-b border-line px-5 py-4" data-testid="place-header">
      {/* `min-w-[18rem]` floors the title block: without it the purpose collapses to its
          min-content width once the action is wide (Code graph's ~740px toolbar crushed the
          sentence into a 110px column), so a too-wide action wraps onto its own line instead. */}
      <div className="min-w-[18rem] flex-1">
        <div className="flex items-baseline gap-2.5">
          <h1 className="text-title font-semibold text-fg">{viewName}</h1>
          {projectTag && (
            <span className="font-mono text-small text-muted" data-testid="place-header-tag">
              {projectTag}
            </span>
          )}
        </div>
        <p className="mt-1 text-body text-muted">{purpose}</p>
      </div>
      {action && <div className="flex-none">{action}</div>}
    </div>
    </Collapsible>
  );
}
