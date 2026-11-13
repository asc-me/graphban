import * as React from "react";
import { createPortal } from "react-dom";
import { useLocation, useNavigate } from "react-router-dom";

import { useProjectCtx } from "@/features/ProjectContext";
import { openDocsReader } from "@/features/docs/DocsReader";
import { useCounts } from "@/lib/queries";

import { OrgPlug } from "./OrgPlug";
import { TOUR_STOPS, type Spot, type TourStop, anchorHref, resolveStops, spotFor, visibleAnchor } from "./stops";

const EASE = "cubic-bezier(.2,.8,.2,1)";
const POP_W = 320;
/** The closing org card is wider (design: 400). */
const PLUG_W = 400;
/** How long a stop waits for its anchor to show after navigating before it is skipped. */
const ANCHOR_WAIT_MS = 1200;

function prefersReducedMotion(): boolean {
  return typeof window !== "undefined" && typeof window.matchMedia === "function"
    && window.matchMedia("(prefers-reduced-motion: reduce)").matches;
}

/**
 * The coach-mark tour over the real nav and views (GRPH-P67 §5, GRPH-1363; design
 * `docs/design/prd-67/Tour Stop.dc.html`). A spotlight ring on the stop's `data-tour` anchor
 * and a popover — an ARIA dialog — with Back, Next, `n / N`, Esc to exit and Read more.
 *
 * Stops are resolved against the rendered nav when the tour opens: one whose anchor is not
 * there is skipped and the count drops with it ("7 / 7" without Fleet). A stop that resolved
 * but never shows after navigating is dropped the same way, so the ring never points at
 * nothing. Stops navigate by following the anchor's own link.
 */
export function AppTour({
  open,
  onExit,
  onComplete,
  stops: allStops = TOUR_STOPS,
}: {
  open: boolean;
  /** Esc, or Read more: the tour closes and nothing is recorded. */
  onExit: () => void;
  /** Finish on the last stop, with the ids this nav skipped. */
  onComplete: (skipped: string[]) => void;
  stops?: TourStop[];
}) {
  if (!open) return null;
  return <TourRun allStops={allStops} onExit={onExit} onComplete={onComplete} />;
}

function TourRun({
  allStops,
  onExit,
  onComplete,
}: {
  allStops: TourStop[];
  onExit: () => void;
  onComplete: (skipped: string[]) => void;
}) {
  const navigate = useNavigate();
  const { pathname, search } = useLocation();
  const { activeId } = useProjectCtx();
  const countsQ = useCounts(activeId);
  const counts = countsQ.isError ? undefined : countsQ.data;

  // Resolved once, against the nav as it is committed when the tour opens.
  const [plan, setPlan] = React.useState<{ stops: TourStop[]; skipped: string[] } | null>(null);
  React.useLayoutEffect(() => {
    const { resolved, skipped } = resolveStops(document, allStops);
    setPlan({ stops: resolved, skipped });
  }, [allStops]);
  const stops = plan?.stops ?? [];
  const skipped = plan?.skipped ?? [];
  const [i, setI] = React.useState(0);
  const dir = React.useRef<1 | -1>(1);
  const [spot, setSpot] = React.useState<Spot | null>(null);
  const [popH, setPopH] = React.useState(0);
  const popRef = React.useRef<HTMLDivElement>(null);
  const nextRef = React.useRef<HTMLButtonElement>(null);
  const returnFocus = React.useRef<Element | null>(null);
  const reduce = prefersReducedMotion();
  const stop = stops[i] as TourStop | undefined;
  const last = i === stops.length - 1;
  // The org plug is the end of the tour, not a numbered stop (design: "7 STOPS + ORG PLUG").
  const numbered = stops.filter((s) => !s.plug).length;
  const popW = stop?.plug ? PLUG_W : POP_W;

  // Back where the keyboard was when the tour ends.
  React.useEffect(() => {
    returnFocus.current = document.activeElement;
    return () => {
      const el = returnFocus.current;
      if (el instanceof HTMLElement && el.isConnected) el.focus({ preventScroll: true });
    };
  }, []);

  // A nav with no stop at all has nothing to tour.
  React.useEffect(() => {
    if (plan && plan.stops.length === 0) onExit();
  }, [plan, onExit]);

  // Follow the anchor's link to the stop's view; a stop without one (the Agent toggle) stays.
  React.useEffect(() => {
    if (!stop || stop.plug) return;
    const href = anchorHref(stop.id);
    if (href && href !== pathname + search && href !== pathname) navigate(href);
    // Navigation is per stop, not per location change.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [stop?.id]);

  const measure = React.useCallback(() => {
    if (!stop) return;
    const el = visibleAnchor(stop.id);
    if (!el) return setSpot(null);
    const next = spotFor(el);
    setSpot((o) =>
      o && o.top === next.top && o.left === next.left && o.width === next.width && o.height === next.height ? o : next,
    );
    const h = popRef.current?.offsetHeight ?? 0;
    if (h) setPopH((p) => (Math.abs(p - h) > 1 ? h : p));
  }, [stop]);

  // Re-measure on mount and each stop, after layout settles (the accordion folds out over
  // 240ms), once fonts are ready, on resize, and after any transition ends.
  React.useLayoutEffect(() => {
    if (!stop) return;
    measure();
    const el = visibleAnchor(stop.id);
    if (el && typeof el.scrollIntoView === "function") el.scrollIntoView({ block: "nearest" });
    let raf2 = 0;
    const raf1 = requestAnimationFrame(() => {
      measure();
      raf2 = requestAnimationFrame(measure);
    });
    const timers = [150, 300].map((ms) => window.setTimeout(measure, ms));
    let live = true;
    document.fonts?.ready.then(() => live && measure());
    window.addEventListener("resize", measure);
    document.addEventListener("transitionend", measure, true);
    return () => {
      live = false;
      cancelAnimationFrame(raf1);
      cancelAnimationFrame(raf2);
      timers.forEach((t) => window.clearTimeout(t));
      window.removeEventListener("resize", measure);
      document.removeEventListener("transitionend", measure, true);
    };
  }, [stop, pathname, measure]);

  // A stop whose anchor never shows is skipped, in the direction of travel.
  React.useEffect(() => {
    if (!stop || spot) return;
    const t = window.setTimeout(() => {
      if (visibleAnchor(stop.id)) return measure();
      const rest = stops.filter((s) => s.id !== stop.id);
      setPlan({ stops: rest, skipped: [...skipped, stop.id] });
      if (dir.current === -1) setI(Math.max(0, i - 1));
      else if (i >= rest.length) onComplete([...skipped, stop.id]);
    }, ANCHOR_WAIT_MS);
    return () => window.clearTimeout(t);
  }, [stop, spot, stops, skipped, i, measure, onComplete]);

  // The keyboard lands on Next at every stop.
  React.useEffect(() => {
    if (spot) {
      (nextRef.current ?? popRef.current?.querySelector<HTMLElement>("button:not([disabled])"))?.focus({
        preventScroll: true,
      });
    }
    // Only when the stop changes or first shows, not on every re-measure.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [stop?.id, !!spot]);

  // The tour is modal, so focus never rests behind it. Without this, a click on the page or a
  // re-render that drops the focused button leaves it on the page, and Enter then activates
  // whatever is there (the last stop's Activity row, GRPH-1552) instead of the tour card.
  React.useEffect(() => {
    if (!spot) return;
    function onFocusIn(e: FocusEvent) {
      const pop = popRef.current;
      if (!pop || (e.target instanceof Node && pop.contains(e.target))) return;
      (nextRef.current ?? pop.querySelector<HTMLElement>("button:not([disabled])"))?.focus({ preventScroll: true });
    }
    document.addEventListener("focusin", onFocusIn);
    return () => document.removeEventListener("focusin", onFocusIn);
  }, [!!spot]);

  // Esc exits. Captured first so the sidebar flyout and the docs reader do not also act on it.
  React.useEffect(() => {
    function onKey(e: KeyboardEvent) {
      if (e.key !== "Escape") return;
      e.preventDefault();
      e.stopPropagation();
      onExit();
    }
    window.addEventListener("keydown", onKey, true);
    return () => window.removeEventListener("keydown", onKey, true);
  }, [onExit]);

  if (!stop) return null;

  const go = (to: number) => {
    dir.current = to < i ? -1 : 1;
    setSpot(null);
    setI(to);
  };
  const next = () => (last ? onComplete(skipped) : go(i + 1));
  const readMore = () => {
    onExit();
    openDocsReader();
  };

  // Tab stays inside the popover (it is modal: the page behind it does not take clicks).
  const trapTab = (e: React.KeyboardEvent) => {
    if (e.key !== "Tab" || !popRef.current) return;
    const f = Array.from(
      popRef.current.querySelectorAll<HTMLElement>("button:not([disabled]), a[href], [tabindex='0']"),
    );
    if (!f.length) return;
    const first = f[0], end = f[f.length - 1];
    if (e.shiftKey && document.activeElement === first) {
      e.preventDefault();
      end.focus();
    } else if (!e.shiftKey && document.activeElement === end) {
      e.preventDefault();
      first.focus();
    }
  };

  const vw = window.innerWidth || 1440;
  const vh = window.innerHeight || 900;
  const sp = spot ?? { top: 0, left: 0, width: 0, height: 0 };
  const ph = popH || 220;
  const below = sp.left + sp.width + 14 + popW > vw - 12;
  let pop: { top: number; left: number };
  let arrow: { top: number; left: number; rot: string };
  if (below) {
    const left = Math.max(12, sp.left + sp.width - popW);
    pop = { top: sp.top + sp.height + 12, left };
    arrow = { top: -7, left: Math.min(popW - 24, sp.left + sp.width / 2 - left - 6), rot: "rotate(135deg)" };
  } else {
    const top = Math.max(12, Math.min(sp.top + sp.height / 2 - 48, vh - ph - 12));
    pop = { top, left: sp.left + sp.width + 14 };
    arrow = { top: Math.max(12, sp.top + sp.height / 2 - top - 6), left: -7, rot: "rotate(45deg)" };
  }
  const moveT = reduce ? "none" : `top 280ms ${EASE}, left 280ms ${EASE}, width 280ms ${EASE}, height 280ms ${EASE}`;
  const popT = reduce ? "none" : `top 280ms ${EASE}, left 280ms ${EASE}`;
  const emptyCount = stop.empty && counts ? counts[stop.empty.count] : undefined;
  const isEmpty = typeof emptyCount === "number" && emptyCount === 0;
  const pos = stop.plug ? "END OF TOUR" : `${i + 1} / ${numbered}`;

  return createPortal(
    <div data-tour-root="">
      {/* The page behind the tour does not take clicks while it runs. */}
      <div className="fixed inset-0 z-[70]" aria-hidden="true" />
      {spot && (
        <div
          aria-hidden="true"
          data-tour-spot={stop.id}
          className="pointer-events-none fixed z-[71] rounded-[12px]"
          style={{
            top: sp.top,
            left: sp.left,
            width: sp.width,
            height: sp.height,
            boxShadow: "0 0 0 2px rgba(198,242,78,.55),0 0 0 9999px rgba(10,11,13,.6)",
            transition: moveT,
          }}
        />
      )}
      <div
        ref={popRef}
        role="dialog"
        aria-modal="true"
        aria-labelledby="tour-title"
        aria-describedby="tour-body"
        onKeyDown={trapTab}
        hidden={!spot}
        className="fixed z-[72] flex flex-col gap-2.5 rounded-[12px] border border-line bg-surface-3 px-4 pb-3 pt-3.5 shadow-[0_12px_32px_rgba(0,0,0,.45),0_2px_6px_rgba(0,0,0,.3)]"
        style={{ top: pop.top, left: pop.left, width: popW, transition: popT }}
      >
        <span
          aria-hidden="true"
          className="absolute h-3 w-3 border-b border-l border-line bg-surface-3"
          style={{ top: arrow.top, left: arrow.left, transform: arrow.rot }}
        />
        <span className="font-mono text-[10px] tracking-[.7px] text-faint" data-tour-pos>
          {!stop.plug && <span className="sr-only">Stop </span>}
          {pos}
        </span>
        <h2 id="tour-title" className="text-[15px] font-semibold">
          {stop.title}
        </h2>
        {stop.plug ? (
          <div id="tour-body">
            <OrgPlug
              onNotNow={() => onComplete(skipped)}
              onNavigate={(to) => {
                onComplete(skipped);
                navigate(to);
              }}
            />
          </div>
        ) : (
          <div id="tour-body" className="flex flex-col gap-1.5">
            <p className="text-[13px] leading-normal text-fg-2 [text-wrap:pretty]">{stop.body}</p>
            {isEmpty && <p className="text-body text-muted" data-tour-empty>{stop.empty!.text}</p>}
          </div>
        )}
        {!stop.plug && (
        <button
          type="button"
          onClick={readMore}
          className="self-start rounded text-body text-faint outline-none hover:text-fg-2 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-focus"
        >
          Read more →
        </button>
        )}
        <div className="mt-0.5 flex items-center gap-2">
          <span className="font-mono text-[10.5px] text-faint">Esc to exit</span>
          <span className="flex-1" />
          {stop.plug ? null : (<>
          <button
            type="button"
            onClick={() => go(i - 1)}
            disabled={i === 0}
            className="h-[30px] rounded-lg border border-control bg-transparent px-[11px] text-body text-fg-2 outline-none hover:border-control-hover focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-focus disabled:text-faint disabled:hover:border-control"
          >
            Back
          </button>
          <button
            ref={nextRef}
            type="button"
            onClick={next}
            className="h-[30px] rounded-lg border border-accent bg-accent px-[13px] text-body font-semibold text-bg outline-none hover:border-accent-hi hover:bg-accent-hi focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-focus"
          >
            {last ? "Finish" : "Next"}
          </button>
          </>)}
        </div>
      </div>
      {/* Each stop is announced, not only the first: focus stays inside one dialog. */}
      <div aria-live="polite" className="sr-only">
        {spot ? `Stop ${pos}: ${stop.title}` : ""}
      </div>
    </div>,
    document.body,
  );
}
