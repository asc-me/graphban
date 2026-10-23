/**
 * DOM matrix shown when neither WebGL2 nor Canvas 2D can draw (PRD-52 §6).
 * This is the reduced view, not the Matrix layout chip (that chip is S4).
 */

import type { ClusterLayout } from "@/lib/graph/bigLayout";

export interface AreaMatrix {
  ids: string[];
  labels: string[];
  counts: number[][];
}

export function areaMatrix(
  areas: { id: string; label: string }[],
  edges: { srcArea?: string; dstArea?: string }[],
): AreaMatrix {
  const ids = areas.map((a) => a.id);
  const index = new Map(ids.map((id, i) => [id, i]));
  const counts = ids.map(() => ids.map(() => 0));
  for (const e of edges) {
    if (!e.srcArea || !e.dstArea) continue;
    const ia = index.get(e.srcArea);
    const ib = index.get(e.dstArea);
    if (ia === undefined || ib === undefined || ia === ib) continue;
    counts[ia][ib] += 1;
  }
  return { ids, labels: areas.map((a) => a.label), counts };
}

export function ReducedView({ layout }: { layout: ClusterLayout | null }) {
  const matrix = layout ? areaMatrix(layout.areas, layout.edges) : null;
  return (
    <div
      data-testid="reduced-view"
      role="region"
      aria-label="Reduced view"
      className="absolute inset-0 z-10 overflow-auto bg-surface p-3"
    >
      <p className="mb-2 text-small text-fg">Reduced view</p>
      <table data-testid="reduced-matrix" className="border-collapse font-mono text-small text-muted">
        {matrix && (
          <>
            <thead>
              <tr>
                <th />
                {matrix.ids.map((id, i) => (
                  <th key={id} className="px-1 text-center font-normal">
                    {matrix.labels[i]}
                  </th>
                ))}
              </tr>
            </thead>
            <tbody>
              {matrix.ids.map((id, r) => (
                <tr key={id}>
                  <th className="pr-2 text-left font-normal text-fg">{matrix.labels[r]}</th>
                  {matrix.counts[r].map((n, c) => (
                    <td key={matrix.ids[c]} className="px-1 text-center" data-cell={`${id}-${matrix.ids[c]}`}>
                      {n}
                    </td>
                  ))}
                </tr>
              ))}
            </tbody>
          </>
        )}
      </table>
    </div>
  );
}
