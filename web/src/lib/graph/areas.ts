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

/**
 * Canonical input (PRD-52 D4). The server has no ORDER BY, so this order is the
 * only index. Member lists keep THIS order — sorting them again would hide a
 * missing sort from the shuffle test.
 */
export function canonicalNodes<T extends { path: string; id: string }>(nodes: T[]): T[] {
  return [...nodes].sort((a, b) => {
    if (a.path !== b.path) return a.path < b.path ? -1 : 1;
    return a.id < b.id ? -1 : a.id > b.id ? 1 : 0;
  });
}

export function canonicalEdges<T extends { src: string; dst: string; type?: string }>(
  edges: T[],
): T[] {
  return [...edges].sort((a, b) => {
    if (a.src !== b.src) return a.src < b.src ? -1 : 1;
    if (a.dst !== b.dst) return a.dst < b.dst ? -1 : 1;
    const ta = a.type ?? "";
    const tb = b.type ?? "";
    return ta < tb ? -1 : ta > tb ? 1 : 0;
  });
}

export interface ScaleNode {
  id: string;
  path: string;
  kind: string;
}

export interface ScaleEdge {
  src: string;
  dst: string;
  type: string;
}

export interface ScaleArea {
  id: string;
  members: string[];
  count: number;
  color: string;
  /** Set on the single area of a flat repo. */
  flat?: boolean;
  /** "flat repo: one area" when `flat` is set. */
  caption?: string;
}

export interface AreasAtScale {
  /** Chosen prefix depth, 1–6. Null when the repo is flat. */
  depth: number | null;
  flat: boolean;
  /** True when `prevDepth` was kept because its area count sat in 10–36. */
  kept: boolean;
  /** Lexicographic by id, with a non-empty `other` last. */
  areas: ScaleArea[];
  /** Always present. Count is 0 when every node has a home. */
  other: { count: number; members: string[] };
}

const DEPTHS = [1, 2, 3, 4, 5, 6] as const;
const BAND_MIN = 12;
const BAND_MAX = 30;
const KEEP_MIN = 10;
const KEEP_MAX = 36;
const OTHER_CAP = 0.15;
const FLAT_CAPTION = "flat repo: one area";

function byText(a: string, b: string): number {
  return a < b ? -1 : a > b ? 1 : 0;
}

function segments(path: string): string[] {
  const file = path.split("::")[0] ?? path;
  return file.split("/").filter(Boolean);
}

/** Prefix of `path` at `depth`, or the whole path when it is shorter. Null when it has no segments. */
function prefixAt(path: string, depth: number): string | null {
  const parts = segments(path);
  if (parts.length === 0) return null;
  return parts.slice(0, Math.min(depth, parts.length)).join("/");
}

function rollupThreshold(n: number): number {
  return Math.max(8, n * 0.01);
}

/** Owning module's path, else the node's own path. */
function homePaths(nodes: ScaleNode[], owns: { src: string; dst: string }[]): Map<string, string> {
  const byId = new Map(nodes.map((n) => [n.id, n]));
  const parent = parentMap(owns);
  const out = new Map<string, string>();
  for (const n of nodes) {
    if (n.kind === "module") {
      out.set(n.id, n.path);
      continue;
    }
    let cur = n.id;
    const seen = new Set<string>();
    let home = n.path;
    while (parent[cur] && !seen.has(cur)) {
      seen.add(cur);
      cur = parent[cur];
      const p = byId.get(cur);
      if (p?.kind === "module") {
        home = p.path;
        break;
      }
    }
    out.set(n.id, home);
  }
  return out;
}

interface Partition {
  depth: number;
  groups: Map<string, string[]>;
  other: string[];
  areaCount: number;
  otherFrac: number;
}

/**
 * Group `orderedIds` (already canonical) by prefix at `depth`.
 * A group smaller than max(8, 1% of nodes) rolls into its parent prefix.
 * A small group with no parent, and anything that never gets a prefix, is `other`.
 */
function partitionAt(
  orderedIds: string[],
  homeOf: Map<string, string>,
  depth: number,
  n: number,
): Partition {
  const threshold = rollupThreshold(n);
  const groups = new Map<string, string[]>();
  for (const id of orderedIds) {
    const key = prefixAt(homeOf.get(id) ?? "", depth);
    if (!key) continue;
    const list = groups.get(key);
    if (list) list.push(id);
    else groups.set(key, [id]);
  }

  // Deepest first, so a child joins its parent before the parent is judged.
  for (let guard = 0; guard < 8; guard++) {
    const keys = [...groups.keys()].sort((a, b) => {
      const da = a.split("/").length;
      const db = b.split("/").length;
      if (da !== db) return db - da;
      return byText(a, b);
    });
    let moved = false;
    for (const key of keys) {
      const members = groups.get(key);
      if (!members || members.length >= threshold) continue;
      groups.delete(key);
      const parts = key.split("/");
      if (parts.length > 1) {
        parts.pop();
        const parent = parts.join("/");
        const existing = groups.get(parent);
        if (existing) existing.push(...members);
        else groups.set(parent, members.slice());
      }
      moved = true;
    }
    if (!moved) break;
  }

  const owner = new Map<string, string>();
  for (const [key, members] of groups) {
    if (members.length < threshold) continue;
    for (const id of members) owner.set(id, key);
  }
  const rebuilt = new Map<string, string[]>();
  const other: string[] = [];
  for (const id of orderedIds) {
    const key = owner.get(id);
    if (!key) {
      other.push(id);
      continue;
    }
    const list = rebuilt.get(key);
    if (list) list.push(id);
    else rebuilt.set(key, [id]);
  }
  const areaCount = rebuilt.size + (other.length > 0 ? 1 : 0);
  return {
    depth,
    groups: rebuilt,
    other,
    areaCount,
    otherFrac: n === 0 ? 0 : other.length / n,
  };
}

function emit(p: Partition, kept: boolean): AreasAtScale {
  const areas: ScaleArea[] = [...p.groups.entries()]
    .sort((a, b) => byText(a[0], b[0]))
    .map(([id, members]) => ({
      id,
      members,
      count: members.length,
      color: areaColor(id),
    }));
  if (p.other.length > 0) {
    areas.push({
      id: "other",
      members: p.other,
      count: p.other.length,
      color: areaColor("other"),
    });
  }
  return {
    depth: p.depth,
    flat: false,
    kept,
    areas,
    other: { count: p.other.length, members: p.other },
  };
}

function flatResult(ids: string[]): AreasAtScale {
  return {
    depth: null,
    flat: true,
    kept: false,
    areas: [
      {
        id: "flat",
        members: ids,
        count: ids.length,
        color: areaColor("flat"),
        flat: true,
        caption: FLAT_CAPTION,
      },
    ],
    other: { count: 0, members: [] },
  };
}

function bandDistance(count: number): number {
  if (count < BAND_MIN) return BAND_MIN - count;
  if (count > BAND_MAX) return count - BAND_MAX;
  return 0;
}

function shallowest(parts: Partition[]): Partition {
  return parts.reduce((a, b) => (a.depth <= b.depth ? a : b));
}

/**
 * Areas for a map above the detail budget (PRD-52 D2).
 *
 * Home is the owning module's path, else the node's own path. Depths 1..6 are
 * prefix lengths. Selection, in order: keep `prevDepth` when its area count is
 * inside 10–36; otherwise discard depths with fewer than 2 areas and depths
 * whose `other` is above 15% when some candidate does better; then the
 * shallowest depth inside 12–30, else the closest, with `prevDepth` only on an
 * exact distance tie. Every depth under 2 areas is a flat repo.
 */
export function areasAtScale(
  nodes: ScaleNode[],
  owns: { src: string; dst: string; type?: string }[],
  prevDepth?: number,
): AreasAtScale {
  const ordered = canonicalNodes(nodes);
  const orderedOwns = canonicalEdges(owns);
  const homes = homePaths(ordered, orderedOwns);
  const ids = ordered.map((n) => n.id);
  const n = ids.length;
  const parts = DEPTHS.map((d) => partitionAt(ids, homes, d, n));
  const viable = parts.filter((p) => p.areaCount >= 2);
  if (viable.length === 0) return flatResult(ids);

  if (prevDepth != null && prevDepth >= 1 && prevDepth <= 6) {
    const prev = parts[prevDepth - 1];
    if (prev && prev.areaCount >= KEEP_MIN && prev.areaCount <= KEEP_MAX) {
      return emit(prev, true);
    }
  }

  const bestOther = Math.min(...viable.map((p) => p.otherFrac));
  const candidates =
    bestOther <= OTHER_CAP
      ? viable.filter((p) => p.otherFrac <= OTHER_CAP)
      : viable.filter((p) => p.otherFrac <= bestOther + 1e-12);

  const inBand = candidates.filter((p) => p.areaCount >= BAND_MIN && p.areaCount <= BAND_MAX);
  if (inBand.length > 0) return emit(shallowest(inBand), false);

  const bestDist = Math.min(...candidates.map((p) => bandDistance(p.areaCount)));
  const tied = candidates.filter((p) => bandDistance(p.areaCount) === bestDist);
  const prev = prevDepth != null ? tied.find((p) => p.depth === prevDepth) : undefined;
  const chosen = prev && tied.length > 1 ? prev : shallowest(tied);
  return emit(chosen, false);
}
