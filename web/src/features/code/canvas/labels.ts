/**
 * DOM label picker (PRD-52 D5).
 *
 * Focus first, then up to 36 neighbours (12 when the focus is a module), then
 * a per-area budget once zoom is at least 1.5. The total is capped at 140.
 * Colliding boxes are dropped. The selected label is kept and wins the cell.
 */

export const LABEL_CAP = 140;

export interface Box {
  x: number;
  y: number;
  w: number;
  h: number;
}

export interface LabelNode {
  id: string;
  area: string;
  degree: number;
  box: Box;
  inView: boolean;
}

export function areaBudget(zoom: number): number {
  return Math.floor((zoom - 1.1) ** 2 * 2.5) + 1;
}

export function boxesOverlap(a: Box, b: Box): boolean {
  return a.x < b.x + b.w && a.x + a.w > b.x && a.y < b.y + b.h && a.y + a.h > b.y;
}

export function pickLabels(opts: {
  nodes: LabelNode[];
  selectedId: string | null;
  hoverId: string | null;
  /** Neighbours of the focus, already in the order they should be considered. */
  neighbors: string[];
  focusKind: string;
  zoom: number;
}): string[] {
  const byId = new Map(opts.nodes.map((n) => [n.id, n]));
  const focus = opts.hoverId ?? opts.selectedId;
  const neighborCap = opts.focusKind === "module" ? 12 : 36;
  const ordered: string[] = [];
  const seen = new Set<string>();
  const forced = new Set<string>();

  const push = (id: string | null, force: boolean) => {
    if (!id || seen.has(id)) return;
    const node = byId.get(id);
    if (!node) return;
    if (!node.inView && !force) return;
    seen.add(id);
    ordered.push(id);
    if (force) forced.add(id);
  };

  // Selected is always a candidate, even off-screen. Hover too: it is the focus.
  push(opts.selectedId, true);
  push(opts.hoverId, true);

  let taken = 0;
  for (const id of opts.neighbors) {
    if (taken >= neighborCap) break;
    if (!focus || id === focus) continue;
    const before = ordered.length;
    push(id, false);
    if (ordered.length !== before) taken += 1;
  }

  if (opts.zoom >= 1.5) {
    const budget = areaBudget(opts.zoom);
    const byArea = new Map<string, LabelNode[]>();
    for (const node of opts.nodes) {
      if (!node.inView || seen.has(node.id)) continue;
      const list = byArea.get(node.area);
      if (list) list.push(node);
      else byArea.set(node.area, [node]);
    }
    for (const list of byArea.values()) {
      list.sort((a, b) => b.degree - a.degree || (a.id < b.id ? -1 : a.id > b.id ? 1 : 0));
      for (let i = 0; i < list.length && i < budget; i++) push(list[i].id, false);
    }
  }

  const kept: string[] = [];
  const boxes: Box[] = [];
  for (const id of ordered) {
    if (kept.length >= LABEL_CAP && !forced.has(id)) continue;
    if (kept.length >= LABEL_CAP) break;
    const box = byId.get(id)!.box;
    const hit = boxes.findIndex((b) => boxesOverlap(b, box));
    if (hit >= 0) {
      // The selected label wins. Anything else, including a colliding hover, is dropped
      // so the drawn set does not overlap.
      continue;
    }
    kept.push(id);
    boxes.push(box);
  }
  return kept;
}
