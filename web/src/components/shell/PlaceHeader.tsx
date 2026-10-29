import type { ReactNode } from "react";

import { Collapsible } from "@/components/planner/Collapsible";
import { useProjectCtx } from "@/features/ProjectContext";

/**
 * The place header: project · view · purpose · one primary action.
 *
 * Views opt in by passing their name, a one-line purpose, and at most one action.
 * The project name is read from context so it is always visible — not buried in the
 * rail dropdown where a long list hides it (GRPH-941).
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
  const projectName = active?.name ?? "";

  return (
    <Collapsible collapsed={condensed}>
    <div className="border-b border-line px-5 py-4" data-testid="place-header">
      <div className="flex items-baseline gap-2">
        {projectName && (
          <>
            <span className="text-body text-muted">{projectName}</span>
            <span className="text-faint-2">·</span>
          </>
        )}
        <h1 className="text-[15px] font-semibold tracking-tight text-fg">{viewName}</h1>
      </div>
      {/* `flex-1` alone lets the purpose collapse to its MIN-CONTENT width — the longest word
          — once the action is wide enough, which on Code graph (a find box, Hubs and five edge
          chips, ~740px) crushed the sentence into a 110px column thirteen lines tall. A floor
          plus wrapping drops a too-wide action onto its own line instead, which is the only
          outcome that keeps the sentence readable at any width. */}
      <div className="mt-1 flex flex-wrap items-center gap-x-3 gap-y-2">
        <p className="min-w-[18rem] flex-1 text-body text-muted">{purpose}</p>
        {action && <div className="flex-none">{action}</div>}
      </div>
    </div>
    </Collapsible>
  );
}
