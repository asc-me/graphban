import * as React from "react";
import { createPortal } from "react-dom";

import { cn } from "@/lib/cn";
import { errorDetail } from "@/lib/errors";
import { usePutOnLine } from "@/lib/queries";
import type { Item, UnscopedFixes as UnscopedFixesData } from "@/lib/types";

/**
 * What a refused "Put on the maintenance line" says on its row (GRPH-1744). The rule is the
 * server's: its refusal names what the fix is missing, and that is rewritten into the design's
 * sentence. Any other refusal is shown as the server wrote it.
 */
export function putRefusalText(key: string, err: unknown): string {
  const detail = errorDetail(err, `Could not put ${key} on the maintenance line.`);
  if (/a fix needs .* before it can be filed/i.test(detail)) {
    const missing = [
      /acceptance clause/i.test(detail) ? "an acceptance clause" : null,
      /touchpoint/i.test(detail) ? "a file" : null,
    ].filter(Boolean);
    if (missing.length) return `${key} needs ${missing.join(" and ")} before it can go on the line.`;
  }
  return detail;
}

/**
 * "N unscoped" beside the Maintenance chip: fixes with no PRD and no line. It opens a small
 * list, one action per row and no bulk move. Zero is shown; a failed read says so and is never
 * drawn as 0.
 */
export function UnscopedFixes({
  projectId,
  query,
  items,
  lineId,
}: {
  projectId: string;
  query: { isError: boolean; isLoading: boolean; data?: UnscopedFixesData };
  items: Item[];
  lineId: string | null;
}) {
  const [open, setOpen] = React.useState(false);
  const [refused, setRefused] = React.useState<Record<string, string>>({});
  const [moved, setMoved] = React.useState<string[]>([]);
  const [done, setDone] = React.useState<string | null>(null);
  const put = usePutOnLine(projectId);
  const btnRef = React.useRef<HTMLButtonElement>(null);
  // Portaled with fixed placement: the filter row sits in a Collapsible, whose overflow would clip it.
  const [pos, setPos] = React.useState<{ top: number; left: number } | null>(null);
  React.useLayoutEffect(() => {
    if (!open) return;
    const place = () => {
      const r = btnRef.current?.getBoundingClientRect();
      if (r) setPos({ top: r.bottom + 8, left: Math.max(8, Math.min(r.left, window.innerWidth - 448)) });
    };
    place();
    window.addEventListener("resize", place);
    window.addEventListener("scroll", place, true);
    return () => {
      window.removeEventListener("resize", place);
      window.removeEventListener("scroll", place, true);
    };
  }, [open]);

  // Rows leave as soon as the move lands; the refetched list agrees a moment later.
  const ids = (query.data?.ids ?? []).filter((id) => !moved.includes(id));
  const n = query.data ? ids.length : null;
  const byId = new Map(items.map((i) => [i.id, i]));

  React.useEffect(() => {
    if (!open) return;
    const onKey = (e: KeyboardEvent) => e.key === "Escape" && setOpen(false);
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [open]);

  function putOnLine(id: string) {
    setDone(null);
    if (!lineId) {
      setRefused((r) => ({ ...r, [id]: "This project has no maintenance line." }));
      return;
    }
    put.mutate(
      { id, lineId },
      {
        onSuccess: () => {
          setMoved((m) => [...m, id]);
          setRefused((r) => {
            const next = { ...r };
            delete next[id];
            return next;
          });
          setDone(`${id} is on the maintenance line.`);
        },
        onError: (err) => setRefused((r) => ({ ...r, [id]: putRefusalText(id, err) })),
      },
    );
  }

  const label = query.isError ? "unscoped: could not load" : n === null ? "… unscoped" : `${n} unscoped`;
  const amber = !!n;

  return (
    <span className="relative inline-flex">
      <button
        ref={btnRef}
        type="button"
        data-testid="unscoped-count"
        aria-expanded={open}
        aria-label={
          n === null ? label : `${n} unscoped fixes: fixes with no PRD and no line. Opens a list.`
        }
        onClick={() => setOpen((o) => !o)}
        className={cn(
          "h-7 px-1.5 font-mono text-small underline underline-offset-[3px]",
          query.isError
            ? "text-st-blocked decoration-[rgba(255,107,107,0.45)]"
            : amber
              ? "text-[#e0b34a] decoration-[rgba(224,179,74,0.45)]"
              : "text-muted decoration-[#3a444c]",
        )}
      >
        {label}
      </button>

      {open && createPortal(
        <div
          role="dialog"
          aria-label="Unscoped fixes"
          style={{ top: pos?.top ?? 120, left: pos?.left ?? 8 }}
          className="fixed z-50 max-h-[70vh] w-[440px] overflow-y-auto rounded-[11px] border border-[#2f3a42] bg-[#0f1317] text-left shadow-[0_18px_48px_rgba(0,0,0,0.55)]"
        >
          <div className="flex items-center gap-2 border-b border-[#1e242a] px-3 py-2.5">
            <span className="flex-1 text-body font-semibold text-fg">{n === null ? label : `${n} unscoped`}</span>
            <button
              type="button"
              aria-label="Close"
              onClick={() => setOpen(false)}
              className="px-1 py-0.5 text-muted hover:text-fg"
            >
              ✕
            </button>
          </div>
          <div className="px-3 pb-0.5 pt-1.5 text-[11.5px] leading-normal text-muted">
            Fixes with no PRD and no line. They are not in the Maintenance filter.
          </div>
          {query.isError && (
            <div role="alert" className="border-t border-[#1a2025] p-3 text-[12px] text-st-blocked">
              The unscoped fixes could not be loaded.
            </div>
          )}
          {ids.map((id) => (
            <div key={id} data-testid={`unscoped-row-${id}`} className="flex flex-col gap-1.5 border-t border-[#1a2025] px-3 py-[9px]">
              <div className="flex items-center gap-2.5">
                <span className="w-[76px] flex-none font-mono text-small text-muted">{id}</span>
                <span className="min-w-0 flex-1 truncate text-body text-fg-2">{byId.get(id)?.title ?? ""}</span>
                <button
                  type="button"
                  disabled={put.isPending && put.variables?.id === id}
                  onClick={() => putOnLine(id)}
                  className="h-[26px] flex-none rounded-[7px] border border-[#2a333a] bg-[#12171b] px-[9px] text-[11.5px] text-fg-2 hover:border-[#3a444c] disabled:opacity-60"
                >
                  Put on the maintenance line
                </button>
              </div>
              {refused[id] && (
                <span role="alert" className="text-[12px] leading-[1.45] text-[#ff8f8f]">
                  {refused[id]}
                </span>
              )}
            </div>
          ))}
          {done && (
            <div role="status" className="border-t border-[#1a2025] px-3 py-[9px] text-[12px] text-st-done">
              {done}
            </div>
          )}
          {n === 0 && (
            <div className="border-t border-[#1a2025] p-3 text-[12px] text-muted">No unscoped fixes.</div>
          )}
        </div>,
        document.body,
      )}
    </span>
  );
}
