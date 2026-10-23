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
  /** The ring number shown in the circled badge (same as `ring`, named for the label). */
  num: number;
  /** Uppercase ownership title, e.g. "CODE WE WRITE". */
  title: string;
  /** Plain-language gloss under the title, e.g. "our deployables". */
  subtitle: string;
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
  /** The "Built with Graphban" frame around an expanded instance, or null. */
  lane: HostFrame | null;
  /** Corner points of every box — for fit-to-screen. */
  fitPoints: { x: number; y: number }[];
}

// ── App-first map (GRPH-1099 / PRD-P55 §4) ──────────────────────────────────
//
// On a project that does not write Graphban, Graphban is the tool that builds the app,
// not the app. Its whole instance collapses into one box in a build lane at the left
// edge, with `builds` edges to the app's deployables, so the app fills the map.

export const BUILD_LANE = "graphban_build";
export const GRAPHBAN_BOX = "graphban:instance";
export const GRAPHBAN_KIND = "graphban";
export const PLACEHOLDER_KIND = "placeholder";
export const UNMAPPED_BOX = "ext:__not_mapped";
export const BUILT_WITH_GRAPHBAN = "Built with Graphban";
export const SERVICES_NOT_MAPPED = "services not mapped yet";
const APP_INFRA = "app_infra";
const APP_HOSTS = new Set(["project_app", APP_INFRA]);
const APP_ID = /^(app|infra|ext|unresolved):/;

/** The project's own box (code map or posted record), not Graphban's instance. */
export function isAppNode(n: StackNode): boolean {
  if (n.kind === PLACEHOLDER_KIND) return true;
  if (n.plane) return n.plane === "app";
  return APP_HOSTS.has(n.host) || APP_ID.test(n.id);
}

/**
 * Whether this project writes Graphban's source. An older API without
 * `graphban_source` answers by ring: Graphban's boxes are ring 1 only when owned.
 */
export function ownsGraphban(topo: StackTopology): boolean {
  if (topo.graphban_source) return topo.graphban_source === "owned";
  return topo.nodes.some((n) => !isAppNode(n) && n.ring === 1);
}

/** No record was posted (or an older API sent no mapping block, which is the same). */
export function isUnmapped(topo: StackTopology): boolean {
  return (topo.mapping?.state ?? "unmapped") === "unmapped";
}

export interface StackMapView {
  /** The topology to draw: collapsed or expanded, with any placeholder added. */
  topo: StackTopology;
  /** Graphban can collapse here (the project does not write it). */
  collapsible: boolean;
  collapsed: boolean;
  layoutOptions: StackLayoutOptions;
}

// Worst-first, for the collapsed box's liveness. `not_configured` is not a failure.
const BOX_RANK: Record<StackLiveness, number> = {
  unreachable: 4,
  unknown: 3,
  uncalibrated: 2,
  reachable: 1,
  not_configured: 0,
};

/**
 * The map to draw for this project. Graphban's own project gets today's map with no
 * self-box. Any other project gets the app first and Graphban as one collapsed box,
 * unless `expanded`, which draws the instance's detail in its lane. Expanding needs no
 * extra call: the instance detail is already in the same payload.
 */
export function prepareStackMap(topo: StackTopology, expanded = false): StackMapView {
  const owned = ownsGraphban(topo);
  const instance = topo.nodes.filter((n) => !isAppNode(n));
  const collapsible = !owned && instance.length > 0;
  const collapsed = collapsible && !expanded;
  const unmapped = isUnmapped(topo) && !owned;

  let nodes = topo.nodes;
  let edges = topo.edges;
  let hosts = topo.hosts;
  if (collapsed) {
    const app = topo.nodes.filter(isAppNode);
    const appIds = new Set(app.map((n) => n.id));
    // The instance's liveness is its core boxes (api, web, db on the compose host),
    // the worst of them: a failed probe reads the whole box unreachable or unknown.
    const core = instance.filter((n) => n.scope === "instance" && n.ring < 3);
    const pool = core.length ? core : instance;
    const liveness = pool.reduce<StackLiveness>(
      (worst, n) => (BOX_RANK[n.liveness] > BOX_RANK[worst] ? n.liveness : worst),
      "not_configured",
    );
    const box: StackNode = {
      id: GRAPHBAN_BOX,
      label: BUILT_WITH_GRAPHBAN,
      title: `${BUILT_WITH_GRAPHBAN} — the instance that builds this app`,
      kind: GRAPHBAN_KIND,
      ring: 2,
      scope: "instance",
      host: BUILD_LANE,
      plane: "graphban",
      liveness: pool.length ? liveness : "unknown",
      surfaces: [],
      extensions: [],
      members: instance.map((n) => n.id),
      facts: [
        { key: "version", value: topo.identity.api_version || "unknown", provenance: "identity.api_version" },
        { key: "git_sha", value: topo.identity.api_git_sha || "unknown", provenance: "identity.api_git_sha" },
        {
          key: "source",
          value:
            topo.graphban_source === "unknown"
              ? "unknown — this project has no code map to check"
              : "a tool this project runs; its source is not in this project's code map",
          provenance: "graphban_source",
        },
      ],
    };
    const builds: StackEdge[] = app
      .filter((n) => n.ring === 1 && n.kind !== PLACEHOLDER_KIND)
      .map((n) => ({ from: GRAPHBAN_BOX, to: n.id, protocol: "builds", label: "builds" }));
    nodes = [box, ...app];
    edges = [...builds, ...topo.edges.filter((e) => appIds.has(e.from) && appIds.has(e.to))];
    hosts = [
      { id: BUILD_LANE, label: "Build lane", nodes: [GRAPHBAN_BOX] },
      ...topo.hosts
        .map((h) => ({ ...h, nodes: h.nodes.filter((id) => appIds.has(id)) }))
        .filter((h) => h.nodes.length > 0),
    ];
  }

  if (unmapped && !nodes.some((n) => n.ring >= 3 && isAppNode(n))) {
    const placeholder: StackNode = {
      id: UNMAPPED_BOX,
      label: SERVICES_NOT_MAPPED,
      title: SERVICES_NOT_MAPPED,
      kind: PLACEHOLDER_KIND,
      ring: 3,
      scope: "project",
      host: "external",
      plane: "app",
      liveness: "unknown",
      surfaces: [],
      extensions: [],
      facts: [],
    };
    nodes = [...nodes, placeholder];
  }

  return {
    topo: { ...topo, nodes, edges, hosts },
    collapsible,
    collapsed,
    layoutOptions: {
      graphbanLeft: collapsible,
      ring3Subtitle: unmapped ? SERVICES_NOT_MAPPED : null,
    },
  };
}

// Ring labels as structured parts: a circled number, an uppercase ownership
// title, and a plain-language subtitle — matching docs/design/Stack.dc.html,
// where each ring header is a badge + title + gloss rather than one flat string.
const RING_LABEL: Record<number, { title: string; subtitle: string }> = {
  1: { title: "CODE WE WRITE", subtitle: "our deployables" },
  2: { title: "INFRASTRUCTURE WE RUN", subtitle: "we operate it, didn't write it" },
  3: { title: "EXTERNAL SERVICES", subtitle: "we only call them, over the network" },
};

// Host column order: the project's own app first (it is the code we write) with the
// infra it runs on beside it, then user machines, agent hosts and the compose/Railway
// stack. Anything unrecognised falls after. External hosts get their own column.
// On a project that does not write Graphban, Graphban is a build lane at the LEFT edge
// (GRPH-1099): the collapsed box, or the instance's own hosts when it is expanded.
function hostRank(id: string, graphbanLeft = false): number {
  const s = id.toLowerCase();
  if (s === BUILD_LANE) return -20;
  if (s === "project_app") return -2;
  if (s === "app_infra") return -1;
  if (graphbanLeft) return graphbanRank(s) - 10;
  return graphbanRank(s);
}

function graphbanRank(s: string): number {
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
  if (n.kind === GRAPHBAN_KIND) return { w: 250, h: 156 };
  if (n.kind === PLACEHOLDER_KIND) return { w: 220, h: 92 };
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
 * Which faces an edge leaves and enters. Decided from the box EXTENTS, not the
 * centre distance: when two boxes are separated along an axis (one wholly left of
 * the other, or wholly above), the edge uses that pair of opposing faces, so the
 * orthogonal route is a clean channel elbow in the box-free gap between them.
 *
 * Horizontal separation wins over vertical, because this layout is columns: the
 * external providers are a right-hand column, and an edge to one far down that
 * column must still leave the source's RIGHT and enter the target's LEFT. The old
 * centre-distance rule flipped to top/bottom once the vertical drop exceeded the
 * horizontal gap (e.g. api at the top of its column → laya at the bottom of the
 * external column), which sent the horizontal mid-segment slicing straight across
 * the boxes in between and dropped the entry through the box directly above the
 * target. Only when the boxes overlap on both axes do we fall back to the centre.
 */
function facesFor(s: Box, t: Box): { sSide: Side; tSide: Side } {
  if (t.x >= s.x + s.w) return { sSide: "right", tSide: "left" };
  if (s.x >= t.x + t.w) return { sSide: "left", tSide: "right" };
  if (t.y >= s.y + s.h) return { sSide: "bottom", tSide: "top" };
  if (s.y >= t.y + t.h) return { sSide: "top", tSide: "bottom" };
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

export interface StackLayoutOptions {
  /** Put Graphban's own hosts in a lane at the left edge, framed "Built with Graphban". */
  graphbanLeft?: boolean;
  /** Replaces ring 3's gloss, e.g. "services not mapped yet" before any record. */
  ring3Subtitle?: string | null;
}

export function buildStackLayout(topo: StackTopology, opts: StackLayoutOptions = {}): StackLayout {
  const graphbanLeft = !!opts.graphbanLeft;
  const byId: Record<string, StackNode> = Object.fromEntries(topo.nodes.map((n) => [n.id, n]));
  const boxes: Record<string, Box> = {};

  // Which nodes are external (ring 3) vs left/centre (ring 1-2).
  const externalIds = topo.nodes.filter((n) => n.ring >= 3).map((n) => n.id);
  const innerNodes = topo.nodes.filter((n) => n.ring < 3);

  // Host columns for the inner (ring 1-2) nodes, ordered by host rank.
  const innerHosts = Array.from(new Set(innerNodes.map((n) => n.host))).sort((a, b) => {
    const r = hostRank(a, graphbanLeft) - hostRank(b, graphbanLeft);
    return r !== 0 ? r : a.localeCompare(b);
  });

  // Within a column, stack by ring then the order the API returned — so the entry
  // deployable (e.g. web) stays above the backend above the infra it sits on, and a
  // deeper-ring box never lands between two boxes an edge runs between.
  const apiOrder = new Map(topo.nodes.map((n, i) => [n.id, i]));

  // Column x-positions for the inner hosts, left→right by rank (width spans all
  // of a host's nodes so a ring-2 box in the band below still lines up).
  const colGeom: Record<string, { x: number; w: number }> = {};
  let cursorX = PAD + 40;
  for (const host of innerHosts) {
    const colW = Math.max(
      ...innerNodes.filter((n) => n.host === host).map((n) => sizeOf(n).w),
    );
    colGeom[host] = { x: cursorX, w: colW };
    cursorX += colW + COL_GAP;
  }

  // Pass 1 — ring-1 ("code we write") nodes, stacked top-down in their column in
  // the order the API returned. Ring-2 nodes are deferred to a band BELOW the
  // ring-1 frame (pass 3) so an infra box (db) never straddles the code/infra ring
  // boundary — which it did when a sibling column (agent hosts) ran taller than it
  // and dragged the ring-1 bounding box down past db's top edge.
  const colCursorY: Record<string, number> = {};
  for (const host of innerHosts) {
    const { x, w } = colGeom[host];
    let y = TOP + 20;
    innerNodes
      .filter((n) => n.host === host && n.ring === 1)
      .sort((a, b) => (apiOrder.get(a.id) ?? 0) - (apiOrder.get(b.id) ?? 0))
      .forEach((n) => {
        const s = sizeOf(n);
        boxes[n.id] = { x: x + (w - s.w) / 2, y, w: s.w, h: s.h };
        y += s.h + V_GAP;
      });
    colCursorY[host] = y;
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

  // Pass 2 — the ring-1 frame's bottom edge (its boxes padded by the ring-1 pad),
  // so ring-2 nodes can be dropped clearly below it.
  const ring1BoxesNow = topo.nodes
    .filter((n) => n.ring === 1)
    .map((n) => boxes[n.id])
    .filter(Boolean);
  const ring1Bottom = ring1BoxesNow.length
    ? Math.max(...ring1BoxesNow.map((b) => b.y + b.h)) + 40
    : TOP + 20;

  // Pass 3 — ring-2 ("infrastructure we run") nodes in a band below the ring-1
  // frame, each in its own host column. db is infra we run (Postgres: we operate
  // it, didn't write it), so it belongs in the ring-2 band, not inside the lime
  // code ring. The host frame will still wrap it (a host legitimately spans the
  // code/infra line); the ownership ring must not.
  //
  // Only a column the ring-1 frame actually spans needs that drop. A column wholly
  // beside the frame starts at the top: when a project's own app is its own column and
  // every Graphban box is ring 2, dropping them all below the app made a tall staircase
  // with an empty quadrant beside it.
  const INFRA_GAP = 28;
  const ring1Span = ring1BoxesNow.length ? pad(bbox(ring1BoxesNow), 40) : null;
  const besideRing1 = (x: number, w: number) =>
    !ring1Span || x >= ring1Span.x + ring1Span.w || x + w <= ring1Span.x;
  //
  // The app's own infra (posted record, GRPH-1099) sits beside the first deployable that
  // uses it: ordered by that user's top, and never higher than it. A shared db lines up
  // with the first service that talks to it, and the rest stack below.
  const userTop = (id: string): number => {
    const tops = topo.edges
      .filter((e) => e.to === id && byId[e.from]?.ring === 1 && boxes[e.from])
      .map((e) => boxes[e.from].y);
    return tops.length ? Math.min(...tops) : Number.POSITIVE_INFINITY;
  };
  const ring2Nodes = innerNodes
    .filter((x) => x.ring === 2)
    .map((n, i) => ({ n, i, top: n.host === APP_INFRA ? userTop(n.id) : 0 }))
    .sort((a, b) => a.top - b.top || a.i - b.i)
    .map((x) => x.n);
  const r2CursorY: Record<string, number> = {};
  for (const n of ring2Nodes) {
    const { x, w } = colGeom[n.host] ?? { x: PAD + 40, w: sizeOf(n).w };
    const s = sizeOf(n);
    let floor = besideRing1(x, w) ? TOP + 20 : ring1Bottom + INFRA_GAP;
    if (n.host === APP_INFRA) {
      const top = userTop(n.id);
      if (Number.isFinite(top)) floor = Math.max(floor, top);
    }
    const start = Math.max(r2CursorY[n.host] ?? 0, floor, colCursorY[n.host] ?? 0);
    boxes[n.id] = { x: x + (w - s.w) / 2, y: start, w: s.w, h: s.h };
    r2CursorY[n.host] = start + s.h + V_GAP;
  }

  // Host frames — each host's dashed group spans ALL its nodes. A host like the
  // compose stack holds both code (api, web) and the infra it runs (db), so its
  // frame legitimately crosses the code/infra ring line.
  const columnHostFrames: HostFrame[] = [];
  for (const host of innerHosts) {
    const hostBoxes = innerNodes
      .filter((n) => n.host === host)
      .map((n) => boxes[n.id])
      .filter(Boolean);
    if (!hostBoxes.length) continue;
    const hl = pad(bbox(hostBoxes), 22);
    const label = topo.hosts.find((h) => h.id === host)?.label ?? host;
    columnHostFrames.push({ id: host, label, ...hl });
  }

  // The expanded instance keeps a "Built with Graphban" frame around its own hosts, so
  // expanding the collapsed box reads as opening it in place, not as a different map.
  let lane: HostFrame | null = null;
  if (graphbanLeft) {
    // Collapsed, the box is its own label; the frame is for the expanded instance only.
    const laneBoxes = columnHostFrames.filter((h) => h.id !== BUILD_LANE &&
      innerNodes.some((n) => n.host === h.id && !isAppNode(n)));
    if (laneBoxes.length) {
      lane = { id: BUILD_LANE, label: BUILT_WITH_GRAPHBAN, ...pad(bbox(laneBoxes), 22) };
    }
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
    rings.push({
      ring,
      num: ring,
      ...RING_LABEL[ring],
      ...(ring === 3 && opts.ring3Subtitle ? { subtitle: opts.ring3Subtitle } : {}),
      ...box,
    });
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

  return { width, height, boxes, rings, hostFrames: columnHostFrames, edges, lane, fitPoints };
}
