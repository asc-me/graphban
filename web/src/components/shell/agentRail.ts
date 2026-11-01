import * as React from "react";

import { viewSegment } from "@/lib/routes";

/**
 * Views that draw their own right-hand aside. The code graph's Inspect / Ask column is
 * 300px, and the agent rail opening beside it on arrival took the width the canvas needs.
 */
const OWN_ASIDE = new Set(["code"]);

export function hasOwnAside(pathname: string): boolean {
  return OWN_ASIDE.has(viewSegment(pathname));
}

/**
 * The agent rail's open state. Closed by default on every view: it takes ~340px from
 * whatever the page draws, and the top-bar Agent toggle opens it. On a view with its own
 * aside, arriving closes it again even after the toggle opened it elsewhere — only the
 * arrival is not the user's choice.
 */
export function useAgentRail(pathname: string) {
  const ownAside = hasOwnAside(pathname);
  const [open, setOpen] = React.useState(false);
  React.useEffect(() => {
    if (ownAside) setOpen(false);
  }, [ownAside]);
  return [open, setOpen] as const;
}
