import * as React from "react";

import { viewFromPath } from "@/lib/routes";

/**
 * Views that draw their own right-hand aside. The code graph's Inspect / Ask column is
 * 300px, and the agent rail opening beside it on arrival took the width the canvas needs.
 */
const OWN_ASIDE = new Set(["code"]);

export function hasOwnAside(pathname: string): boolean {
  return OWN_ASIDE.has(viewFromPath(pathname).split("/")[0]);
}

/**
 * The agent rail's open state. Open by default, except on a view with its own aside:
 * there it starts closed, and arriving at one closes it again. The toggle still opens it
 * there — only the arrival is not the user's choice.
 */
export function useAgentRail(pathname: string) {
  const ownAside = hasOwnAside(pathname);
  const [open, setOpen] = React.useState(!ownAside);
  React.useEffect(() => {
    if (ownAside) setOpen(false);
  }, [ownAside]);
  return [open, setOpen] as const;
}
