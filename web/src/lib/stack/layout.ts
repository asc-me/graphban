import type { StackEdge, StackLiveness, StackNode, StackTopology } from "@/lib/types";

/**
 * Derived spatial layout for the Stack canvas (GRPH-1034 / PRD-P51 §2, design
 * `docs/design/Stack.dc.html`). Nothing here is hand-placed per instance: every
 * box, ring frame, host frame and edge route is computed from the topology the
 * API returns, so the map is a function of the running system, not a drawing.
 *
 * Scheme (mirrors the design): ownership rings are three NESTED frames; external
 * services (ring 3) sit in a right-hand column; our code (ring 1) and the infra we
 * run (ring 2) fill the left/centre, grouped into host columns. Ring frames are the
 * padded bounding boxes of their nodes, and because ring2's box is the bbox of
 * (ring1 ∪ ring2) and ring3 is the whole canvas, the frames nest by construction.
 */

export interface Box {
  x: number;
  y: number;
  w: number;
  h: number;
}

export interface RingFrame extends Box {
  ring: number;
  label: string;
}

export interface HostFrame extends Box {
  id: string;
  label: string;
}

export interface EdgeRoute {
  from: string;
  to: string;
  protocol: string;
  label: string;
  /** Polyline in canvas coords; last point is the arrow head (at the `to` box). */
  points: { x: number; y: number }[];
  /** Mid-point of the route, for the protocol label. */
  mid: { x: number; y: number };
  liveness: StackLiveness;
}

export interface StackLayout {
  width: number;
  height: number;
  boxes: Record<string, Box>;
  rings: RingFrame[];
  hostFrames: HostFrame[];
  edges: EdgeRoute[];
  /** Corner points of every box — for fit-to-screen. */
  fitPoints: { x: number; y: number }[];
}

const RING_LABEL: Record<number, string> = {
  1: "1 · CODE WE WRITE — our deployables",
  2: "2 · INFRASTRUCTURE WE RUN — we operate it, didn't write it",
  3: "3 · EXTERNAL SERVICES — we only call them",
};

// Host column order: user machines, then agent hosts, then the compose/Railway
// stack. Anything unrecognised falls after. External hosts get their own column.
function hostRank(id: string): number {
  const s = id.toLowerCase();
  if (s.includes("user")) return 0;
  if (s.includes("agent") || s.includes("outpost")) return 1;
  if (s.includes("compose") || s.includes("stack") || s.includes("railway")) return 2;
  return 3;
}

const PAD = 80; // canvas padding outside ring 3
const COL_GAP = 120; // horizontal gap between host columns
const EXT_GAP = 150; // gap before the external column
const V_GAP = 40; // vertical gap between boxes in a column
const TOP = 150; // headroom for ring labels

/**
 * Content-driven box size. Boxes stay close in size so the whole map fits and
 * reads at once — api (surfaces) and db (extension) run a little larger, but no
 * box is allowed to blow out the canvas. Surfaces render as compact wrapped
 * chips (not full-size tiles), so even api's four surfaces cost ~two lines, and
 * the full list lives in the inspector.
 */
function sizeOf(n: StackNode): { w: number; h: number } {
  const external = n.ring >= 3;
  const surfaces = n.surfaces.length;
  const extensions = n.extensions.length;
  const outposts = n.outposts?.length ?? 0;
  if (external) return { w: 220, h: 92 };
  let h = 104;
  // Surface chips wrap ~2 per line in a 250px box; >2 surfaces takes a 2nd line.
  if (surfaces > 0) h += surfaces > 2 ? 48 : 30;
  if (extensions > 0) h += 30;
  if (outposts > 0) h += outposts * 20;
  // Ring-1 deployables carry the code-health footer (GRPH-1035); reserve its
  // height so content never clips when the overlay is on (its default).
  if (n.ring === 1) h += 24;
  const w = extensions > 0 || surfaces > 0 ? 250 : 220;
  return { w, h };
}

function pad(b: Box, p: number): Box {
  return { x: b.x - p, y: b.y - p, w: b.w + p * 2, h: b.h + p * 2 };
}

function bbox(boxes: Box[]): Box {
  const xs = boxes.flatMap((b) => [b.x, b.x + b.w]);
  const ys = boxes.flatMap((b) => [b.y, b.y + b.h]);
  const minX = Math.min(...xs);
  const minY = Math.min(...ys);
  return { x: minX, y: minY, w: Math.max(...xs) - minX, h: Math.max(...ys) - minY };
}

/** Rank liveness worst-first so an edge takes the colour of its unhealthiest end. */
const LIVE_RANK: Record<StackLiveness, number> = {
  unreachable: 3,
  uncalibrated: 2,
  unknown: 2,
  not_configured: 1,
  reachable: 0,
};

function edgeLiveness(a?: StackNode, b?: StackNode): StackLiveness {
  const la = a?.liveness ?? "unknown";
  const lb = b?.liveness ?? "unknown";
  return LIVE_RANK[la] >= LIVE_RANK[lb] ? la : lb;
}

function center(b: Box) {
  return { x: b.x + b.w / 2, y: b.y + b.h / 2 };
}

/** Orthogonal elbow between two boxes, leaving/entering on the facing sides. */
function route(s: Box, t: Box): { points: { x: number; y: number }[]; mid: { x: number; y: number } } {
  const sc = center(s);
  const tc = center(t);
  const dx = tc.x - sc.x;
  const dy = tc.y - sc.y;
  let p0: { x: number; y: number };
  let p3: { x: number; y: number };
  let pts: { x: number; y: number }[];
  if (Math.abs(dx) >= Math.abs(dy)) {
    // horizontal dominant: leave the right/left face, enter the opposite face
    p0 = { x: dx >= 0 ? s.x + s.w : s.x, y: sc.y };
    p3 = { x: dx >= 0 ? t.x : t.x + t.w, y: tc.y };
    const midX = (p0.x + p3.x) / 2;
    pts = [p0, { x: midX, y: p0.y }, { x: midX, y: p3.y }, p3];
  } else {
    // vertical dominant: leave the bottom/top face
    p0 = { x: sc.x, y: dy >= 0 ? s.y + s.h : s.y };
    p3 = { x: tc.x, y: dy >= 0 ? t.y : t.y + t.h };
    const midY = (p0.y + p3.y) / 2;
    pts = [p0, { x: p0.x, y: midY }, { x: p3.x, y: midY }, p3];
  }
  const mid = pts[1]; // the first bend reads clearly for the protocol label
  return { points: pts, mid };
}

export function buildStackLayout(topo: StackTopology): StackLayout {
  const byId: Record<string, StackNode> = Object.fromEntries(topo.nodes.map((n) => [n.id, n]));
  const boxes: Record<string, Box> = {};

  // Which nodes are external (ring 3) vs left/centre (ring 1-2).
  const externalIds = topo.nodes.filter((n) => n.ring >= 3).map((n) => n.id);
  const innerNodes = topo.nodes.filter((n) => n.ring < 3);

  // Host columns for the inner (ring 1-2) nodes, ordered by host rank.
  const innerHosts = Array.from(new Set(innerNodes.map((n) => n.host))).sort((a, b) => {
    const r = hostRank(a) - hostRank(b);
    return r !== 0 ? r : a.localeCompare(b);
  });

  // Within a column, stack by ring then the order the API returned — so the entry
  // deployable (e.g. web) stays above the backend above the infra it sits on, and a
  // deeper-ring box never lands between two boxes an edge runs between.
  const apiOrder = new Map(topo.nodes.map((n, i) => [n.id, i]));

  let cursorX = PAD + 40;
  const columnHostFrames: HostFrame[] = [];
  for (const host of innerHosts) {
    const nodes = innerNodes
      .filter((n) => n.host === host)
      .sort(
        (a, b) => a.ring - b.ring || (apiOrder.get(a.id) ?? 0) - (apiOrder.get(b.id) ?? 0),
      );
    const sizes = nodes.map(sizeOf);
    const colW = Math.max(...sizes.map((s) => s.w));
    let y = TOP + 20;
    nodes.forEach((n, i) => {
      const s = sizes[i];
      boxes[n.id] = { x: cursorX + (colW - s.w) / 2, y, w: s.w, h: s.h };
      y += s.h + V_GAP;
    });
    const hostBoxes = nodes.map((n) => boxes[n.id]);
    const hl = pad(bbox(hostBoxes), 22);
    const label = topo.hosts.find((h) => h.id === host)?.label ?? host;
    columnHostFrames.push({ id: host, label, ...hl });
    cursorX += colW + COL_GAP;
  }

  // External column on the right.
  const innerRight = cursorX - COL_GAP;
  const extX = innerRight + EXT_GAP;
  const extNodes = externalIds.map((id) => byId[id]).filter(Boolean);
  let ey = TOP + 20;
  let extW = 220;
  for (const n of extNodes) {
    const s = sizeOf(n);
    extW = Math.max(extW, s.w);
    boxes[n.id] = { x: extX, y: ey, w: s.w, h: s.h };
    ey += s.h + 22;
  }

  // Ring frames — nested bounding boxes (ring1 ⊂ ring2 ⊂ ring3-canvas).
  const ring1 = topo.nodes.filter((n) => n.ring === 1).map((n) => boxes[n.id]).filter(Boolean);
  const ring12 = topo.nodes
    .filter((n) => n.ring <= 2)
    .map((n) => boxes[n.id])
    .filter(Boolean);
  const allBoxes = Object.values(boxes);

  const rings: RingFrame[] = [];
  const canvasInner = allBoxes.length ? pad(bbox(allBoxes), 44) : { x: 0, y: 0, w: 800, h: 400 };
  // ring 3 spans the whole canvas (externals + everything).
  const ring3Frame: Box = {
    x: PAD,
    y: TOP - 70,
    w: canvasInner.x + canvasInner.w - PAD + 24,
    h: canvasInner.y + canvasInner.h - (TOP - 70) + 24,
  };
  // Stagger the nested tops so each ring's corner label has its own line and the
  // nesting reads from the top-left, the way the design frames it.
  const r3Top = ring3Frame.y;
  if (topo.nodes.some((n) => n.ring === 3)) {
    rings.push({ ring: 3, label: RING_LABEL[3], ...ring3Frame });
  }
  if (ring12.length) {
    const r2 = pad(bbox(ring12), 34);
    const top = r3Top + 30;
    rings.push({ ring: 2, label: RING_LABEL[2], x: r2.x, y: top, w: r2.w, h: r2.y + r2.h - top });
  }
  if (ring1.length) {
    const r1 = pad(bbox(ring1), 20);
    const top = r3Top + 58;
    rings.push({ ring: 1, label: RING_LABEL[1], x: r1.x, y: top, w: r1.w, h: r1.y + r1.h - top });
  }

  // Edge routes.
  const edges: EdgeRoute[] = topo.edges
    .map((e: StackEdge): EdgeRoute | null => {
      const s = boxes[e.from];
      const t = boxes[e.to];
      if (!s || !t) return null;
      const { points, mid } = route(s, t);
      return {
        from: e.from,
        to: e.to,
        protocol: e.protocol,
        label: e.label || e.protocol,
        points,
        mid,
        liveness: edgeLiveness(byId[e.from], byId[e.to]),
      };
    })
    .filter((e): e is EdgeRoute => !!e);

  const width = ring3Frame.x + ring3Frame.w + PAD;
  const height = ring3Frame.y + ring3Frame.h + PAD;
  const fitPoints = allBoxes.flatMap((b) => [
    { x: b.x, y: b.y },
    { x: b.x + b.w, y: b.y + b.h },
  ]);

  return { width, height, boxes, rings, hostFrames: columnHostFrames, edges, fitPoints };
}
