import * as React from "react";

import { computeLayout, layoutKey, type LayoutEdge, type LayoutOpts, type Pos } from "./layout";
import type { LayoutResponse } from "./layout.worker";

export interface GraphLayout {
  /** Node positions. While a new run is in flight these are the PREVIOUS ones, never empty. */
  pos: Record<string, Pos>;
  /** True while a run is outstanding — for a subtle busy hint, not for blanking the graph. */
  pending: boolean;
  /**
   * Force a run against a caller-supplied edge set, ignoring the memo.
   *
   * This is the "re-layout to visible" escape hatch: under a deep filter the positions reflect
   * edges the user can no longer see, and this is how they ask for a map of what is left. It is
   * never called automatically — automatic re-layout is precisely the jank D1 removes.
   */
  relayout: (edges: LayoutEdge[]) => void;
}

/**
 * Run the shared layout off the main thread, one worker instance per view (PRD-20 D1).
 *
 * **One instance per hook, not one shared module-level worker.** The Code and Links views are
 * separate routes and never co-render, so a shared instance would buy nothing and cost request
 * tagging plus a race between two clients of one worker. The instance is terminated on unmount.
 *
 * **Previous positions survive a run.** The hook never returns `{}` once it has a result, so
 * the graph does not blank while re-laying out.
 *
 * **Synchronous fallback.** Where `Worker` is unavailable — jsdom under vitest, or any
 * environment that blocks module workers — this computes inline rather than rendering nothing.
 * A graph that silently fails to appear is worse than one that briefly janks.
 *
 * `edges` should be the caller's **unfiltered** set: the memo key is derived from it, so
 * toggling an edge-type chip redraws without moving a node.
 */
export function useGraphLayout(
  ids: string[],
  edges: LayoutEdge[],
  opts: LayoutOpts,
): GraphLayout {
  // `opts.pinned` is read through `optsRef` at run time and is deliberately NOT part of the
  // memo key: dragging a node moves that node and nothing else, because reflowing the whole
  // graph on every drag is the churn D1 just removed. Pins are honoured on the NEXT run — a
  // data change or an explicit re-layout — which is where "stop moving it" actually bites.
  const [pos, setPos] = React.useState<Record<string, Pos>>({});
  const [pending, setPending] = React.useState(false);

  const workerRef = React.useRef<Worker | null>(null);
  const reqRef = React.useRef(0);
  // Latest opts without making them a dependency — width/height are literals at every call
  // site, and threading them through the memo would re-run layout on an unrelated re-render.
  const optsRef = React.useRef(opts);
  optsRef.current = opts;

  React.useEffect(() => {
    if (typeof Worker === "undefined") return;
    let worker: Worker;
    try {
      worker = new Worker(new URL("./layout.worker.ts", import.meta.url), { type: "module" });
    } catch {
      return; // Fall through to the synchronous path below.
    }
    workerRef.current = worker;
    worker.onmessage = (ev: MessageEvent<LayoutResponse>) => {
      // Drop a reply that a newer request has already superseded.
      if (ev.data.id !== reqRef.current) return;
      setPos(ev.data.pos);
      setPending(false);
    };
    return () => {
      worker.terminate();
      workerRef.current = null;
    };
  }, []);

  const run = React.useCallback((runIds: string[], runEdges: LayoutEdge[]) => {
    const id = ++reqRef.current;
    const worker = workerRef.current;
    if (!worker) {
      setPos(computeLayout(runIds, runEdges, optsRef.current));
      setPending(false);
      return;
    }
    setPending(true);
    worker.postMessage({ id, ids: runIds, edges: runEdges, opts: optsRef.current });
  }, []);

  // The memo: re-run only when the node set or the UNFILTERED edge set actually changes.
  const key = React.useMemo(() => layoutKey(ids, edges), [ids, edges]);
  const idsRef = React.useRef(ids);
  idsRef.current = ids;
  const edgesRef = React.useRef(edges);
  edgesRef.current = edges;

  React.useEffect(() => {
    if (idsRef.current.length === 0) {
      setPos({});
      setPending(false);
      return;
    }
    run(idsRef.current, edgesRef.current);
  }, [key, run]);

  const relayout = React.useCallback(
    (filtered: LayoutEdge[]) => run(idsRef.current, filtered),
    [run],
  );

  return { pos, pending, relayout };
}
