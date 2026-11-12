/** Replaying the app tour from Help and the command palette (GRPH-1364). */
const START_EVENT = "graphban:start-tour";

/** Replay the tour from anywhere (Help, the command palette). The shell owns the tour. */
export function requestTour() {
  window.dispatchEvent(new Event(START_EVENT));
}

/** Subscribe the shell to {@link requestTour}. */
export function onTourRequested(fn: () => void): () => void {
  window.addEventListener(START_EVENT, fn);
  return () => window.removeEventListener(START_EVENT, fn);
}

const GUIDE_EVENT = "graphban:open-guide";

/** Help's other two guides open the quick-start dialog on a panel (GRPH-1512). */
export type GuideKind = "concepts" | "prompts";

export function requestGuide(kind: GuideKind) {
  window.dispatchEvent(new CustomEvent<GuideKind>(GUIDE_EVENT, { detail: kind }));
}

export function onGuideRequested(fn: (kind: GuideKind) => void): () => void {
  const h = (e: Event) => fn((e as CustomEvent<GuideKind>).detail);
  window.addEventListener(GUIDE_EVENT, h);
  return () => window.removeEventListener(GUIDE_EVENT, h);
}

/** Why Take the tour is unavailable: it points at a project's pages, so it needs one. */
export const TOUR_NEEDS_PROJECT = "Create a project first — the tour points at its pages.";
