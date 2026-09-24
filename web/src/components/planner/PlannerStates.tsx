import * as React from "react";

import { Button } from "@/components/ui/button";
import { cn } from "@/lib/cn";

/** Named fetch failure with retry — Home's counts-unavailable pattern. */
export function PlannerError({
  message,
  onRetry,
}: {
  message: string;
  onRetry: () => void;
}) {
  return (
    <div className="flex flex-col items-center gap-3 px-8 py-12 text-center" role="alert">
      <p className="text-[13px] text-st-blocked">{message}</p>
      <Button type="button" variant="outline" size="sm" onClick={onRetry}>
        Retry
      </Button>
    </div>
  );
}

/** True empty — invitation, not a failed search. */
export function PlannerEmpty({
  title,
  description,
  action,
}: {
  title: string;
  description: string;
  action?: React.ReactNode;
}) {
  return (
    <div className="flex flex-col items-center gap-3 px-8 py-16 text-center">
      <h2 className="text-[15px] font-semibold text-fg">{title}</h2>
      <p className="max-w-md text-[13px] leading-relaxed text-muted">{description}</p>
      {action}
    </div>
  );
}

/** Filtered-empty — distinct from project empty and palette miss. */
export function PlannerFilteredEmpty({
  message,
  onClear,
}: {
  message: string;
  onClear: () => void;
}) {
  return (
    <div className="flex flex-col items-center gap-3 px-8 py-12 text-center">
      <p className="text-[13px] text-muted">{message}</p>
      <Button type="button" variant="outline" size="sm" onClick={onClear}>
        Clear filter
      </Button>
    </div>
  );
}

function Pulse({ className }: { className?: string }) {
  return <div className={cn("animate-pulse rounded-md bg-surface-3", className)} />;
}

/** Tracker list skeleton: header/chips stay; 8 placeholder rows. */
export function TrackerListSkeleton({ rows = 8 }: { rows?: number }) {
  return (
    <div className="divide-y divide-line/70" aria-busy="true" aria-label="Loading tracker">
      {Array.from({ length: rows }, (_, i) => (
        <div key={i} className="flex items-center gap-3 px-4 py-3">
          <Pulse className="h-3.5 w-3.5" />
          <Pulse className="h-5 w-5 rounded-full" />
          <Pulse className="h-3 w-[52px]" />
          <Pulse className="h-3.5 flex-1" />
          <Pulse className="h-3 w-10" />
        </div>
      ))}
    </div>
  );
}

/** PRD list skeleton: 6 placeholder rows. */
export function PrdListSkeleton({ rows = 6 }: { rows?: number }) {
  return (
    <div className="space-y-2 p-4" aria-busy="true" aria-label="Loading PRDs">
      {Array.from({ length: rows }, (_, i) => (
        <div key={i} className="flex items-center gap-3 rounded-[12px] border border-line-2 bg-surface-2 px-4 py-3">
          <Pulse className="h-4 w-4" />
          <Pulse className="h-3 w-[52px]" />
          <Pulse className="h-3.5 flex-1" />
          <Pulse className="h-3 w-12" />
          <Pulse className="h-3 w-16" />
        </div>
      ))}
    </div>
  );
}

/** Memory review skeleton: header stays; card placeholders. */
export function MemoryReviewSkeleton({ cards = 4 }: { cards?: number }) {
  return (
    <div className="mx-auto flex max-w-3xl flex-col gap-2.5 p-5" aria-busy="true" aria-label="Loading memory review">
      {Array.from({ length: cards }, (_, i) => (
        <div key={i} className="rounded-[12px] border border-line-2 bg-surface-2 p-4">
          <Pulse className="mb-3 h-3 w-24" />
          <Pulse className="mb-2 h-3.5 w-full" />
          <Pulse className="h-3.5 w-4/5" />
        </div>
      ))}
    </div>
  );
}

/** Roadmap board skeleton: 3 phase columns with milestone placeholders. */
export function RoadmapBoardSkeleton({ columns = 3 }: { columns?: number }) {
  return (
    <div className="grid grid-cols-1 gap-4 md:grid-cols-3" aria-busy="true" aria-label="Loading roadmap">
      {Array.from({ length: columns }, (_, i) => (
        <div key={i} className="flex flex-col rounded-[14px] border border-line-2 bg-surface/40 p-4">
          <div className="mb-1 flex items-center gap-2">
            <Pulse className="h-2.5 w-2.5 rounded-[3px]" />
            <Pulse className="h-3.5 w-24" />
            <Pulse className="ml-auto h-3 w-12" />
          </div>
          <Pulse className="mb-3 h-3 w-20" />
          <Pulse className="mb-4 h-1.5 w-full rounded-full" />
          <div className="space-y-2">
            {Array.from({ length: 3 }, (_, j) => (
              <div key={j} className="flex items-start gap-2.5">
                <Pulse className="h-4 w-4 rounded-[5px]" />
                <div className="min-w-0 flex-1 space-y-1.5">
                  <Pulse className="h-3.5 w-full" />
                  <Pulse className="h-2.5 w-16" />
                </div>
              </div>
            ))}
          </div>
        </div>
      ))}
    </div>
  );
}

/** Live board skeleton: header and census chips stay; agent rows pulse. */
export function LiveBoardSkeleton({ rows = 4 }: { rows?: number }) {
  return (
    <div className="mx-auto flex max-w-3xl flex-col gap-5 p-5" aria-busy="true" aria-label="Loading live board">
      {Array.from({ length: rows }, (_, i) => (
        <div key={i} className="rounded-[12px] border border-line-2 bg-surface-2 p-4">
          <Pulse className="mb-3 h-4 w-32" />
          <Pulse className="mb-2 h-3 w-full" />
          <Pulse className="h-3 w-2/3" />
        </div>
      ))}
    </div>
  );
}

/** Lessons catalog skeleton: header stays; lesson rows pulse. */

/** Lessons catalog skeleton: header stays; lesson rows pulse. */
export function LessonsListSkeleton({ rows = 6 }: { rows?: number }) {
  return (
    <div className="mx-auto flex max-w-3xl flex-col gap-2.5 p-5" aria-busy="true" aria-label="Loading lessons">
      {Array.from({ length: rows }, (_, i) => (
        <div key={i} className="rounded-[12px] border border-line-2 bg-surface-2 px-4 py-3">
          <Pulse className="mb-2 h-3 w-20" />
          <Pulse className="h-3.5 w-full" />
        </div>
      ))}
    </div>
  );
}

/** Requests queue skeleton. */

/** Requests queue skeleton. */
export function RequestsListSkeleton({ rows = 6 }: { rows?: number }) {
  return (
    <div className="space-y-2 p-4" aria-busy="true" aria-label="Loading requests">
      {Array.from({ length: rows }, (_, i) => (
        <div key={i} className="flex items-center gap-3 rounded-[12px] border border-line-2 bg-surface-2 px-3 py-2.5">
          <Pulse className="h-10 w-10 rounded-lg" />
          <Pulse className="h-3 w-16" />
          <Pulse className="h-3.5 flex-1" />
        </div>
      ))}
    </div>
  );
}
