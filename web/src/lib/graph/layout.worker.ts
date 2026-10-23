/// <reference lib="webworker" />
import { layoutFor, type ClusterInput, type ClusterLayout } from "./bigLayout";
import { runLayout, type LayoutEdge, type LayoutOpts, type Pos } from "./layout";

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

/** Canvas-mode area layout. Distinct from `LayoutRequest`, which GalaxyView still sends. */
export interface AreaLayoutRequest extends ClusterInput {
  kind: "area";
  id: number;
}

export interface AreaLayoutWorkerResponse {
  kind: "area";
  id: number;
  layout: ClusterLayout;
}

function isArea(data: unknown): data is AreaLayoutRequest {
  return !!data && typeof data === "object" && (data as { kind?: string }).kind === "area";
}

self.onmessage = (ev: MessageEvent<LayoutRequest | AreaLayoutRequest>) => {
  if (isArea(ev.data)) {
    const { id, nodes, edges, prevDepth, layout: layoutKind } = ev.data;
    const layout = layoutFor(layoutKind, { nodes, edges, prevDepth });
    const reply: AreaLayoutWorkerResponse = { kind: "area", id, layout };
    (self as unknown as Worker).postMessage(reply);
    return;
  }
  const { id, ids, edges, opts } = ev.data;
  const pos = runLayout(ids, edges, opts);
  (self as unknown as Worker).postMessage({ id, pos } satisfies LayoutResponse);
};
