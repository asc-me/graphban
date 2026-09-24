import * as React from "react";

/** Last intentional input modality — keyboard-initiated UI must not animate (PRD-46 §6). */
export type InputModality = "pointer" | "keyboard";

let current: InputModality = "pointer";
const listeners = new Set<(m: InputModality) => void>();

function setModality(next: InputModality) {
  if (next === current) return;
  current = next;
  for (const fn of listeners) fn(current);
}

let installed = false;
function ensureInstalled() {
  if (installed || typeof window === "undefined") return;
  installed = true;
  window.addEventListener("keydown", () => setModality("keyboard"), true);
  window.addEventListener("pointerdown", () => setModality("pointer"), true);
}

export function getInputModality(): InputModality {
  ensureInstalled();
  return current;
}

/** Subscribe to modality flips; returns the current value on each render. */
export function useInputModality(): InputModality {
  ensureInstalled();
  const [modality, set] = React.useState(current);
  React.useEffect(() => {
    listeners.add(set);
    return () => {
      listeners.delete(set);
    };
  }, []);
  return modality;
}
