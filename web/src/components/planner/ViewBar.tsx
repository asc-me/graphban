import { ChevronDown, Clock, Maximize2, Minimize2 } from "lucide-react";
import * as React from "react";

import { cn } from "@/lib/cn";

import {
  HIDE_OPTS,
  WINDOW_OPTS,
  scopeSummary,
  type ViewDef,
  type ViewsApi,
} from "./viewScope";

function Segmented({
  label,
  options,
  value,
  onPick,
}: {
  label: string;
  options: [number, string][];
  value: number;
  onPick: (n: number) => void;
}) {
  return (
    <div role="group" aria-label={label} className="flex gap-0.5 rounded-lg border border-control bg-surface-2 p-0.5">
      {options.map(([n, text]) => (
        <button
          key={n}
          type="button"
          aria-pressed={value === n}
          onClick={() => onPick(n)}
          className={cn(
            "h-6 flex-1 rounded-md px-2 font-mono text-[10.5px] transition-colors",
            value === n ? "bg-surface-4 text-fg" : "text-muted hover:text-fg-2",
          )}
        >
          {text}
        </button>
      ))}
    </div>
  );
}

/**
 * View tabs with live counts, the EDITED/Save/Reset/Set-default controls and the scope
 * popover. Shared by Tracker and PRDs; `doneWord` is "done" or "closed".
 */
export function ViewBar({
  views,
  countFor,
  doneWord,
  leading,
  condensed,
  onToggleCondense,
}: {
  views: ViewsApi;
  countFor: (v: ViewDef) => number;
  doneWord: "done" | "closed";
  /** Page-specific controls that sit before the tabs (layout toggle, columns). */
  leading?: React.ReactNode;
  condensed: boolean;
  onToggleCondense: () => void;
}) {
  const [open, setOpen] = React.useState(false);
  const root = React.useRef<HTMLDivElement>(null);

  React.useEffect(() => {
    if (!open) return;
    const onDown = (e: MouseEvent) => {
      if (!root.current?.contains(e.target as Node)) setOpen(false);
    };
    const onKey = (e: KeyboardEvent) => e.key === "Escape" && setOpen(false);
    document.addEventListener("mousedown", onDown);
    document.addEventListener("keydown", onKey);
    return () => {
      document.removeEventListener("mousedown", onDown);
      document.removeEventListener("keydown", onKey);
    };
  }, [open]);

  const { state } = views;
  const label = condensed ? "Show all controls" : "Condense controls";
  return (
    <div className="flex flex-none flex-wrap items-center gap-x-4 gap-y-2.5 border-b border-line px-5 py-2.5">
      {leading}
      <div role="tablist" aria-label="Views" className="flex flex-wrap gap-0.5 rounded-lg border border-control bg-surface-2 p-0.5">
        {views.views.map((v) => {
          const on = v.id === state.view;
          return (
            <button
              key={v.id}
              type="button"
              role="tab"
              aria-selected={on}
              title={v.title ?? "Saved view"}
              onClick={() => views.select(v)}
              className={cn(
                "inline-flex h-6 items-center gap-1.5 rounded-md px-2.5 text-[12px] transition-colors",
                on ? "bg-surface-4 text-fg" : "text-muted hover:text-fg-2",
              )}
            >
              {v.id === views.defaultId && <span aria-label="Default view" className="text-[9px] text-accent">★</span>}
              {v.label}
              <span className="font-mono text-[10px] text-faint">{countFor(v)}</span>
            </button>
          );
        })}
      </div>

      {!condensed && views.edited && (
        <div className="flex items-center gap-2">
          <span className="font-mono text-meta tracking-wide text-[#e0b34a]">EDITED</span>
          <button
            type="button"
            onClick={views.save}
            className="h-6 rounded-md border border-control bg-surface-2 px-2.5 text-[11.5px] text-fg-2 hover:border-control-hover"
          >
            Save as view
          </button>
          <button type="button" onClick={views.reset} className="h-6 px-2 text-[11.5px] text-muted hover:text-fg">
            Reset
          </button>
        </div>
      )}
      {!condensed && views.canSetDefault && (
        <button
          type="button"
          title="Open this page on this view"
          onClick={views.setDefault}
          className="h-6 rounded-md border border-control px-2.5 text-[11.5px] text-muted hover:border-control-hover hover:text-accent"
        >
          ☆ Set as default
        </button>
      )}

      <div className="flex-1" />

      <div ref={root} className="relative">
        <button
          type="button"
          aria-haspopup="dialog"
          aria-expanded={open}
          title="Time window and auto-hide"
          onClick={() => setOpen((o) => !o)}
          className="inline-flex h-[30px] items-center gap-2 whitespace-nowrap rounded-lg border border-control bg-surface-2 px-2.5 font-mono text-[10.5px] text-fg-2 hover:border-control-hover"
        >
          {!condensed && <Clock size={13} className="text-muted" />}
          {scopeSummary(state, doneWord)}
          {!condensed && <ChevronDown size={10} className="text-faint" />}
        </button>
        {open && (
          <div
            role="dialog"
            aria-label="Scope"
            className="absolute right-0 top-full z-20 mt-1.5 flex w-[280px] flex-col gap-3.5 rounded-[12px] border border-line-2 bg-surface-2 p-3.5 shadow-lg"
          >
            <div className="flex flex-col gap-1.5">
              <span className="font-mono text-[9px] tracking-wider text-faint">WINDOW</span>
              <Segmented label="Window" options={WINDOW_OPTS} value={state.window} onPick={(n) => views.patch({ window: n })} />
              <span className="text-small leading-snug text-muted">Only items with activity in this period.</span>
            </div>
            <div className="flex flex-col gap-1.5">
              <span className="font-mono text-[9px] tracking-wider text-faint">
                AUTO-HIDE {doneWord.toUpperCase()} AFTER
              </span>
              <Segmented label="Auto-hide" options={HIDE_OPTS} value={state.hide} onPick={(n) => views.patch({ hide: n })} />
            </div>
          </div>
        )}
      </div>

      <button
        type="button"
        aria-pressed={condensed}
        title={label}
        aria-label={label}
        onClick={() => {
          setOpen(false);
          onToggleCondense();
        }}
        className={cn(
          "relative inline-flex h-[30px] w-[30px] items-center justify-center rounded-lg border transition-colors",
          condensed
            ? "border-accent bg-accent text-black"
            : "border-control bg-surface-2 text-muted hover:border-control-hover hover:text-fg-2",
        )}
      >
        <Minimize2
          size={14}
          className={cn("absolute transition-all duration-[180ms] motion-reduce:transition-none", condensed ? "scale-50 opacity-0" : "scale-100 opacity-100")}
        />
        <Maximize2
          size={14}
          className={cn("absolute transition-all duration-[180ms] motion-reduce:transition-none", condensed ? "scale-100 opacity-100" : "scale-50 opacity-0")}
        />
      </button>
    </div>
  );
}

/** `Showing N · hidden by view: …` with a Show hidden button; nothing when nothing is hidden. */
export function ViewFooter({
  shown,
  hidden,
  onShowHidden,
}: {
  shown: number;
  hidden: string;
  onShowHidden: () => void;
}) {
  if (!hidden) return null;
  return (
    <div className="flex flex-none items-center gap-3 border-t border-line px-5 py-2 text-[11.5px] text-muted">
      <span>
        Showing {shown} · hidden by view: {hidden}
      </span>
      <button type="button" onClick={onShowHidden} className="text-accent hover:underline">
        Show hidden
      </button>
    </div>
  );
}
