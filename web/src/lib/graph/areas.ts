/**
 * Named areas for the code graph (Build.dc.html, GRPH-1051).
 *
 * The design colours by cluster, chips by cluster, and draws a hull around a cluster.
 * The server has no cluster column — modules already *are* areas (`kind=module`, a package
 * or directory), and `owns` edges already walk module → file → symbol. This file names that
 * axis rather than inventing one.
 *
 * Colour is a hash onto the existing token set (PRD-20 G7: no new colour vocabulary).
 */

/** Tokens already in `index.css`. Order is stable so a given area keeps its colour. */
export const AREA_PALETTE = [
  "#c6f24e", // --color-accent
  "#7ca2ff", // --color-st-next
  "#a78bfa", // --color-purple
  "#5fd07a", // --color-st-done
  "#e0b34a", // --color-st-review
  "#c9b8ff", // --color-purple-2
  "#ff8f8f", // --color-ty-bug
] as const;

function hash(s: string): number {
  let h = 0;
  for (let i = 0; i < s.length; i++) h = (Math.imul(h, 31) + s.charCodeAt(i)) | 0;
  return Math.abs(h);
}

export function areaColor(area: string): string {
  return AREA_PALETTE[hash(area) % AREA_PALETTE.length];
}

/** `owns` dst → src. Last write wins after a sorted pass, so the map is order-independent. */
export function parentMap(owns: { src: string; dst: string }[]): Record<string, string> {
  const m: Record<string, string> = {};
  const sorted = [...owns].sort((a, b) =>
    a.dst < b.dst ? -1 : a.dst > b.dst ? 1 : a.src < b.src ? -1 : a.src > b.src ? 1 : 0,
  );
  for (const e of sorted) m[e.dst] = e.src;
  return m;
}

/**
 * The area a node belongs to: the owning module, else the directory the path lives in.
 *
 * A module is itself. A file or symbol walks `owns` until it hits a module. Without an owns
 * edge, a path with a directory (`web/src/a.ts`, `a.py::foo` under `web/`) still shares an
 * area with its siblings — that prefix is real, not invented. A bare filename is its own
 * area rather than a silent "root" bucket that would colour the whole graph as one.
 */
export function areaOf(
  path: string,
  kind: string,
  parentOf: Record<string, string>,
  kindOf: Record<string, string>,
): string {
  if (kind === "module") return path;
  let cur = path;
  const seen = new Set<string>();
  while (parentOf[cur] && !seen.has(cur)) {
    seen.add(cur);
    cur = parentOf[cur];
    if ((kindOf[cur] ?? "") === "module") return cur;
  }
  const file = path.split("::")[0] ?? path;
  const parts = file.split("/").filter(Boolean);
  if (parts.length > 1) {
    parts.pop();
    return parts.join("/");
  }
  return path;
}

export interface AreaGroup {
  id: string;
  members: string[];
}

export function groupAreas(ids: string[], membership: Record<string, string>): AreaGroup[] {
  const buckets = new Map<string, string[]>();
  for (const id of ids) {
    const a = membership[id] ?? id;
    const list = buckets.get(a) ?? [];
    list.push(id);
    buckets.set(a, list);
  }
  return [...buckets.entries()]
    .sort(([a], [b]) => (a < b ? -1 : a > b ? 1 : 0))
    .map(([id, members]) => ({ id, members: members.sort() }));
}

/**
 * Chips and jump cards: named modules when there are two or more, else groups of size ≥2.
 * A chip row of one is a legend of one; a chip per orphan file is a second node list.
 */
export function areasForChips(groups: AreaGroup[], kindOf: Record<string, string>): AreaGroup[] {
  const modules = groups.filter((g) => kindOf[g.id] === "module");
  if (modules.length >= 2) return modules;
  return groups.filter((g) => g.members.length >= 2);
}
