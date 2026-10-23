/**
 * Path overlay for canvas mode (PRD-52 D5 path dimming, D6 path trace).
 *
 * The edge batch draws every non-path edge at one alpha. This module draws
 * only the hops, with their types, and owns the request the banner is about.
 * A newer target bumps a sequence number and aborts the previous fetch, so a
 * late response cannot paint over the route the user asked for last.
 */

import * as React from "react";

import { api } from "@/lib/api";
import type { CodePath } from "@/lib/types";

import { EDGE_COLOR } from "./edges";

export const NON_PATH_EDGE_ALPHA = 0.05;
export const ARMED_BANNER = "Pick a target · Esc to cancel";
export const PENDING_BANNER = "Finding path… · Esc to cancel";
export const STILL_WORKING = "still working";
export const NO_PATH_MESSAGE = "No path over the shown edge types";
export const TRY_ALL_LABEL = "Try all edge types";
/** No hard timeout. The banner admits the wait; the request keeps running. */
export const SLOW_AFTER_MS = 10_000;

export interface PathSegment {
  src: string;
  dst: string;
  type: string;
  color: string;
  x0: number;
  y0: number;
  x1: number;
  y1: number;
}

/** World-space hops. O(hops) after one id index — it does not scan the edge list. */
export function pathSegments(
  path: CodePath | null,
  ids: readonly string[],
  x: ArrayLike<number>,
  y: ArrayLike<number>,
): PathSegment[] {
  if (!path?.found || path.hops.length === 0) return [];
  const index = new Map<string, number>();
  for (let i = 0; i < ids.length; i++) index.set(ids[i], i);
  const out: PathSegment[] = [];
  for (const h of path.hops) {
    const ia = index.get(h.src);
    const ib = index.get(h.dst);
    if (ia === undefined || ib === undefined) continue;
    out.push({
      src: h.src,
      dst: h.dst,
      type: h.type,
      color: EDGE_COLOR[h.type] ?? "#8b949e",
      x0: x[ia],
      y0: y[ia],
      x1: x[ib],
      y1: y[ib],
    });
  }
  return out;
}

export function PathBanner({ pending, slow }: { pending: boolean; slow: boolean }) {
  return (
    <div
      data-testid="path-banner"
      role="status"
      className="pointer-events-none absolute left-1/2 top-3 z-20 flex -translate-x-1/2 items-center gap-2 rounded-lg border border-line-2 bg-surface/95 px-3 py-1.5 font-mono text-[12px] text-fg"
    >
      {pending ? (
        <>
          <span
            aria-hidden
            data-testid="path-spinner"
            className="inline-block h-3 w-3 animate-spin rounded-full border border-current border-t-transparent"
          />
          <span>
            {PENDING_BANNER}
            {slow ? ` · ${STILL_WORKING}` : ""}
          </span>
        </>
      ) : (
        <span>{ARMED_BANNER}</span>
      )}
    </div>
  );
}

export interface CanvasPathApi {
  /** Crosshair and target clicks. True while armed, including during the request. */
  picking: boolean;
  pending: boolean;
  slow: boolean;
  /** Hops to draw. Null unless a route with hops was found. */
  path: CodePath | null;
  /** Inspector body, including a not-found result. Null on error and while idle. */
  trace: CodePath | null;
  requestError: boolean;
  toggle: () => void;
  disarm: () => void;
  pick: (id: string) => void;
  /** Alt-click (PRD-20 AC-19): trace from the selection to `id` without arming first. */
  traceTo: (id: string) => void;
  /** One layer. The view clears the selection only when this returns `selection`. */
  escape: () => "pending" | "armed" | "selection";
  retry: () => void;
  tryAll: () => void;
  clearTrace: () => void;
}

function isAbort(err: unknown): boolean {
  return !!err && typeof err === "object" && (err as { name?: string }).name === "AbortError";
}

/**
 * Canvas path mode. The SVG view keeps its own armed flag; this one can abort.
 */
export function useCanvasPath(opts: {
  projectId?: string;
  source: string | null;
  edgeTypes: string[];
  allEdgeTypes: string[];
  onUseAllTypes: () => void;
}): CanvasPathApi {
  const [armed, setArmed] = React.useState(false);
  const [pending, setPending] = React.useState<{ slow: boolean } | null>(null);
  const [trace, setTrace] = React.useState<CodePath | null>(null);
  const [requestError, setRequestError] = React.useState(false);

  const armedRef = React.useRef(false);
  const pendingRef = React.useRef<{ slow: boolean } | null>(null);
  const errorRef = React.useRef(false);
  const seqRef = React.useRef(0);
  const abortRef = React.useRef<AbortController | null>(null);
  const timerRef = React.useRef<ReturnType<typeof setTimeout> | null>(null);
  const targetRef = React.useRef<string | null>(null);
  const sourceRef = React.useRef(opts.source);
  const projectRef = React.useRef(opts.projectId);
  const typesRef = React.useRef(opts.edgeTypes);
  const allTypesRef = React.useRef(opts.allEdgeTypes);
  const onAllRef = React.useRef(opts.onUseAllTypes);
  sourceRef.current = opts.source;
  projectRef.current = opts.projectId;
  typesRef.current = opts.edgeTypes;
  allTypesRef.current = opts.allEdgeTypes;
  onAllRef.current = opts.onUseAllTypes;

  const clearTimer = () => {
    if (timerRef.current !== null) {
      clearTimeout(timerRef.current);
      timerRef.current = null;
    }
  };

  const stopInFlight = () => {
    abortRef.current?.abort();
    abortRef.current = null;
    clearTimer();
    pendingRef.current = null;
    setPending(null);
  };

  React.useEffect(() => {
    return () => {
      seqRef.current += 1;
      abortRef.current?.abort();
      if (timerRef.current !== null) clearTimeout(timerRef.current);
    };
  }, []);

  const prevSource = React.useRef(opts.source);
  React.useEffect(() => {
    if (prevSource.current === opts.source) return;
    prevSource.current = opts.source;
    seqRef.current += 1;
    stopInFlight();
    armedRef.current = false;
    setArmed(false);
    setTrace(null);
    errorRef.current = false;
    setRequestError(false);
    targetRef.current = null;
  }, [opts.source]);

  const run = (target: string, types: string[]) => {
    const source = sourceRef.current;
    if (!source) return;
    stopInFlight();
    const seq = ++seqRef.current;
    const ctrl = new AbortController();
    abortRef.current = ctrl;
    targetRef.current = target;
    armedRef.current = true;
    setArmed(true);
    const waiting = { slow: false };
    pendingRef.current = waiting;
    setPending(waiting);
    setTrace(null);
    errorRef.current = false;
    setRequestError(false);
    timerRef.current = setTimeout(() => {
      if (seqRef.current !== seq) return;
      const slow = { slow: true };
      pendingRef.current = slow;
      setPending(slow);
    }, SLOW_AFTER_MS);
    void api
      .codeAnalysis({
        projectId: projectRef.current,
        edgeTypes: types,
        a: source,
        b: target,
        signal: ctrl.signal,
      })
      .then((data) => {
        if (seqRef.current !== seq) return;
        clearTimer();
        abortRef.current = null;
        pendingRef.current = null;
        setPending(null);
        const path = data.path ?? {
          a: source,
          b: target,
          found: false,
          missing: [],
          hops: [],
        };
        setTrace(path);
        const route = path.found && path.hops.length > 0;
        // A missing endpoint is a coverage gap, not "no path", and it is not armed.
        // No route over the shown types stays armed so Try all and another target work.
        if (route || path.missing.length > 0) {
          armedRef.current = false;
          setArmed(false);
        } else {
          armedRef.current = true;
          setArmed(true);
        }
      })
      .catch((err: unknown) => {
        if (seqRef.current !== seq || isAbort(err)) return;
        clearTimer();
        abortRef.current = null;
        pendingRef.current = null;
        setPending(null);
        setTrace(null);
        errorRef.current = true;
        setRequestError(true);
        armedRef.current = true;
        setArmed(true);
      });
  };

  const disarm = () => {
    seqRef.current += 1;
    stopInFlight();
    armedRef.current = false;
    setArmed(false);
    setTrace(null);
    errorRef.current = false;
    setRequestError(false);
  };

  const toggle = () => {
    if (!sourceRef.current) return;
    if (armedRef.current || pendingRef.current) {
      disarm();
      return;
    }
    armedRef.current = true;
    setArmed(true);
    errorRef.current = false;
    setRequestError(false);
  };

  const pick = (id: string) => {
    if (!armedRef.current && !pendingRef.current) return;
    run(id, typesRef.current);
  };

  const traceTo = (id: string) => {
    if (!sourceRef.current || sourceRef.current === id) return;
    run(id, typesRef.current);
  };

  const escape = (): "pending" | "armed" | "selection" => {
    if (pendingRef.current) {
      seqRef.current += 1;
      stopInFlight();
      armedRef.current = true;
      setArmed(true);
      return "pending";
    }
    if (armedRef.current || errorRef.current) {
      seqRef.current += 1;
      stopInFlight();
      armedRef.current = false;
      setArmed(false);
      setTrace(null);
      errorRef.current = false;
      setRequestError(false);
      return "armed";
    }
    return "selection";
  };

  const retry = () => {
    const target = targetRef.current;
    if (!target) return;
    run(target, typesRef.current);
  };

  const tryAll = () => {
    const target = targetRef.current;
    onAllRef.current();
    if (!target) return;
    run(target, allTypesRef.current);
  };

  const clearTrace = () => {
    setTrace(null);
    errorRef.current = false;
    setRequestError(false);
    armedRef.current = false;
    setArmed(false);
  };

  const route = trace && trace.found && trace.hops.length > 0 ? trace : null;

  return {
    picking: armed,
    pending: pending !== null,
    slow: pending?.slow ?? false,
    path: route,
    trace,
    requestError,
    toggle,
    disarm,
    pick,
    traceTo,
    escape,
    retry,
    tryAll,
    clearTrace,
  };
}
