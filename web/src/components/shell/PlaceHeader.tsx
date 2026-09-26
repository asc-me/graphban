import type { ReactNode } from "react";

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
}: {
  viewName: string;
  purpose: ReactNode;
  action?: ReactNode;
}) {
  const { active } = useProjectCtx();
  const projectName = active?.name ?? "";

  return (
    <div className="flex-none border-b border-line px-5 py-4" data-testid="place-header">
      <div className="flex items-baseline gap-2">
        {projectName && (
          <>
            <span className="text-[12.5px] text-muted">{projectName}</span>
            <span className="text-faint-2">·</span>
          </>
        )}
        <h1 className="text-[15px] font-semibold tracking-tight text-fg">{viewName}</h1>
      </div>
      <div className="mt-1 flex items-center gap-3">
        <p className="flex-1 text-[12.5px] text-muted">{purpose}</p>
        {action && <div className="flex-none">{action}</div>}
      </div>
    </div>
  );
}
