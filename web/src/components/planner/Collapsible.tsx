import type { ReactNode } from "react";

import { cn } from "@/lib/cn";

/**
 * Collapses to zero height with a grid-rows transition (240ms) and an opacity fade (180ms).
 * Collapsed content is `inert` and `aria-hidden` so it leaves the tab order and the a11y tree.
 * Reduced motion drops both transitions.
 */
export function Collapsible({ collapsed, children }: { collapsed: boolean; children: ReactNode }) {
  return (
    <div
      className={cn(
        "grid flex-none transition-[grid-template-rows] duration-[240ms] ease-out motion-reduce:transition-none",
        collapsed ? "grid-rows-[0fr]" : "grid-rows-[1fr]",
      )}
    >
      <div
        aria-hidden={collapsed || undefined}
        inert={collapsed}
        className={cn(
          "min-h-0 overflow-hidden transition-opacity duration-[180ms] motion-reduce:transition-none",
          collapsed ? "opacity-0" : "opacity-100",
        )}
      >
        {children}
      </div>
    </div>
  );
}
