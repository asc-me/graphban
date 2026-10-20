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
/**
 * How many outpost (agent-host) rows a box shows before it collapses the rest
 * into a "+N more" line. The fleet node lists one row per agent host, and a busy
 * instance has dozens of them (retired waves included) — rendering them all made
 * the box grow 20px a row until it was thousands of px tall and the whole map
 * shrank to an unreadable speck. The full list still lives in the inspector.
 * Shared with StackView so the drawn rows and the reserved height agree.
 */
export const OUTPOST_PREVIEW = 4;

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
  // Only the previewed outpost rows cost height; everything past the cap is one
  // "+N more" line, so a node with 4 hosts and one with 400 size the same.
  if (outposts > 0) {
    h += Math.min(outposts, OUTPOST_PREVIEW) * 20;
    if (outposts > OUTPOST_PREVIEW) h += 18;
  }
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

type Side = "left" | "right" | "top" | "bottom";
const isHoriz = (s: Side) => s === "left" || s === "right";

/** A point a fraction `t` (0..1) along one face of a box. */
function facePoint(b: Box, side: Side, t: number): { x: number; y: number } {
  switch (side) {
    case "left":
      return { x: b.x, y: b.y + b.h * t };
    case "right":
      return { x: b.x + b.w, y: b.y + b.h * t };
    case "top":
      return { x: b.x + b.w * t, y: b.y };
    default:
      return { x: b.x + b.w * t, y: b.y + b.h };
  }
}

/**
 * Which faces an edge leaves and enters, from the dominant axis between the two
 * box centres. Faces are always an opposing pair on one axis (right↔left or
 * bottom↔top), so the orthogonal route is a clean channel elbow, never an L
 * across a corner.
 */
function facesFor(s: Box, t: Box): { sSide: Side; tSide: Side } {
  const dx = center(t).x - center(s).x;
  const dy = center(t).y - center(s).y;
  if (Math.abs(dx) >= Math.abs(dy)) {
    return { sSide: dx >= 0 ? "right" : "left", tSide: dx >= 0 ? "left" : "right" };
  }
  return { sSide: dy >= 0 ? "bottom" : "top", tSide: dy >= 0 ? "top" : "bottom" };
}

/**
 * Spread `n` ports across the middle band of a face so several edges sharing one
 * side of a box leave/enter at distinct points instead of stacking on the centre
 * (which overlapped the lines and piled their labels into one unreadable run,
 * e.g. "HTTP HTTP chat"). Index `i` (already ordered by the other endpoint's
 * cross-axis position, so lines don't cross at the face) maps to a fraction in
 * [0.26, 0.74] — off the corners, where a route would clip the box.
 */
function portFrac(i: number, n: number): number {
  if (n <= 1) return 0.5;
  return 0.26 + (0.74 - 0.26) * (i / (n - 1));
}

/**
 * Orthogonal channel route between an exit port and an entry port on opposing
 * faces. The bend sits in the gap BETWEEN the two columns, which is box-free —
 * so the protocol label (placed on that mid segment) is never drawn under a box,
 * and distributed ports give each edge its own channel offset.
 */
function routePorts(
  p0: { x: number; y: number },
  p3: { x: number; y: number },
  horizontal: boolean,
  shift: number,
): { points: { x: number; y: number }[]; mid: { x: number; y: number } } {
  if (horizontal) {
    // Stagger the channel so a fan of edges leaving one box (e.g. web → four
    // model providers) doesn't stack every vertical run on one x. Clamp the
    // shift inside the gap so the bend never re-enters either box.
    const span = Math.abs(p3.x - p0.x);
    const midX = (p0.x + p3.x) / 2 + Math.max(-span / 2 + 8, Math.min(span / 2 - 8, shift));
    return {
      points: [p0, { x: midX, y: p0.y }, { x: midX, y: p3.y }, p3],
      mid: { x: midX, y: (p0.y + p3.y) / 2 },
    };
  }
  const span = Math.abs(p3.y - p0.y);
  const midY = (p0.y + p3.y) / 2 + Math.max(-span / 2 + 8, Math.min(span / 2 - 8, shift));
  return {
    points: [p0, { x: p0.x, y: midY }, { x: p3.x, y: midY }, p3],
    mid: { x: (p0.x + p3.x) / 2, y: midY },
  };
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

  // Ownership rings — three frames that nest by construction. Each pad is larger
  // than the host-frame pad (22) and grows outward, so ring1 ⊃ host boxes, ring2 ⊃
  // ring1, ring3 ⊃ everything, and no ring edge ever crosses a node box or the
  // host frames the way the tight ring1 (pad 20 < host 22) used to. The corner
  // labels sit ABOVE each frame's top edge (RING_LABEL_DY), so they stack in the
  // clear band over the canvas instead of piling onto the host labels and boxes.
  const rings: RingFrame[] = [];
  const mkRing = (ring: number, box: Box) =>
    rings.push({ ring, label: RING_LABEL[ring], ...box });
  if (ring1.length) mkRing(1, pad(bbox(ring1), 40));
  if (ring12.length) mkRing(2, pad(bbox(ring12), 70));
  if (allBoxes.length) {
    // Ring 3 wraps the whole canvas (externals included) with the widest pad, and
    // is drawn first; a project with no external nodes still gets the outer frame.
    mkRing(3, pad(bbox(allBoxes), 100));
  }
  // Draw outermost first so inner frames and their labels sit on top.
  rings.sort((a, b) => b.ring - a.ring);
  const ring3Frame = rings.find((r) => r.ring === 3) ?? { x: PAD, y: TOP - 70, w: 800, h: 400 };

  // Edge routes — two passes so parallel edges don't pile on one port.
  //
  // Pass 1 plans each edge's exit/entry faces. Pass 2 groups edges by the face
  // they share on a box and spreads their ports across that face (ordered by the
  // other endpoint, so the fan doesn't self-cross). Center-to-center routing used
  // to overlap every edge leaving a box and stack their labels; distributing the
  // ports and putting the label in the box-free channel fixes both.
  interface EdgePlan {
    e: StackEdge;
    s: Box;
    t: Box;
    sSide: Side;
    tSide: Side;
  }
  const plans: EdgePlan[] = [];
  for (const e of topo.edges) {
    const s = boxes[e.from];
    const t = boxes[e.to];
    if (!s || !t) continue;
    plans.push({ e, s, t, ...facesFor(s, t) });
  }

  // Group endpoints by `${nodeId}:${side}`; each entry carries the cross-axis
  // coordinate of the OTHER box so ports can be ordered to avoid crossings.
  interface Endpoint {
    plan: EdgePlan;
    end: "s" | "t";
    cross: number;
  }
  const groups = new Map<string, Endpoint[]>();
  const add = (key: string, ep: Endpoint) => {
    const arr = groups.get(key);
    if (arr) arr.push(ep);
    else groups.set(key, [ep]);
  };
  for (const p of plans) {
    const sc = center(p.s);
    const tc = center(p.t);
    add(`${p.e.from}:${p.sSide}`, {
      plan: p,
      end: "s",
      cross: isHoriz(p.sSide) ? tc.y : tc.x,
    });
    add(`${p.e.to}:${p.tSide}`, {
      plan: p,
      end: "t",
      cross: isHoriz(p.tSide) ? sc.y : sc.x,
    });
  }

  // Resolve each endpoint to a fraction along its face.
  const portOf = new Map<Endpoint, number>();
  for (const arr of groups.values()) {
    arr.sort((a, b) => a.cross - b.cross || (a.end < b.end ? -1 : 1));
    arr.forEach((ep, i) => portOf.set(ep, portFrac(i, arr.length)));
    // Keep the resolved slot reachable from the plan for the build step below.
    arr.forEach((ep) => {
      if (ep.end === "s") (ep.plan as EdgePlan & { sT?: number }).sT = portOf.get(ep);
      else (ep.plan as EdgePlan & { tT?: number }).tT = portOf.get(ep);
    });
  }

  const edges: EdgeRoute[] = plans.map((p): EdgeRoute => {
    const sT = (p as EdgePlan & { sT?: number }).sT ?? 0.5;
    const tT = (p as EdgePlan & { tT?: number }).tT ?? 0.5;
    const p0 = facePoint(p.s, p.sSide, sT);
    const p3 = facePoint(p.t, p.tSide, tT);
    // Spread each edge's channel a little by its exit-port fraction, so a fan
    // from one box doesn't collapse its vertical runs onto a single line.
    const { points, mid } = routePorts(p0, p3, isHoriz(p.sSide), (sT - 0.5) * 44);
    return {
      from: p.e.from,
      to: p.e.to,
      protocol: p.e.protocol,
      label: p.e.label || p.e.protocol,
      points,
      mid,
      liveness: edgeLiveness(byId[p.e.from], byId[p.e.to]),
    };
  });

  const width = ring3Frame.x + ring3Frame.w + PAD;
  const height = ring3Frame.y + ring3Frame.h + PAD;
  const fitPoints = allBoxes.flatMap((b) => [
    { x: b.x, y: b.y },
    { x: b.x + b.w, y: b.y + b.h },
  ]);

  return { width, height, boxes, rings, hostFrames: columnHostFrames, edges, fitPoints };
}
