/// <reference lib="webworker" />
import { computeLayout, type LayoutEdge, type LayoutOpts, type Pos } from "./layout";

export interface LayoutRequest {
  /** Monotonic per hook instance; echoed back so a stale reply can be dropped. */
  id: number;
  ids: string[];
  edges: LayoutEdge[];
  opts: LayoutOpts;
}

export interface LayoutResponse {
  id: number;
  pos: Record<string, Pos>;
}

self.onmessage = (ev: MessageEvent<LayoutRequest>) => {
  const { id, ids, edges, opts } = ev.data;
  const pos = computeLayout(ids, edges, opts);
  (self as unknown as Worker).postMessage({ id, pos } satisfies LayoutResponse);
};
