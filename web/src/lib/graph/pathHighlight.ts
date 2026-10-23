import type { CodePath } from "@/lib/types";

/** The shape the canvas dims against — identical to the reach highlight's, on purpose. */
export interface Highlight {
  hlNodes: Set<string>;
  hlEdges: Set<string>;
}

interface Edge {
  src: string;
  dst: string;
  type: string;
}

/**
 * A path, expressed as a highlight (PRD-20 AC-19, "off-path dimmed").
 *
 * **One dimming language, not two.** The canvas already dims everything outside the reach ring;
 * returning the same `{hlNodes, hlEdges}` shape means a path *replaces* that rather than layering
 * a second set of rules over the same pixels. Nothing in the renderer needs to know a path exists.
 *
 * **Only the hop edges light.** Lighting every edge that happens to run between two nodes on the
 * route draws a thicket and calls it a path — on a dense graph the shortest route between two
 * hubs would come back looking like a subgraph. The type is matched as well as the endpoints,
 * because two nodes can be joined by both `imports` and `references` and only one of them is the
 * hop that was walked.
 *
 * Orientation-insensitive, because the walk is: `path` may return a hop as `src -> dst` where the
 * drawn edge runs `dst -> src`. That is exactly what `forward: false` records, and it must not
 * cause the edge to be missed.
 */
export function pathHighlight(path: CodePath | null, edges: Edge[]): Highlight | null {
  if (!path?.found || path.hops.length === 0) return null;

  const hlNodes = new Set<string>();
  for (const h of path.hops) {
    hlNodes.add(h.src);
    hlNodes.add(h.dst);
  }

  const hlEdges = new Set<string>();
  edges.forEach((e, i) => {
    const walked = path.hops.some(
      (h) =>
        e.type === h.type &&
        ((e.src === h.src && e.dst === h.dst) || (e.src === h.dst && e.dst === h.src)),
    );
    if (walked) hlEdges.add(String(i));
  });

  return { hlNodes, hlEdges };
}

/**
 * Off-path nodes in the canvas alpha buffer.
 *
 * The same 0.22 the reach ring already uses, so a path replaces that dimming
 * instead of inventing a second scale. 0.22 × 255, rounded once.
 */
export const PATH_NODE_OFF = Math.round(0.22 * 255);

/**
 * One alpha buffer for a found path. Null when there is nothing to dim.
 *
 * Written by the caller when the result arrives, not per frame: the buffer is
 * O(nodes) and the canvas already rebuilds a frame from whatever buffer it was
 * given. Scanning the edge list here would make the path frame cost more than
 * the pan frame, which is the budget D6 names.
 */
export function pathAlphaBuffer(ids: readonly string[], path: CodePath | null): Uint8Array | null {
  if (!path?.found || path.hops.length === 0) return null;
  const on = new Set<string>();
  for (const h of path.hops) {
    on.add(h.src);
    on.add(h.dst);
  }
  const out = new Uint8Array(ids.length);
  for (let i = 0; i < ids.length; i++) out[i] = on.has(ids[i]) ? 255 : PATH_NODE_OFF;
  return out;
}
