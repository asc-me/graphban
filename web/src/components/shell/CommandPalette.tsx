import {
  Activity,
  BarChart3,
  BookMarked,
  Gauge,
  GitFork,
  LayoutGrid,
  ListChecks,
  Map,
  Network,
  Radar,
  ScrollText,
  Server,
  Settings,
  Star,
  Users,
} from "lucide-react";
import * as React from "react";
import { useNavigate } from "react-router-dom";

import { useProjectCtx } from "@/features/ProjectContext";
import { cn } from "@/lib/cn";
import { PALETTE_ITEM_PARAM } from "@/lib/palette-nav";
import { projectPath, settingsPath } from "@/lib/routes";
import { useConfig, useItems, usePrds } from "@/lib/queries";
import type { Item, PrdSummary } from "@/lib/types";

/** Self-host destinations — same routes the rail serves, in palette order. */
const DESTINATIONS = [
  { label: "Home", path: "/home", icon: <LayoutGrid size={14} /> },
  { label: "Tracker", path: "/tracker", icon: <ListChecks size={14} /> },
  { label: "Triage", path: "/triage", icon: <Radar size={14} /> },
  { label: "Requests", path: "/requests", icon: <Star size={14} /> },
  { label: "PRDs", path: "/prds", icon: <BarChart3 size={14} /> },
  { label: "Roadmap", path: "/roadmap", icon: <Map size={14} /> },
  { label: "Code graph", path: "/code", icon: <Network size={14} /> },
  { label: "Links", path: "/links", icon: <GitFork size={14} /> },
  { label: "Fleet.v2", path: "/fleet.v2", icon: <Users size={14} /> },
  { label: "Fleet.v1", path: "/fleet.v1", icon: <Users size={14} /> },
  { label: "Outposts", path: "/outposts", icon: <Server size={14} /> },
  { label: "Activity", path: "/activity", icon: <ScrollText size={14} /> },
  { label: "Live", path: "/live", icon: <Activity size={14} /> },
  { label: "Memory", path: "/memory-review", icon: <BookMarked size={14} /> },
  { label: "Lessons", path: "/lessons", icon: <BookMarked size={14} /> },
  { label: "Harness", path: "/harness", icon: <Gauge size={14} /> },
  { label: "Settings", path: settingsPath(), icon: <Settings size={14} /> },
] as const;

export const COMMAND_PALETTE_PLACEHOLDER = "Jump to a page, item, or PRD…";

/** Global shortcut — exported so sabotage tests can assert it exists. */
export function isCommandPaletteShortcut(e: KeyboardEvent): boolean {
  return e.key === "k" && (e.metaKey || e.ctrlKey);
}

function isTypingTarget(el: EventTarget | null): boolean {
  if (!(el instanceof HTMLElement)) return false;
  return el.tagName === "INPUT" || el.tagName === "TEXTAREA" || el.isContentEditable;
}

function matchesQuery(text: string, query: string): boolean {
  return text.toLowerCase().includes(query.toLowerCase());
}

type GotoRow = { kind: "goto"; id: string; label: string; path: string; icon: React.ReactNode };
type ItemRow = { kind: "item"; id: string; label: string; title: string };
type PrdRow = { kind: "prd"; id: string; label: string; title: string };
type PaletteRow = GotoRow | ItemRow | PrdRow;

function buildRows(
  query: string,
  items: Item[],
  prds: PrdSummary[],
  resolvePath: (flat: string) => string,
): { goto: GotoRow[]; items: ItemRow[]; prds: PrdRow[] } {
  const q = query.trim();
  const goto = DESTINATIONS.filter((d) => !q || matchesQuery(d.label, q)).map((d) => ({
    kind: "goto" as const,
    id: `goto:${d.path}`,
    label: d.label,
    path: d.path.startsWith("/settings") ? d.path : resolvePath(d.path.replace(/^\//, "")),
    icon: d.icon,
  }));
  const itemRows = items
    .filter((it) => !q || matchesQuery(it.id, q) || matchesQuery(it.title, q))
    .map((it) => ({
      kind: "item" as const,
      id: `item:${it.id}`,
      label: it.id,
      title: it.title,
    }));
  const prdRows = prds
    .filter((p) => !q || matchesQuery(p.id, q) || matchesQuery(p.title, q))
    .map((p) => ({
      kind: "prd" as const,
      id: `prd:${p.id}`,
      label: p.id,
      title: p.title,
    }));
  return { goto, items: itemRows, prds: prdRows };
}

export function CommandPalette({
  open,
  onOpenChange,
}: {
  open: boolean;
  onOpenChange: (open: boolean) => void;
}) {
  const navigate = useNavigate();
  const { active, activeId } = useProjectCtx();
  const { data: config } = useConfig();
  const hosted = config?.hosted_mode ?? false;
  const { data: items = [], isLoading: itemsLoading } = useItems(activeId);
  const { data: prds = [], isLoading: prdsLoading } = usePrds(activeId);

  const [query, setQuery] = React.useState("");
  const [activeIndex, setActiveIndex] = React.useState(0);
  const inputRef = React.useRef<HTMLInputElement>(null);
  const listRef = React.useRef<HTMLDivElement>(null);

  const resolvePath = React.useCallback(
    (view: string) => (hosted && active?.tag ? projectPath(active.tag, view) : `/${view}`),
    [hosted, active?.tag],
  );

  const groups = buildRows(query, items, prds, resolvePath);
  const flat = [...groups.goto, ...groups.items, ...groups.prds];

  React.useEffect(() => {
    if (!open) {
      setQuery("");
      setActiveIndex(0);
      return;
    }
    const t = window.setTimeout(() => inputRef.current?.focus(), 0);
    return () => window.clearTimeout(t);
  }, [open]);

  React.useEffect(() => {
    setActiveIndex((i) => (flat.length === 0 ? 0 : Math.min(i, flat.length - 1)));
  }, [flat.length, query]);

  React.useEffect(() => {
    function onKey(e: KeyboardEvent) {
      if (isCommandPaletteShortcut(e) && !isTypingTarget(e.target)) {
        e.preventDefault();
        onOpenChange(!open);
        return;
      }
      if (!open) return;
      if (e.key === "Escape") {
        e.preventDefault();
        onOpenChange(false);
      }
    }
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [open, onOpenChange]);

  function close() {
    onOpenChange(false);
  }

  function activate(row: PaletteRow) {
    close();
    if (row.kind === "goto") {
      navigate(row.path);
      return;
    }
    if (row.kind === "item") {
      navigate(`${resolvePath("tracker")}?${PALETTE_ITEM_PARAM}=${encodeURIComponent(row.label)}`);
      return;
    }
    navigate(resolvePath(`prds/${row.label}`));
  }

  function onInputKeyDown(e: React.KeyboardEvent<HTMLInputElement>) {
    if (e.key === "ArrowDown") {
      e.preventDefault();
      setActiveIndex((i) => (flat.length === 0 ? 0 : (i + 1) % flat.length));
    } else if (e.key === "ArrowUp") {
      e.preventDefault();
      setActiveIndex((i) => (flat.length === 0 ? 0 : (i - 1 + flat.length) % flat.length));
    } else if (e.key === "Enter" && flat[activeIndex]) {
      e.preventDefault();
      activate(flat[activeIndex]);
    }
  }

  React.useEffect(() => {
    const el = listRef.current?.querySelector(`[data-index="${activeIndex}"]`);
    el?.scrollIntoView({ block: "nearest" });
  }, [activeIndex]);

  if (!open) return null;

  const trimmed = query.trim();
  const itemsEmpty = !itemsLoading && items.length === 0;
  const prdsEmpty = !prdsLoading && prds.length === 0;

  return (
    <div className="fixed inset-0 z-[70] flex items-start justify-center pt-[14vh]">
      <div
        className="gb-overlay fixed inset-0 bg-[rgba(4,6,8,0.55)] backdrop-blur-[2px]"
        data-motion="instant"
        data-state="open"
        onClick={close}
        aria-hidden
      />
      <div
        role="dialog"
        aria-label="Command palette"
        className="gb-dialog-content relative z-[71] w-full max-w-[560px] rounded-[13px] border border-line-hover bg-surface-3 p-0 shadow-[0_24px_60px_rgba(0,0,0,0.6)]"
        data-motion="instant"
        data-state="open"
        onClick={(e) => e.stopPropagation()}
      >
        <div className="border-b border-line px-4 py-3">
          <input
            ref={inputRef}
            value={query}
            onChange={(e) => setQuery(e.target.value)}
            onKeyDown={onInputKeyDown}
            placeholder={COMMAND_PALETTE_PLACEHOLDER}
            aria-label="Command palette search"
            className="h-[36px] w-full rounded-[9px] border border-line-2 bg-surface-2 px-3 text-[13px] outline-none focus:border-line-hover focus:bg-surface-2"
            autoComplete="off"
            spellCheck={false}
          />
          <p className="sr-only" aria-live="polite" aria-atomic="true">
            {flat.length} result{flat.length === 1 ? "" : "s"}
          </p>
        </div>

        <div ref={listRef} className="max-h-[min(52vh,420px)] overflow-y-auto px-2 py-2">
          {groups.goto.length > 0 && (
            <PaletteGroup label="Go to">
              {groups.goto.map((row, i) => {
                const index = i;
                return (
                  <PaletteOption
                    key={row.id}
                    dataIndex={index}
                    active={activeIndex === index}
                    onMouseEnter={() => setActiveIndex(index)}
                    onClick={() => activate(row)}
                  >
                    <span className="text-muted">{row.kind === "goto" ? row.icon : null}</span>
                    <span>{row.label}</span>
                  </PaletteOption>
                );
              })}
            </PaletteGroup>
          )}

          <PaletteGroup label="Items">
            {itemsLoading ? (
              <PaletteMessage>Loading items…</PaletteMessage>
            ) : itemsEmpty ? (
              <PaletteMessage>No items in this project</PaletteMessage>
            ) : groups.items.length === 0 && trimmed ? (
              <PaletteMessage>{`No items match "${trimmed}"`}</PaletteMessage>
            ) : (
              groups.items.map((row, i) => {
                const index = groups.goto.length + i;
                return (
                  <PaletteOption
                    key={row.id}
                    dataIndex={index}
                    active={activeIndex === index}
                    onMouseEnter={() => setActiveIndex(index)}
                    onClick={() => activate(row)}
                  >
                    <span className="w-[88px] flex-none font-mono text-[11px] text-faint">{row.label}</span>
                    <span className="min-w-0 truncate text-fg-2">{row.title}</span>
                  </PaletteOption>
                );
              })
            )}
          </PaletteGroup>

          <PaletteGroup label="PRDs">
            {prdsLoading ? (
              <PaletteMessage>Loading PRDs…</PaletteMessage>
            ) : prdsEmpty && !trimmed ? (
              <PaletteMessage>No PRDs in this project</PaletteMessage>
            ) : groups.prds.length === 0 && trimmed ? (
              <PaletteMessage>{`No PRDs match "${trimmed}"`}</PaletteMessage>
            ) : (
              groups.prds.map((row, i) => {
                const index = groups.goto.length + groups.items.length + i;
                return (
                  <PaletteOption
                    key={row.id}
                    dataIndex={index}
                    active={activeIndex === index}
                    onMouseEnter={() => setActiveIndex(index)}
                    onClick={() => activate(row)}
                  >
                    <span className="w-[88px] flex-none font-mono text-[11px] text-faint">{row.label}</span>
                    <span className="min-w-0 truncate text-fg-2">{row.title}</span>
                  </PaletteOption>
                );
              })
            )}
          </PaletteGroup>

          {!trimmed && flat.length > 0 && (
            <p className="px-3 py-2 font-mono text-[10px] uppercase tracking-[0.05em] text-faint">
              Type to jump · ↑↓ move · Enter open · Esc close
            </p>
          )}
        </div>
      </div>
    </div>
  );
}

function PaletteGroup({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <div className="mb-2">
      <div className="px-3 py-1.5 font-mono text-[10px] uppercase tracking-[0.06em] text-faint">{label}</div>
      <div role="listbox" aria-label={label}>{children}</div>
    </div>
  );
}

function PaletteMessage({ children }: { children: React.ReactNode }) {
  return <div className="px-3 py-2 text-[12.5px] text-muted">{children}</div>;
}

function PaletteOption({
  children,
  active,
  dataIndex,
  onMouseEnter,
  onClick,
}: {
  children: React.ReactNode;
  active: boolean;
  dataIndex: number;
  onMouseEnter: () => void;
  onClick: () => void;
}) {
  return (
    <button
      type="button"
      role="option"
      data-index={dataIndex}
      aria-selected={active}
      onMouseEnter={onMouseEnter}
      onClick={onClick}
      className={cn(
        "flex w-full items-center gap-2.5 rounded-[9px] px-3 py-2 text-left text-[13px]",
        active ? "bg-surface-4 text-fg" : "text-fg-2 hover:bg-surface-2",
      )}
    >
      {children}
    </button>
  );
}
