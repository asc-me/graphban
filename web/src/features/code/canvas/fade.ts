/**
 * Highlight fades (Build.dc.html: `transition: opacity .25s` on nodes, edges and hulls).
 *
 * The alpha buffer is still written once per focus, Isolate or path change. This only eases
 * the displayed copy toward it, so a hover does not snap the whole map from lit to dim.
 */

import * as React from "react";

import { easeOutCubic, prefersReducedMotion } from "./camera";

export const FADE_MS = 220;

/** `from` eased toward `to` by `t` in [0, 1]. Both buffers are the same length or `to` wins. */
export function blendAlpha(from: Uint8Array | null, to: Uint8Array, t: number): Uint8Array {
  if (!from || from.length !== to.length || t >= 1) return to;
  const out = new Uint8Array(to.length);
  for (let i = 0; i < to.length; i++) out[i] = Math.round(from[i] + (to[i] - from[i]) * t);
  return out;
}

/**
 * The alpha buffer to draw this frame, and how far in a new focus is (`mix`, 0→1).
 * A new focus restarts `mix`; a changed buffer eases from whatever is on screen now.
 */
export function useAlphaFade(
  target: Uint8Array | null,
  focus: string | null,
): { alpha: Uint8Array | null; mix: number } {
  const [frame, setFrame] = React.useState<{ alpha: Uint8Array | null; mix: number }>({
    alpha: target,
    mix: 1,
  });
  const shown = React.useRef<Uint8Array | null>(target);
  const lastFocus = React.useRef<string | null>(focus);

  React.useEffect(() => {
    if (!target) {
      shown.current = null;
      setFrame({ alpha: null, mix: 1 });
      return;
    }
    const newFocus = focus !== null && focus !== lastFocus.current;
    lastFocus.current = focus;
    const from = shown.current;
    if (prefersReducedMotion() || !from || from.length !== target.length) {
      shown.current = target;
      setFrame({ alpha: target, mix: 1 });
      return;
    }
    let raf = 0;
    const start = performance.now();
    const step = (now: number) => {
      const t = easeOutCubic((now - start) / FADE_MS);
      const alpha = blendAlpha(from, target, t);
      shown.current = alpha;
      setFrame({ alpha, mix: newFocus ? t : 1 });
      if (t < 1) raf = requestAnimationFrame(step);
    };
    raf = requestAnimationFrame(step);
    return () => cancelAnimationFrame(raf);
  }, [target, focus]);

  // Before the first eased frame lands, draw the target rather than nothing.
  return frame.alpha ? frame : { alpha: target, mix: 1 };
}
