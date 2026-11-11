/**
 * The app tour's stops and how each finds its anchor (GRPH-P67 §5, GRPH-1363).
 *
 * An anchor is a `data-tour="…"` attribute on the real nav or chrome, never a CSS selector or
 * label text: the nav rows carry their view segment (`tracker`, `prds`, …), Dashboard carries
 * `dashboard`, and the top bar's Agent toggle carries `agent`. A stop whose anchor is not in
 * the document is skipped, so the tour never points at nothing.
 */
import type { ShellCounts } from "@/lib/types";

export type TourStop = {
  /** The `data-tour` value this stop anchors on, and the id recorded when it is skipped. */
  id: string;
  title: string;
  /** At most two sentences — the copy in `Tour Stop.dc.html`. */
  body: string;
  /**
   * Shown under the body when the view is empty, so the tour never fakes data (G10). Only
   * when the count read succeeded and said 0: an absent count is not an empty view.
   */
  empty?: { count: keyof ShellCounts; text: string };
  /**
   * The closing org plug (GRPH-1364): not a numbered stop — it reads "END OF TOUR", stays out
   * of the `n / N` count, never navigates (hosted Organization is a console link), and its
   * popover holds the plug's actions instead of Back / Next.
   */
  plug?: true;
};

export const TOUR_STOPS: TourStop[] = [
  {
    id: "dashboard",
    title: "Dashboard",
    body: "Setup progress and the first-job counters stay here until each one is proven.",
  },
  {
    id: "tracker",
    title: "Tracker",
    body: "Six states, as a board or a list. Agents claim ready work, and the files an item names — its touchpoints — keep parallel agents from colliding.",
    empty: { count: "items", text: "Your agent's first items will appear here." },
  },
  {
    id: "prds",
    title: "Specs",
    body: "Versioned specs. Draft, grill, approve, then decompose into tracked items.",
  },
  {
    id: "agent",
    title: "Agent sidebar",
    body: "Ask about the page you're on. It calls the same tools your agent does.",
  },
  {
    id: "memory",
    title: "Memory review",
    body: "Candidates your agent proposed. Nothing is published until you publish it.",
    empty: { count: "review", text: "Nothing is waiting yet. Your agent's first candidates will appear here." },
  },
  {
    id: "code",
    title: "Code graph",
    body: "Modules, files and symbols from your checkout. Agents read it before they touch code.",
    empty: { count: "code_nodes", text: "Nothing indexed yet. Your agent's first code-graph pass fills it in." },
  },
  {
    id: "fleet",
    title: "Fleet",
    body: "Who works here and what they spawn as.",
  },
  {
    id: "activity",
    title: "Activity",
    body: "Every accepted change, attributed to a person or an agent.",
  },
  {
    id: "org",
    title: "Organization",
    body: "Working with a team? Link this deployment to an org. Not now is always an equal choice.",
    plug: true,
  },
];

const anchorsFor = (root: ParentNode, id: string) =>
  Array.from(root.querySelectorAll<HTMLElement>(`[data-tour="${id}"]`));

/**
 * Hidden copies do not count as the place to point: the sidebar keeps its rail and its
 * expanded layer both mounted, and a folded-up accordion section stays in the DOM, inert.
 */
function isShown(el: HTMLElement): boolean {
  return !el.closest("[inert], [aria-hidden='true']");
}

/** The element a stop's ring should wrap right now, or null while none is shown. */
export function visibleAnchor(id: string, root: ParentNode = document): HTMLElement | null {
  return anchorsFor(root, id).find(isShown) ?? null;
}

/**
 * Which stops this nav can play and which it skips. A stop resolves when its anchor is
 * rendered at all — shown, or in a folded section the stop's navigation will open; a stop
 * with no anchor anywhere (a view hidden by role, hosted versus self-host) is skipped.
 */
export function resolveStops(root: ParentNode = document, stops: TourStop[] = TOUR_STOPS) {
  const resolved: TourStop[] = [];
  const skipped: string[] = [];
  for (const s of stops) {
    if (anchorsFor(root, s.id).length) resolved.push(s);
    else skipped.push(s.id);
  }
  return { resolved, skipped };
}

/** The route a stop navigates to: the anchor's own link, so no route table is copied here. */
export function anchorHref(id: string, root: ParentNode = document): string | null {
  for (const el of anchorsFor(root, id)) {
    const a = el.closest("a") ?? el.querySelector("a");
    const href = a?.getAttribute("href");
    if (href) return href;
  }
  return null;
}

/** Ring padding from the design: 4px above and below, 8px either side. */
export const SPOT_PAD = { y: 4, x: 8 } as const;

export type Spot = { top: number; left: number; width: number; height: number };

export function spotFor(el: HTMLElement): Spot {
  const r = el.getBoundingClientRect();
  return {
    top: Math.round(r.top - SPOT_PAD.y),
    left: Math.round(r.left - SPOT_PAD.x),
    width: Math.round(r.width + SPOT_PAD.x * 2),
    height: Math.round(r.height + SPOT_PAD.y * 2),
  };
}
