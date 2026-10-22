/**
 * Area layouts on the existing layout worker (PRD-52 D4).
 *
 * A new message kind, `area`. `useGraphLayout` is not this hook and is not
 * modified: GalaxyView still owns that contract.
 *
 * Where Worker is missing, the layout runs inline. If the worker dies, it is
 * respawned once. A second death pins inline mode for the session.
 */

import * as React from "react";

import { clustersLayout, type ClusterInput, type ClusterLayout } from "./bigLayout";

export interface AreaWorker {
  postMessage(msg: unknown): void;
  terminate(): void;
  onmessage: ((ev: { data: AreaLayoutResponse }) => void) | null;
  onerror: ((ev: Event) => void) | null;
}

export interface AreaLayoutResponse {
  kind: "area";
  id: number;
  layout: ClusterLayout;
}

export interface AreaLayoutState {
  layout: ClusterLayout | null;
  pending: boolean;
  mode: "worker" | "inline";
  deaths: number;
  /** True only when an inline run itself took more than a second. */
  slow: boolean;
}

/** First death respawns. The one after that, and any later one, stays inline. */
export function workerAfterDeath(priorDeaths: number): "respawn" | "inline" {
  return priorDeaths < 1 ? "respawn" : "inline";
}

function defaultSpawn(): AreaWorker | null {
  if (typeof Worker === "undefined") return null;
  try {
    return new Worker(new URL("./layout.worker.ts", import.meta.url), { type: "module" }) as unknown as AreaWorker;
  } catch {
    return null;
  }
}

function inputKey(input: ClusterInput | null): string {
  if (!input) return "";
  let h = 2166136261;
  const mix = (s: string) => {
    for (let i = 0; i < s.length; i++) h = Math.imul(h ^ s.charCodeAt(i), 16777619);
    h = Math.imul(h ^ 0xff, 16777619);
  };
  for (const n of input.nodes) {
    mix(n.id);
    mix(n.kind);
  }
  for (const e of input.edges) {
    mix(e.src);
    mix(e.dst);
    mix(e.type);
  }
  mix(String(input.prevDepth ?? ""));
  return (h >>> 0).toString(16);
}

export function useAreaLayout(
  input: ClusterInput | null,
  opts?: { spawn?: () => AreaWorker | null },
): AreaLayoutState {
  const spawn = opts?.spawn ?? defaultSpawn;
  const [layout, setLayout] = React.useState<ClusterLayout | null>(null);
  const [pending, setPending] = React.useState(false);
  const [mode, setMode] = React.useState<"worker" | "inline">("worker");
  const [deaths, setDeaths] = React.useState(0);
  const [slow, setSlow] = React.useState(false);

  const deathsRef = React.useRef(0);
  const inlineRef = React.useRef(false);
  const workerRef = React.useRef<AreaWorker | null>(null);
  const reqRef = React.useRef(0);
  const inputRef = React.useRef(input);
  inputRef.current = input;
  const spawnRef = React.useRef(spawn);
  spawnRef.current = spawn;
  const depthRef = React.useRef<number | undefined>(input?.prevDepth);

  const runInline = React.useCallback((src: ClusterInput) => {
    const t0 = performance.now();
    const next = clustersLayout({ ...src, prevDepth: depthRef.current });
    depthRef.current = next.depth ?? undefined;
    setLayout(next);
    setPending(false);
    setMode("inline");
    if (performance.now() - t0 > 1000) setSlow(true);
  }, []);

  const onMessage = React.useCallback((ev: { data: AreaLayoutResponse }) => {
    if (!ev.data || ev.data.kind !== "area") return;
    if (ev.data.id !== reqRef.current) return;
    depthRef.current = ev.data.layout.depth ?? undefined;
    setLayout(ev.data.layout);
    setPending(false);
  }, []);

  const wire = React.useCallback((worker: AreaWorker) => {
    worker.onmessage = onMessage;
    worker.onerror = () => {
      const prior = deathsRef.current;
      deathsRef.current = prior + 1;
      setDeaths(prior + 1);
      try {
        worker.terminate();
      } catch {
        // A worker that already died has nothing to terminate.
      }
      if (workerRef.current === worker) workerRef.current = null;
      const src = inputRef.current;
      if (workerAfterDeath(prior) === "respawn") {
        const next = spawnRef.current();
        if (!next) {
          inlineRef.current = true;
          if (src) runInline(src);
          return;
        }
        workerRef.current = next;
        wire(next);
        if (src) {
          const id = ++reqRef.current;
          setPending(true);
          next.postMessage({ kind: "area", id, ...src, prevDepth: depthRef.current });
        }
        return;
      }
      inlineRef.current = true;
      console.warn("layout worker died twice; using inline layout for this session");
      if (src) runInline(src);
    };
  }, [onMessage, runInline]);

  const key = inputKey(input);

  React.useEffect(() => {
    const src = inputRef.current;
    if (!src || src.nodes.length === 0) {
      setLayout(null);
      setPending(false);
      return;
    }
    if (inlineRef.current) {
      runInline(src);
      return;
    }
    let worker = workerRef.current;
    if (!worker) {
      worker = spawnRef.current();
      if (!worker) {
        inlineRef.current = true;
        runInline(src);
        return;
      }
      workerRef.current = worker;
      wire(worker);
    }
    const id = ++reqRef.current;
    setPending(true);
    worker.postMessage({ kind: "area", id, ...src, prevDepth: depthRef.current });
  }, [key, runInline, wire]);

  React.useEffect(() => {
    return () => {
      workerRef.current?.terminate();
      workerRef.current = null;
    };
  }, []);

  return { layout, pending, mode, deaths, slow };
}
