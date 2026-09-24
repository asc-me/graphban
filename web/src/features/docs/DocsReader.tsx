import { BookMarked, ExternalLink, Info, ThumbsDown, ThumbsUp, X } from "lucide-react";
import * as React from "react";
import { useLocation, useNavigate } from "react-router-dom";

import { cn } from "@/lib/cn";
import { useInputModality } from "@/lib/input-modality";

import { GLOBAL_SHORTCUTS, docFor } from "./content";

/** Inline, context-aware docs reader: a floating trigger + right slide-over whose
 *  content follows the page you're on. Mounted once inside the app shell. */
export function DocsReader({ agentOpen = false }: { agentOpen?: boolean }) {
  const [open, setOpen] = React.useState(false);
  const [rendered, setRendered] = React.useState(false);
  const [thanks, setThanks] = React.useState(false);
  const { pathname } = useLocation();
  const plannerRoute = /\/(home|tracker|prds|memory-review|requests)(\/|$)/.test(pathname);
  const navigate = useNavigate();
  const doc = docFor(pathname);
  const liveModality = useInputModality();
  const [motion, setMotion] = React.useState(liveModality);
  const exitTimer = React.useRef<number | null>(null);

  // Reset the "was this helpful" state whenever the page or open-state changes.
  React.useEffect(() => setThanks(false), [pathname, open]);

  React.useEffect(() => {
    return () => {
      if (exitTimer.current != null) window.clearTimeout(exitTimer.current);
    };
  }, []);

  function openDocs(nextModality = liveModality) {
    if (exitTimer.current != null) {
      window.clearTimeout(exitTimer.current);
      exitTimer.current = null;
    }
    setMotion(nextModality);
    setRendered(true);
    // Double-rAF so data-state=closed paints before open (enter path).
    requestAnimationFrame(() => {
      requestAnimationFrame(() => setOpen(true));
    });
  }

  function closeDocs(nextModality = liveModality) {
    setMotion(nextModality);
    setOpen(false);
    const ms = nextModality === "keyboard" ? 0 : 240;
    if (exitTimer.current != null) window.clearTimeout(exitTimer.current);
    exitTimer.current = window.setTimeout(() => {
      setRendered(false);
      exitTimer.current = null;
    }, ms);
  }

  // Global keyboard: "?" toggles, "Esc" closes — ignored while typing. No animation (PRD-46 §6).
  React.useEffect(() => {
    function onKey(e: KeyboardEvent) {
      const el = e.target as HTMLElement | null;
      const typing = el && (el.tagName === "INPUT" || el.tagName === "TEXTAREA" || el.isContentEditable);
      if (e.key === "Escape") {
        if (open || rendered) closeDocs("keyboard");
        return;
      }
      if (e.key === "?" && !typing) {
        e.preventDefault();
        if (open) closeDocs("keyboard");
        else openDocs("keyboard");
      }
    }
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [open, rendered, liveModality]);

  function goRelated(to: string) {
    closeDocs(liveModality);
    navigate(to);
  }

  return (
    <>
      {/* Floating trigger — hover lift only on fine pointers. Hidden while the agent rail
          is open so the last shard card stays readable (GRPH-923). */}
      {!agentOpen && (
      <button
        onClick={() => openDocs("pointer")}
        title="Docs for this page"
        aria-label="Open docs for this page"
        className={cn(
          "gb-pressable fixed right-6 z-[55] flex h-[50px] w-[50px] items-center justify-center rounded-full [@media(hover:hover)_and_(pointer:fine)]:transition-transform [@media(hover:hover)_and_(pointer:fine)]:hover:-translate-y-0.5",
          plannerRoute ? "bottom-24" : "bottom-6",
        )}
        style={{
          background: "linear-gradient(150deg,#c6f24e,#8fd12e)",
          border: "1px solid rgba(198,242,78,.4)",
          boxShadow: "0 8px 24px rgba(198,242,78,.22)",
        }}
      >
        <Info size={22} className="text-bg" />
      </button>
      )}

      {rendered && (
        <>
          <div
            onClick={() => closeDocs("pointer")}
            className="gb-overlay fixed inset-0 z-[60] bg-[rgba(4,6,8,0.5)] backdrop-blur-[2px]"
            data-state={open ? "open" : "closed"}
            data-motion={motion === "keyboard" ? "instant" : "pointer"}
          />
          <aside
            className="gb-sheet fixed inset-y-0 right-0 z-[61] flex w-[400px] max-w-[92vw] flex-col border-l border-[#1e242a] bg-[#0d1114] shadow-[-28px_0_70px_rgba(0,0,0,0.55)]"
            data-state={open ? "open" : "closed"}
            data-motion={motion === "keyboard" ? "instant" : "pointer"}
            role="dialog"
            aria-label="Docs"
          >
            {/* Header */}
            <div className="flex flex-none items-center gap-[11px] border-b border-line px-4 pb-3.5 pt-4">
              <span
                className="flex h-7 w-7 flex-none items-center justify-center rounded-[8px]"
                style={{ background: "linear-gradient(150deg,#c6f24e,#8fd12e)" }}
              >
                <BookMarked size={15} className="text-bg" />
              </span>
              <div className="min-w-0 flex-1">
                <div className="flex items-center gap-2">
                  <span className="text-[13.5px] font-semibold">Docs</span>
                  <span className="font-mono text-[9px] tracking-[0.6px] text-faint-2">{doc.badge}</span>
                </div>
                <div className="text-[11px] text-muted">Help for the page you’re on</div>
              </div>
              <button
                onClick={() => closeDocs("pointer")}
                className="flex h-[30px] w-[30px] flex-none items-center justify-center rounded-lg border border-line-2 bg-surface text-muted transition-colors hover:border-line-hover hover:text-fg"
                aria-label="Close"
              >
                <X size={13} />
              </button>
            </div>

            {/* Body */}
            <div className="min-h-0 flex-1 overflow-y-auto p-4">
              <h2 className="text-[16px] font-semibold tracking-tight">{doc.title}</h2>
              <p className="mb-4 mt-0.5 text-[12.5px] text-muted">{doc.tagline}</p>

              {doc.sections.map((s) => (
                <div key={s.num} className="mb-4 flex gap-3">
                  <span className="flex h-[18px] w-[18px] flex-none items-center justify-center rounded-full border border-line-2 font-mono text-[10px] text-accent">
                    {s.num}
                  </span>
                  <div className="min-w-0">
                    <div className="text-[13px] font-semibold text-fg">{s.h}</div>
                    <div className="mt-0.5 text-[12.5px] leading-relaxed text-muted">{s.b}</div>
                  </div>
                </div>
              ))}

              <div className="mb-5">
                <div className="mb-2.5 font-mono text-[9px] tracking-[0.6px] text-faint-2">SHORTCUTS</div>
                <div className="space-y-1.5">
                  {[...GLOBAL_SHORTCUTS, ...(doc.shortcuts ?? [])].map((sc) => (
                    <div key={sc.k} className="flex items-center gap-3 text-[12.5px]">
                      <span className="min-w-[26px] rounded-[5px] border border-line-2 px-1.5 py-0.5 text-center font-mono text-[10px] text-muted-2">
                        {sc.k}
                      </span>
                      <span className="text-muted">{sc.d}</span>
                    </div>
                  ))}
                </div>
              </div>

              {doc.related && doc.related.length > 0 && (
                <div>
                  <div className="mb-2.5 font-mono text-[9px] tracking-[0.6px] text-faint-2">RELATED</div>
                  <div className="flex flex-col gap-0.5">
                    {doc.related.map((r) => (
                      <button
                        key={r.to}
                        onClick={() => goRelated(r.to)}
                        className="flex w-full items-center gap-2.5 rounded-lg px-2 py-2 text-left text-[12.5px] text-fg-2 transition-colors hover:bg-surface-3"
                      >
                        <ExternalLink size={13} className="flex-none text-faint" />
                        {r.label}
                      </button>
                    ))}
                  </div>
                </div>
              )}
            </div>

            {/* Footer */}
            <div className="flex flex-none items-center gap-2 border-t border-line px-4 py-3">
              <span className="text-[12px] text-muted">
                {thanks ? "Thanks for the feedback!" : "Was this helpful?"}
              </span>
              {!thanks && (
                <>
                  <div className="flex-1" />
                  <button
                    onClick={() => setThanks(true)}
                    aria-label="Helpful"
                    className="flex h-7 w-[30px] items-center justify-center rounded-[7px] border border-line-2 bg-surface text-muted transition-colors hover:border-[#2a3d20] hover:text-accent"
                  >
                    <ThumbsUp size={14} />
                  </button>
                  <button
                    onClick={() => setThanks(true)}
                    aria-label="Not helpful"
                    className="flex h-7 w-[30px] items-center justify-center rounded-[7px] border border-line-2 bg-surface text-muted transition-colors hover:border-[#3a2626] hover:text-st-blocked"
                  >
                    <ThumbsDown size={14} />
                  </button>
                </>
              )}
            </div>
          </aside>
        </>
      )}
    </>
  );
}
