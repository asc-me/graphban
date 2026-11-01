import { ChevronLeft, ChevronRight, EyeOff } from "lucide-react";
import * as React from "react";

import { Avatar } from "@/components/ui/avatar";
import { cn } from "@/lib/cn";
import { BOARD_COLUMNS, CHECK_COLOR, PR_STATE_COLOR, STATUS_META } from "@/lib/meta";
import type { Item, Status } from "@/lib/types";

import type { ColumnPrefsApi } from "./columnPrefs";

function initials(name: string | undefined): string {
  return (name ?? "?").split(" ").map((p) => p[0]).slice(0, 2).join("");
}

/** Board layout: one column per status; dropping a card on a column sets that status. */
export function TrackerBoard({
  items,
  selectedId,
  onSelect,
  onStatus,
  columns,
  statuses = BOARD_COLUMNS,
  doneHidden = 0,
  hideDays = 0,
  onShowHidden,
}: {
  items: Item[];
  selectedId: string | null;
  onSelect: (id: string) => void;
  onStatus: (item: Item, status: Status) => void;
  columns: ColumnPrefsApi;
  /** Columns the active view/chip keeps; the rest are not rendered. */
  statuses?: readonly Status[];
  /** Done cards the view's auto-hide left out; the Done column says so. */
  doneHidden?: number;
  hideDays?: number;
  onShowHidden?: () => void;
}) {
  const [dragId, setDragId] = React.useState<string | null>(null);
  const [overCol, setOverCol] = React.useState<Status | null>(null);

  function drop(status: Status) {
    const item = items.find((i) => i.id === dragId);
    setDragId(null);
    setOverCol(null);
    if (item && item.status !== status) onStatus(item, status);
  }

  return (
    <div className="flex min-h-0 flex-1 items-stretch gap-3 overflow-x-auto px-5 py-4">
      {statuses.map((status) => {
        const mode = columns.modeOf(status);
        if (mode === "hide") return null;
        const meta = STATUS_META[status];
        const cards = items.filter((i) => i.status === status);
        if (mode === "collapse") {
          return (
            <button
              key={status}
              type="button"
              aria-label={`Expand ${meta.label} (${cards.length})`}
              title={`Expand ${meta.label}`}
              onClick={() => columns.setMode(status, "show")}
              className="flex min-h-[320px] w-11 flex-none flex-col items-center gap-2 rounded-[12px] border border-control bg-surface/40 py-3 hover:border-control-hover"
            >
              <ChevronRight size={12} className="text-faint" />
              <span className="h-2 w-2 rounded-full" style={{ background: meta.color }} />
              <span className="font-mono text-[10.5px] text-faint">{cards.length}</span>
              <span className="text-body font-semibold text-fg-2 [writing-mode:vertical-rl]">{meta.label}</span>
            </button>
          );
        }
        return (
          <section
            key={status}
            aria-label={meta.label}
            onDragOver={(e) => {
              e.preventDefault();
              setOverCol(status);
            }}
            onDragLeave={() => setOverCol((c) => (c === status ? null : c))}
            onDrop={(e) => {
              e.preventDefault();
              drop(status);
            }}
            className={cn(
              "flex min-h-[320px] min-w-[232px] flex-1 flex-col gap-2 rounded-[12px] border px-2 py-2.5",
              overCol === status && dragId
                ? "bg-surface-2"
                : "border-line-2 bg-surface/40",
            )}
            style={overCol === status && dragId ? { borderColor: meta.color } : undefined}
          >
            <div className="flex items-center gap-2 pb-1 pl-1">
              <span className="h-2 w-2 rounded-full" style={{ background: meta.color }} />
              <span className="text-body font-semibold text-fg-2">{meta.label}</span>
              <span className="font-mono text-[10.5px] text-faint">{cards.length}</span>
              <span className="flex-1" />
              <button
                type="button"
                aria-label={`Collapse ${meta.label}`}
                title="Collapse"
                onClick={() => columns.setMode(status, "collapse")}
                className="flex h-[22px] w-[22px] items-center justify-center rounded text-faint hover:bg-surface-4 hover:text-fg"
              >
                <ChevronLeft size={12} />
              </button>
              <button
                type="button"
                aria-label={`Hide ${meta.label}`}
                title="Hide column"
                onClick={() => columns.setMode(status, "hide")}
                className="flex h-[22px] w-[22px] items-center justify-center rounded text-faint hover:bg-surface-4 hover:text-fg"
              >
                <EyeOff size={12} />
              </button>
            </div>
            {cards.map((item) => (
              <BoardCard
                key={item.id}
                item={item}
                selected={item.id === selectedId}
                dragging={item.id === dragId}
                onSelect={() => onSelect(item.id)}
                onDragStart={(e) => {
                  e.dataTransfer?.setData("text/plain", item.id);
                  setDragId(item.id);
                }}
                onDragEnd={() => {
                  setDragId(null);
                  setOverCol(null);
                }}
              />
            ))}
            {cards.length === 0 && (
              <div className="flex min-h-[72px] flex-1 items-center justify-center rounded-[10px] border border-dashed border-line-2 font-mono text-[10.5px] text-faint-2">
                Drop here
              </div>
            )}
            {status === "done" && doneHidden > 0 && onShowHidden && (
              <button
                type="button"
                onClick={onShowHidden}
                className="mt-auto w-full px-1 text-left font-mono text-[10px] text-faint hover:text-accent"
              >
                + {doneHidden} done more than {hideDays}d ago · show
              </button>
            )}
          </section>
        );
      })}
    </div>
  );
}

function BoardCard({
  item,
  selected,
  dragging,
  onSelect,
  onDragStart,
  onDragEnd,
}: {
  item: Item;
  selected: boolean;
  dragging: boolean;
  onSelect: () => void;
  onDragStart: (e: React.DragEvent) => void;
  onDragEnd: () => void;
}) {
  const owner = item.claimed_by || item.assignee;
  return (
    <div
      draggable
      onDragStart={onDragStart}
      onDragEnd={onDragEnd}
      onClick={onSelect}
      aria-selected={selected}
      data-testid={`card-${item.id}`}
      className={cn(
        "flex cursor-grab flex-col gap-2 rounded-[10px] border bg-surface-2 px-[11px] py-2.5 transition-colors",
        selected ? "border-accent/55" : "border-line-2 hover:border-control-hover",
        dragging && "opacity-40",
      )}
    >
      <div className="flex items-center gap-1.5">
        <span className="font-mono text-[10.5px] text-faint">{item.id}</span>
        {item.fidelity === "high" && (
          <span
            className="rounded border border-[rgba(167,139,250,0.3)] px-[5px] font-mono text-[9px] tracking-wide text-[#c9b8ff]"
            title="Needs a prototype before it can be specced"
          >
            PROTO
          </span>
        )}
        <span className="flex-1" />
        {item.pr && (
          <span
            className="flex items-center gap-1 font-mono text-[10px]"
            style={{ color: PR_STATE_COLOR[item.pr.state] ?? "#8b949e" }}
            title={`PR #${item.pr.number} · ${item.pr.state} · checks ${item.pr.checks}`}
          >
            <span
              className="h-[5px] w-[5px] rounded-full"
              style={{ background: CHECK_COLOR[item.pr.checks] ?? "#8b949e" }}
            />
            #{item.pr.number}
          </span>
        )}
      </div>
      <div className="text-[13px] leading-[1.4] text-fg-2 [text-wrap:pretty]">{item.title}</div>
      <div className="flex flex-wrap items-center gap-[5px]">
        {item.tags.slice(0, 3).map((t) => (
          <span key={t} className="rounded-[5px] border border-line-2 px-1.5 py-px font-mono text-[10px] text-muted">
            {t}
          </span>
        ))}
        {item.effort > 0 && (
          <span className="rounded-md bg-surface-4 px-1.5 py-px font-mono text-[10px] text-muted-2" title="effort">
            {item.effort}
          </span>
        )}
        <span className="flex-1" />
        <span
          className={cn(
            "flex items-center gap-1 font-mono text-[10px]",
            item.claimed_by ? "text-accent" : "text-faint",
          )}
          title={item.claimed_by ? `Claimed by ${item.claimed_by}` : owner ? `Assigned to ${owner}` : "Unassigned"}
        >
          {item.claimed_by && <span className="blink h-[5px] w-[5px] rounded-full bg-accent" />}
          <span className="max-w-[80px] truncate">{owner || "unassigned"}</span>
        </span>
        {item.reporter?.avatar && (
          <Avatar initials={initials(item.reporter.name)} color={item.reporter.avatar} size={20} />
        )}
      </div>
    </div>
  );
}
