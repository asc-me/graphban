import * as React from "react";
import { Link } from "react-router-dom";

import { cn } from "@/lib/cn";
import type { LiveFeed, LiveFeedRow } from "@/lib/types";

import {
  STREAM_FILTERS,
  ageLabel,
  collapseRuns,
  matchesStreamFilter,
  type StreamFilter,
} from "./liveUtils";

const ITEM_ID = /^[A-Z][A-Z0-9]*-\d+$/;

export function LiveStreamPanel({
  feed,
  servedAt,
  trackerTo,
  focusId,
  focusLabel,
}: {
  feed: LiveFeed | undefined;
  servedAt: string;
  trackerTo: string;
  focusId: string | null;
  focusLabel: string | null;
}) {
  const [filter, setFilter] = React.useState<StreamFilter>("all");

  if (!feed) {
    return <div className="text-[12px] text-muted">Loading stream…</div>;
  }
  if (feed.state === "never") {
    return (
      <div className="text-[12px] text-muted">
        No calls recorded in the last {feed.retention_days} days
        {focusLabel ? ` for ${focusLabel}` : ""}.
      </div>
    );
  }

  const shown = feed.rows.filter((r) => matchesStreamFilter(r, filter));
  const runs = collapseRuns(shown);
  const clock = feed.served_at || servedAt;

  return (
    <div>
      {focusId && focusLabel && (
        <div className="mb-2 text-[11.5px] text-fg-2">
          Stream filtered to <span className="font-mono">{focusLabel}</span>
        </div>
      )}
      <div className="mb-2 flex flex-wrap items-center gap-1" role="group" aria-label="stream filter">
        {STREAM_FILTERS.map((f) => (
          <button
            key={f}
            type="button"
            aria-pressed={filter === f}
            onClick={() => setFilter(f)}
            className={cn(
              "rounded border px-1.5 py-0.5 font-mono text-[10px]",
              filter === f ? "border-control-hover bg-surface-3 text-fg" : "border-control text-muted hover:text-fg-2",
            )}
          >
            {f}
          </button>
        ))}
      </div>
      {runs.length === 0 ? (
        <div className="text-[12px] text-muted">No {filter} in this stream.</div>
      ) : (
        <ul className="max-h-64 space-y-0.5 overflow-y-auto border-l border-line-2 pl-2.5" aria-label="stream">
          {runs.map((run) => (
            <FeedRowView
              key={run.row.id}
              row={run.row}
              count={run.count}
              servedAt={clock}
              trackerTo={trackerTo}
            />
          ))}
        </ul>
      )}
    </div>
  );
}

function FeedRowView({
  row: r,
  count,
  servedAt,
  trackerTo,
}: {
  row: LiveFeedRow;
  count: number;
  servedAt: string;
  trackerTo: string;
}) {
  if (r.source === "reported") {
    return (
      <li className="flex flex-wrap items-baseline gap-x-2 text-[11.5px]">
        <span className="rounded border border-line-2 px-1 font-mono text-meta uppercase tracking-wide text-faint">
          reported
        </span>
        <span className="text-fg-2">{r.status}</span>
        {(r.files ?? []).length > 0 && (
          <span className="font-mono text-[10.5px] text-muted">{(r.files ?? []).join(", ")}</span>
        )}
        <span className="text-faint">{ageLabel(r.at, servedAt)}</span>
      </li>
    );
  }
  return (
    <li
      className={cn(
        "flex flex-wrap items-baseline gap-x-2 text-[11.5px]",
        !r.ok && "text-[color:var(--color-st-blocked)]",
      )}
    >
      <span className="font-mono">{r.tool}</span>
      {r.target && (
        !r.ok && ITEM_ID.test(r.target.split(/\s+/)[0] ?? "") ? (
          <Link to={trackerTo} className="text-muted underline decoration-dotted" title="open the tracker">
            {r.target}
          </Link>
        ) : (
          <span className="text-muted">{r.target}</span>
        )
      )}
      {count > 1 && (
        <span className="font-mono text-[10px] text-faint" aria-label={`${count} calls`}>
          ×{count}
        </span>
      )}
      {!r.ok && r.error_code && <span className="font-mono text-[10px]">{r.error_code}</span>}
      <span className="text-faint">{ageLabel(r.at, servedAt)}</span>
    </li>
  );
}
