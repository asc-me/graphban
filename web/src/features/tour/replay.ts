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
