import { Columns3, List } from "lucide-react";
import * as React from "react";
import { useSearchParams } from "react-router-dom";

import {
  PlannerEmpty,
  PlannerError,
  PlannerFilteredEmpty,
  TrackerBoardSkeleton,
  TrackerListSkeleton,
} from "@/components/planner/PlannerStates";
import { PlaceHeader } from "@/components/shell/PlaceHeader";
import { Dot } from "@/components/ui/badge";
import { useProjectCtx } from "@/features/ProjectContext";
import { cn } from "@/lib/cn";
import { errorDetail } from "@/lib/errors";
import { STATUS_META, STATUS_ORDER } from "@/lib/meta";
import { PALETTE_ITEM_PARAM } from "@/lib/palette-nav";
import { useItems, useReorderItems, useUpdateItem } from "@/lib/queries";
import type { Item, Status } from "@/lib/types";

import { ItemDetailPanel } from "./ItemDetailPanel";
import { ItemRow } from "./ItemRow";
import { NewItemDialog } from "./NewItemDialog";
import { OwnerCloseDialog } from "./OwnerCloseDialog";
import { useColumnPrefs } from "./columnPrefs";
import { ColumnsPopover } from "./ColumnsPopover";
import { TrackerBoard } from "./TrackerBoard";

/** Typed waits can PATCH `done` without an attestation — a human emptied them. */
const WAIT_TAGS = new Set(["wait:merge", "wait:decision", "wait:secret", "wait:access", "wait:deploy"]);

function isHumanWait(item: Item): boolean {
  return (item.tags || []).some((t) => WAIT_TAGS.has(String(t).toLowerCase()));
}

function hasValidAttestation(item: Item): boolean {
  return (item.evidence || []).some(
    (e) => e.kind === "attestation" && Boolean(e.adapter) && Boolean(e.commit),
  );
}

/** `done` that would 409 must not be PATCHed; route through owner-close (GRPH-1030). */
function canPatchDone(item: Item): boolean {
  return isHumanWait(item) || hasValidAttestation(item);
}

/** Planner-facing statuses: live work, not backlog/next/done. */
const ACTIVE_STATUSES: Status[] = ["in_progress", "review", "blocked"];

type TrackerFilter = Status | "all" | "active";

type TrackerLayout = "board" | "list";

/** Per-user layout choice; localStorage until the GRPH-1133 prefs store exists. */
export const TRACKER_LAYOUT_KEY = "gb_tracker_layout";

function readLayout(): TrackerLayout {
  try {
    return localStorage.getItem(TRACKER_LAYOUT_KEY) === "list" ? "list" : "board";
  } catch {
    return "board";
  }
}

export function TrackerView() {
  const [searchParams, setSearchParams] = useSearchParams();
  const { activeId } = useProjectCtx();
  const { data: items = [], isLoading, isError, refetch } = useItems(activeId);
  const update = useUpdateItem();
  const reorder = useReorderItems();

  const [filter, setFilter] = React.useState<TrackerFilter>("active");
  const [selectedId, setSelectedId] = React.useState<string | null>(null);
  const [dragId, setDragId] = React.useState<string | null>(null);
  const [overId, setOverId] = React.useState<string | null>(null);
  const [closing, setClosing] = React.useState<Item | null>(null);
  const [layout, setLayoutState] = React.useState<TrackerLayout>(readLayout);
  const columns = useColumnPrefs();
  const [refusal, setRefusal] = React.useState<string | null>(null);

  function setLayout(next: TrackerLayout) {
    setLayoutState(next);
    try {
      localStorage.setItem(TRACKER_LAYOUT_KEY, next);
    } catch {
      /* storage unavailable — the choice lasts this session */
    }
  }

  React.useEffect(() => {
    const id = searchParams.get(PALETTE_ITEM_PARAM);
    if (!id) return;
    setSelectedId(id);
    const next = new URLSearchParams(searchParams);
    next.delete(PALETTE_ITEM_PARAM);
    setSearchParams(next, { replace: true });
  }, [searchParams, setSearchParams]);

  const ordered = React.useMemo(
    () => [...items].sort((a, b) => a.sort_order - b.sort_order),
    [items],
  );

  const visible = ordered.filter((it) => {
    if (filter === "all") return true;
    if (filter === "active") return ACTIVE_STATUSES.includes(it.status);
    return it.status === filter;
  });

  const counts = React.useMemo(() => {
    const c: Record<string, number> = {};
    for (const it of ordered) c[it.status] = (c[it.status] ?? 0) + 1;
    return c;
  }, [ordered]);

  const activeCount = React.useMemo(
    () => ACTIVE_STATUSES.reduce((n, s) => n + (counts[s] ?? 0), 0),
    [counts],
  );

  const selected = items.find((i) => i.id === selectedId) ?? null;
  const isEmpty = !isLoading && !isError && ordered.length === 0;
  const isFilteredEmpty = !isLoading && !isError && ordered.length > 0 && visible.length === 0;

  function setStatus(item: Item, status: Status) {
    if (status === "done" && !canPatchDone(item)) {
      setClosing(item);
      return;
    }
    setRefusal(null);
    // A refused move (wait-dep, review gate) rolls back in useUpdateItem; say why here.
    update.mutate(
      { id: item.id, body: { status } },
      { onError: (err) => setRefusal(errorDetail(err, `Could not move ${item.id}`)) },
    );
  }

  function reorderIds(ids: string[]) {
    reorder.mutate(ids);
  }

  function moveItem(id: string, delta: -1 | 1) {
    const ids = ordered.map((i) => i.id);
    const idx = ids.indexOf(id);
    if (idx < 0) return;
    const next = idx + delta;
    if (next < 0 || next >= ids.length) return;
    ids.splice(next, 0, ids.splice(idx, 1)[0]);
    reorderIds(ids);
  }

  function onDrop() {
    if (!dragId || !overId || dragId === overId) {
      setDragId(null);
      setOverId(null);
      return;
    }
    const ids = ordered.map((i) => i.id);
    const from = ids.indexOf(dragId);
    const to = ids.indexOf(overId);
    ids.splice(to, 0, ids.splice(from, 1)[0]);
    reorderIds(ids);
    setDragId(null);
    setOverId(null);
  }

  React.useEffect(() => {
    function onKey(e: KeyboardEvent) {
      if (!selectedId || !e.altKey) return;
      if (e.key === "ArrowUp") {
        e.preventDefault();
        moveItem(selectedId, -1);
      } else if (e.key === "ArrowDown") {
        e.preventDefault();
        moveItem(selectedId, 1);
      }
    }
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [selectedId, ordered]);

  const emptyState = (
    <PlannerEmpty
      title="No items yet"
      description="The tracker is your project's linear stream — one ordered list of work. Create the first item to start grooming."
      action={<NewItemDialog />}
    />
  );

  return (
    <div className="relative flex h-full min-h-0 flex-col">
      <PlaceHeader
        viewName="Tracker"
        purpose={
          layout === "board"
            ? "Work by status. Drag a card to another column to move it."
            : "One linear stream. Priority + recency. Drag to reorder, click a status to advance it."
        }
        action={<NewItemDialog />}
      />

      <div className="flex flex-none flex-wrap items-center gap-1.5 border-b border-line px-5 py-2.5">
        <LayoutToggle layout={layout} onChange={setLayout} />
        {layout === "board" && <ColumnsPopover prefs={columns} items={items} />}
        {layout === "list" && (
          <>
            <FilterChip active={filter === "all"} onClick={() => setFilter("all")} label="All" count={ordered.length} />
            <FilterChip
              active={filter === "active"}
              onClick={() => setFilter("active")}
              label="Active"
              count={activeCount}
            />
            {STATUS_ORDER.map((s) => (
              <FilterChip
                key={s}
                active={filter === s}
                onClick={() => setFilter(s)}
                label={STATUS_META[s].label}
                count={counts[s] ?? 0}
                color={STATUS_META[s].color}
              />
            ))}
          </>
        )}
      </div>

      {refusal && (
        <div
          role="alert"
          className="flex flex-none items-center gap-3 border-b border-line bg-[rgba(255,107,107,0.06)] px-5 py-2 text-body text-st-blocked"
        >
          <span className="min-w-0 flex-1">{refusal}</span>
          <button type="button" onClick={() => setRefusal(null)} className="text-faint hover:text-fg">
            Dismiss
          </button>
        </div>
      )}

      {layout === "board" ? (
        isLoading ? (
          <TrackerBoardSkeleton />
        ) : isError ? (
          <PlannerError message="Items unavailable" onRetry={() => refetch()} />
        ) : isEmpty ? (
          emptyState
        ) : (
          <TrackerBoard
            items={ordered}
            selectedId={selectedId}
            onSelect={setSelectedId}
            onStatus={setStatus}
            columns={columns}
          />
        )
      ) : (
        <div className="min-h-0 flex-1 overflow-y-auto">
          {isLoading ? (
            <TrackerListSkeleton rows={items.length > 0 ? items.length : 8} />
          ) : isError ? (
            <PlannerError message="Items unavailable" onRetry={() => refetch()} />
          ) : isEmpty ? (
            emptyState
          ) : isFilteredEmpty ? (
            <PlannerFilteredEmpty
              message={`No items match this filter${filterLabel(filter)}.`}
              onClear={() => setFilter("all")}
            />
          ) : (
            visible.map((item) => {
              const globalIndex = ordered.findIndex((i) => i.id === item.id);
              return (
                <ItemRow
                  key={item.id}
                  item={item}
                  selected={item.id === selectedId}
                  onSelect={() => setSelectedId(item.id)}
                  onStatus={(s) => setStatus(item, s)}
                  onMoveUp={() => moveItem(item.id, -1)}
                  onMoveDown={() => moveItem(item.id, 1)}
                  canMoveUp={globalIndex > 0}
                  canMoveDown={globalIndex >= 0 && globalIndex < ordered.length - 1}
                  dragging={dragId === item.id}
                  dragOver={overId === item.id && dragId !== item.id}
                  dragHandlers={{
                    onDragStart: () => setDragId(item.id),
                    onDragEnter: () => setOverId(item.id),
                    onDragOver: (e) => e.preventDefault(),
                    onDragEnd: onDrop,
                  }}
                />
              );
            })
          )}
        </div>
      )}

      {selected && (
        <ItemDetailPanel
          item={selected}
          open={!!selected}
          onClose={() => setSelectedId(null)}
          onStatus={(s) => setStatus(selected, s)}
        />
      )}

      <OwnerCloseDialog item={closing} open={!!closing} onClose={() => setClosing(null)} />
    </div>
  );
}

function filterLabel(filter: TrackerFilter): string {
  if (filter === "all") return "";
  if (filter === "active") return " (Active)";
  return ` (${STATUS_META[filter].label})`;
}

function LayoutToggle({
  layout,
  onChange,
}: {
  layout: TrackerLayout;
  onChange: (l: TrackerLayout) => void;
}) {
  return (
    <div role="group" aria-label="Layout" className="mr-2 inline-flex rounded-lg border border-control bg-surface-2 p-0.5">
      {(["board", "list"] as const).map((l) => (
        <button
          key={l}
          type="button"
          aria-pressed={layout === l}
          onClick={() => onChange(l)}
          className={cn(
            "inline-flex items-center gap-1.5 rounded-md px-2.5 py-0.5 text-[12px] transition-colors",
            layout === l ? "bg-surface-4 text-fg" : "text-muted hover:text-fg-2",
          )}
        >
          {l === "board" ? <Columns3 size={12} /> : <List size={12} />}
          {l === "board" ? "Board" : "List"}
        </button>
      ))}
    </div>
  );
}

function FilterChip({
  active,
  onClick,
  label,
  count,
  color,
}: {
  active: boolean;
  onClick: () => void;
  label: string;
  count: number;
  color?: string;
}) {
  return (
    <button
      onClick={onClick}
      className={cn(
        "inline-flex items-center gap-1.5 rounded-lg border px-2.5 py-1 text-[12px] transition-colors",
        active
          ? "border-control-hover bg-surface-3 text-fg"
          : "border-control bg-surface-2 text-muted hover:border-control-hover hover:text-fg-2",
      )}
    >
      {color && <Dot color={color} />}
      {label}
      <span className="font-mono text-[10px] text-faint">{count}</span>
    </button>
  );
}
