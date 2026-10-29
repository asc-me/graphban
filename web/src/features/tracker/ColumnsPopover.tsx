import { SlidersHorizontal } from "lucide-react";
import * as React from "react";

import { cn } from "@/lib/cn";
import { useMe } from "@/lib/queries";
import { BOARD_COLUMNS, STATUS_META } from "@/lib/meta";
import type { Item } from "@/lib/types";

import { columnsSummary, type ColumnMode, type ColumnPrefsApi } from "./columnPrefs";

const MODES: { mode: ColumnMode; label: string }[] = [
  { mode: "show", label: "Show" },
  { mode: "collapse", label: "Collapse" },
  { mode: "hide", label: "Hide" },
];

function initials(name?: string): string {
  const i = (name ?? "").split(/\s+/).filter(Boolean).map((p) => p[0]).slice(0, 2).join("");
  return (i || "you").toUpperCase();
}

/** View-bar button + popover: per-user show / collapse / hide for each board column. */
export function ColumnsPopover({ prefs, items }: { prefs: ColumnPrefsApi; items: Item[] }) {
  const [open, setOpen] = React.useState(false);
  const root = React.useRef<HTMLDivElement>(null);
  const { data: me } = useMe();

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

  return (
    <div ref={root} className="relative mr-2">
      <button
        type="button"
        aria-haspopup="dialog"
        aria-expanded={open}
        onClick={() => setOpen((o) => !o)}
        className={cn(
          "inline-flex items-center gap-1.5 rounded-lg border px-2.5 py-1 text-[12px] transition-colors",
          prefs.hidden > 0
            ? "border-[rgba(245,181,68,0.4)] text-[#f5b544]"
            : "border-control text-muted hover:text-fg-2",
        )}
      >
        <SlidersHorizontal size={12} />
        Columns
        {(prefs.hidden > 0 || prefs.collapsed > 0) && (
          <span className="font-mono text-[10.5px]">{columnsSummary(prefs.hidden, prefs.collapsed).slice("Columns ".length)}</span>
        )}
      </button>
      {open && (
        <div
          role="dialog"
          aria-label="Board columns"
          className="absolute left-0 top-full z-20 mt-1.5 w-[320px] rounded-[12px] border border-line-2 bg-surface-2 p-3 shadow-lg"
        >
          <div className="flex items-center justify-between pb-2">
            <span className="text-body font-semibold text-fg">Board columns</span>
            <span className="font-mono text-[10px] uppercase tracking-wide text-faint">
              only you · {initials(me?.name)}
            </span>
          </div>
          <div className="flex flex-col gap-1">
            {BOARD_COLUMNS.map((status) => {
              const meta = STATUS_META[status];
              const current = prefs.modeOf(status);
              return (
                <div key={status} className="flex items-center gap-2 py-0.5">
                  <span className="h-2 w-2 rounded-full" style={{ background: meta.color }} />
                  <span className="flex-1 text-body text-fg-2">{meta.label}</span>
                  <span className="font-mono text-[10.5px] text-faint">
                    {items.filter((i) => i.status === status).length}
                  </span>
                  <div role="group" aria-label={`${meta.label} column`} className="inline-flex rounded-md border border-control p-0.5">
                    {MODES.map(({ mode, label }) => (
                      <button
                        key={mode}
                        type="button"
                        aria-pressed={current === mode}
                        onClick={() => prefs.setMode(status, mode)}
                        className={cn(
                          "h-5 rounded px-1.5 font-mono text-[10px]",
                          current === mode ? "bg-surface-4 text-fg" : "text-muted hover:text-fg-2",
                        )}
                      >
                        {label}
                      </button>
                    ))}
                  </div>
                </div>
              );
            })}
          </div>
          <div className="mt-2 flex items-center gap-2 border-t border-line pt-2">
            <span className="flex-1 text-[12px] text-faint">
              Hidden columns still count toward views. Their cards don't move.
            </span>
            <button type="button" onClick={prefs.reset} className="h-6 rounded-md border border-control px-2.5 text-[12px] text-muted hover:text-fg">
              Reset
            </button>
          </div>
        </div>
      )}
    </div>
  );
}
