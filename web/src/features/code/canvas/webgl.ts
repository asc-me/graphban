/**
 * WebGL2 renderer (PRD-52 D5, S10). Primary path. Canvas 2D is the fallback.
 *
 * The scene already applied every D5 rule (LOD, the 6,000 cap, bundle offsets,
 * square-under-2.2px, DPR ≤ 2). This module only rasterises that scene:
 * instanced quads for nodes (shape in the fragment shader) and one instanced
 * draw for every edge. Per-node alpha and the batch edge alpha ride in the
 * instance buffer — there is no alpha texture to upload.
 *
 * A pan does not rebuild that buffer. Geometry is in world space, so the camera
 * is a uniform. The buffer is uploaded again only when the strokes, the node
 * set, or the zoom (past 8% of the bake) actually change. The bench's ±0.05
 * zoom step stays inside that window at both 1× and 2.6×.
 *
 * Z-order matches Canvas 2D: clouds, edges, glow, markers, nodes, rings.
 */

import { kindShape, shapeCode } from "../kindShape";
import type { Renderer } from "./renderer";
import { GLOW_CORE_ALPHA, GLOW_WASH_ALPHA, GLOW_WASH_PX, type DrawNode, type DrawScene } from "./scene";
import type { Stroke } from "./edges";
import { quadPoint } from "./edges";
import { presenceClouds, predictedStrokes, type Cloud } from "./clouds";
import { createCanvas2D } from "./canvas2d";

const STROKE = [10 / 255, 12 / 255, 14 / 255] as const;
const NODE_FLOATS = 9;
/** x0,y0,x1,y1, halfWidth, rgba, then the mitred offset at each end (n0, n1). */
export const LINE_FLOATS = 13;
/** Rebake when zoom drifts more than this fraction from the uploaded bake. */
const ZOOM_SLACK = 0.08;

export function parseHex(color: string): [number, number, number] {
  let h = color.trim();
  if (h.startsWith("#")) h = h.slice(1);
  if (h.length === 3) h = `${h[0]}${h[0]}${h[1]}${h[1]}${h[2]}${h[2]}`;
  const n = Number.parseInt(h.slice(0, 6), 16);
  if (!Number.isFinite(n) || h.length < 6) return [0.55, 0.58, 0.62];
  return [((n >> 16) & 255) / 255, ((n >> 8) & 255) / 255, (n & 255) / 255];
}

/** 0 file circle, 1 symbol diamond, 2 module ring, 3 square, 7 config hexagon. */
export function shapeKind(node: DrawNode): number {
  return shapeCode(kindShape(node.kind, node.square));
}

/** Quad half-extent in world units, including the stroke and one pixel of AA. */
export function nodeExtent(node: DrawNode, zoom: number, dpr: number): number {
  const k = Math.max(Math.abs(zoom), 0.05);
  const aa = 1 / Math.max(k * Math.max(dpr, 1), 0.001);
  const kind = shapeKind(node);
  if (kind === 3) return node.r + aa;
  const sw = (kind === 2 ? 2 : 1.25) / k;
  if (kind === 2) {
    const dot = Math.max(2.2, node.r * 0.36);
    return Math.max(node.r + sw / 2, dot) + aa;
  }
  return node.r + sw / 2 + aa;
}

export function quadSteps(stroke: Stroke, zoom: number): number {
  const span =
    Math.hypot(stroke.cx - stroke.x0, stroke.cy - stroke.y0) +
    Math.hypot(stroke.x1 - stroke.cx, stroke.y1 - stroke.cy);
  const screen = span * Math.max(Math.abs(zoom), 0.05);
  return Math.max(8, Math.min(48, Math.ceil(screen / 6)));
}

function pushSegment(
  out: number[],
  x0: number,
  y0: number,
  x1: number,
  y1: number,
  hw: number,
  zoom: number,
) {
  const ext = 0.5 / Math.max(Math.abs(zoom), 0.05);
  const len = Math.hypot(x1 - x0, y1 - y0) || 1;
  const ux = (x1 - x0) / len;
  const uy = (y1 - y0) / len;
  out.push(x0 - ux * ext, y0 - uy * ext, x1 + ux * ext, y1 + uy * ext, hw);
}

/** Append instanced line data (x0,y0,x1,y1,halfWidth) for one stroke. */
export function appendStroke(out: number[], stroke: Stroke, zoom: number): void {
  const hw = stroke.widthPx / Math.max(Math.abs(zoom), 0.05) / 2;
  if (stroke.kind === "line") {
    pushSegment(out, stroke.x0, stroke.y0, stroke.x1, stroke.y1, hw, zoom);
    return;
  }
  const steps = quadSteps(stroke, zoom);
  let prev = quadPoint(stroke, 0);
  for (let i = 1; i <= steps; i++) {
    const p = quadPoint(stroke, i / steps);
    pushSegment(out, prev.x, prev.y, p.x, p.y, hw, zoom);
    prev = p;
  }
}

export interface StrokeRun {
  color: string;
  /** Five floats per segment. */
  data: number[];
}

/** One run per colour, in first-seen order. The draw path flattens these into one call. */
export function groupStrokes(strokes: Stroke[], zoom: number): StrokeRun[] {
  const order: string[] = [];
  const by = new Map<string, number[]>();
  for (const stroke of strokes) {
    let data = by.get(stroke.color);
    if (!data) {
      data = [];
      by.set(stroke.color, data);
      order.push(stroke.color);
    }
    appendStroke(data, stroke, zoom);
  }
  return order.map((color) => ({ color, data: by.get(color)! }));
}

/**
 * One instanced buffer for every stroke: x0,y0,x1,y1,halfWidth,rgba.
 * Colour is per instance so the whole edge layer is a single draw.
 * `alpha` is the batch alpha (`scene.edgesAlpha`); it is not decided per edge.
 */
export function bakeLines(strokes: Stroke[], zoom: number, alpha: number): Float32Array {
  let count = 0;
  for (const s of strokes) count += s.kind === "line" ? 1 : quadSteps(s, zoom);
  const out = new Float32Array(count * LINE_FLOATS);
  if (count === 0) return out;
  const k = Math.max(Math.abs(zoom), 0.05);
  const colors = new Map<string, [number, number, number]>();
  const colorOf = (c: string): [number, number, number] => {
    let parsed = colors.get(c);
    if (!parsed) {
      parsed = parseHex(c);
      colors.set(c, parsed);
    }
    return parsed;
  };
  let o = 0;
  const write = (
    x0: number, y0: number, x1: number, y1: number, hw: number, rgb: [number, number, number],
    n0x: number, n0y: number, n1x: number, n1y: number,
  ) => {
    out[o++] = x0;
    out[o++] = y0;
    out[o++] = x1;
    out[o++] = y1;
    out[o++] = hw;
    out[o++] = rgb[0];
    out[o++] = rgb[1];
    out[o++] = rgb[2];
    out[o++] = alpha;
    out[o++] = n0x;
    out[o++] = n0y;
    out[o++] = n1x;
    out[o++] = n1y;
  };
  const xs: number[] = [];
  const ys: number[] = [];
  for (const stroke of strokes) {
    const rgb = colorOf(stroke.color);
    const hw = stroke.widthPx / k / 2;
    if (stroke.kind === "line") {
      const [nx, ny] = unitNormal(stroke.x1 - stroke.x0, stroke.y1 - stroke.y0);
      write(stroke.x0, stroke.y0, stroke.x1, stroke.y1, hw, rgb, nx, ny, nx, ny);
      continue;
    }
    const steps = quadSteps(stroke, zoom);
    xs.length = 0;
    ys.length = 0;
    for (let i = 0; i <= steps; i++) {
      const p = quadPoint(stroke, i / steps);
      xs.push(p.x);
      ys.push(p.y);
    }
    // One offset per point, shared by the two segments that meet there.
    const mx: number[] = [];
    const my: number[] = [];
    for (let i = 0; i <= steps; i++) {
      const [ax, ay] = i > 0 ? unitNormal(xs[i] - xs[i - 1], ys[i] - ys[i - 1]) : unitNormal(xs[1] - xs[0], ys[1] - ys[0]);
      const [bx, by] = i < steps ? unitNormal(xs[i + 1] - xs[i], ys[i + 1] - ys[i]) : [ax, ay];
      const [m, scale] = miter(ax, ay, bx, by);
      mx.push(m[0] * scale);
      my.push(m[1] * scale);
    }
    for (let i = 0; i < steps; i++) {
      write(xs[i], ys[i], xs[i + 1], ys[i + 1], hw, rgb, mx[i], my[i], mx[i + 1], my[i + 1]);
    }
  }
  return out;
}

function unitNormal(dx: number, dy: number): [number, number] {
  const len = Math.hypot(dx, dy);
  return len < 1e-9 ? [0, 1] : [-dy / len, dx / len];
}

/** The joint's offset direction, and how far along it the stroke edge sits (capped for sharp turns). */
function miter(ax: number, ay: number, bx: number, by: number): [[number, number], number] {
  let mx = ax + bx;
  let my = ay + by;
  const len = Math.hypot(mx, my);
  if (len < 1e-6) return [[ax, ay], 1];
  mx /= len;
  my /= len;
  const cos = mx * ax + my * ay;
  return [[mx, my], 1 / Math.max(cos, 0.5)];
}

/** Geometry identity. The camera is deliberately absent: a pan must not miss. */
export function geometrySig(strokes: Stroke[], alpha: number): number {
  let h = (strokes.length | 0) ^ Math.round(alpha * 10000);
  for (let i = 0; i < strokes.length; i++) {
    const s = strokes[i];
    h = Math.imul(h ^ ((s.x0 * 100) | 0), 16777619) ^ ((s.y0 * 100) | 0);
    h = Math.imul(h ^ ((s.x1 * 100) | 0), 16777619) ^ ((s.y1 * 100) | 0);
    h = Math.imul(h ^ ((s.cx * 100) | 0), 16777619) ^ ((s.widthPx * 100) | 0);
    h = Math.imul(h, 33) ^ s.color.charCodeAt(1) ^ (s.kind === "quad" ? 7 : 1);
  }
  return h;
}

export interface Dot {
  x: number;
  y: number;
  r: number;
  color: string;
}

/** Midpoint dots for bundles that collapsed to one stroke. */
export function markersOf(strokes: Stroke[], zoom: number): Dot[] {
  const k = Math.max(Math.abs(zoom), 0.05);
  const out: Dot[] = [];
  for (const s of strokes) {
    if (s.marker !== "double") continue;
    out.push({
      x: 0.25 * s.x0 + 0.5 * s.cx + 0.25 * s.x1,
      y: 0.25 * s.y0 + 0.5 * s.cy + 0.25 * s.y1,
      r: 3 / k,
      color: s.color,
    });
  }
  return out;
}

const NODE_VS = `#version 300 es
layout(location = 0) in vec2 a_corner;
layout(location = 1) in vec2 a_pos;
layout(location = 2) in float a_r;
layout(location = 3) in float a_extent;
layout(location = 4) in vec3 a_color;
layout(location = 5) in float a_kind;
layout(location = 6) in float a_alpha;
uniform vec3 u_cam;
uniform float u_dpr;
uniform vec2 u_view;
out vec2 v_local;
out vec3 v_color;
out float v_r;
out float v_kind;
out float v_alpha;
void main() {
  vec2 world = a_pos + a_corner * a_extent;
  v_local = a_corner * a_extent;
  v_color = a_color;
  v_r = a_r;
  v_kind = a_kind;
  v_alpha = a_alpha;
  vec2 css = world * u_cam.z + u_cam.xy;
  vec2 px = css * u_dpr;
  vec2 bit = max(u_view * u_dpr, vec2(1.0));
  gl_Position = vec4(px.x / bit.x * 2.0 - 1.0, 1.0 - px.y / bit.y * 2.0, 0.0, 1.0);
}`;

const NODE_FS = `#version 300 es
precision highp float;
in vec2 v_local;
in vec3 v_color;
in float v_r;
in float v_kind;
in float v_alpha;
uniform float u_k;
uniform float u_dpr;
out vec4 o;
float strokeMask(float dist, float halfW, float aa) {
  float w = max(halfW, aa);
  return 1.0 - smoothstep(w - aa, w + aa, dist);
}
// Flat-top hexagon. r is the apothem (center to a flat), so the caller
// passes circumradius * sqrt(3)/2 and the vertices land on the same circle
// as hexagonPoints in kindShape.ts.
float sdHexagon(vec2 p, float r) {
  vec3 k = vec3(-0.866025404, 0.5, 0.577350269);
  p = abs(p);
  p -= 2.0 * min(dot(k.xy, p), 0.0) * k.xy;
  p -= vec2(clamp(p.x, -k.z * r, k.z * r), r);
  return length(p) * sign(p.y);
}
void main() {
  float aa = 1.0 / max(abs(u_k) * max(u_dpr, 1.0), 0.0001);
  float swFile = 1.25 / max(abs(u_k), 0.05);
  float swModule = 2.0 / max(abs(u_k), 0.05);
  float swRing = 1.5 / max(abs(u_k), 0.05);
  int kind = int(v_kind + 0.5);
  vec3 strokeCol = vec3(${STROKE[0].toFixed(6)}, ${STROKE[1].toFixed(6)}, ${STROKE[2].toFixed(6)});
  float d = length(v_local);
  float cov = 0.0;
  vec3 col = v_color;
  if (kind == 3) {
    cov = smoothstep(0.0, aa, min(v_r - abs(v_local.x), v_r - abs(v_local.y)));
  } else if (kind == 1) {
    float diamond = abs(v_local.x) + abs(v_local.y);
    float inside = 1.0 - smoothstep(v_r - aa, v_r + aa, diamond);
    float strokeA = strokeMask(abs(diamond - v_r), swFile * 0.5, aa);
    col = mix(v_color, strokeCol, clamp(strokeA, 0.0, 1.0));
    cov = max(inside, strokeA);
  } else if (kind == 2) {
    float strokeA = strokeMask(abs(d - v_r), swModule * 0.5, aa);
    float dotR = max(2.2, v_r * 0.36);
    float dotA = 1.0 - smoothstep(dotR - aa, dotR + aa, d);
    cov = max(strokeA, dotA);
  } else if (kind == 4) {
    cov = strokeMask(abs(d - v_r), swRing * 0.5, aa);
  } else if (kind == 5) {
    cov = 1.0 - smoothstep(v_r - aa, v_r + aa, d);
  } else if (kind == 6) {
    float t = d / max(v_r, 0.0001);
    cov = clamp(1.0 - t, 0.0, 1.0);
  } else if (kind == 7) {
    float sd = sdHexagon(v_local, v_r * 0.866025404);
    float inside = 1.0 - smoothstep(-aa, aa, sd);
    float strokeA = strokeMask(abs(sd), swFile * 0.5, aa);
    col = mix(v_color, strokeCol, clamp(strokeA, 0.0, 1.0));
    cov = max(inside, strokeA);
  } else {
    float inside = 1.0 - smoothstep(v_r - aa, v_r + aa, d);
    float strokeA = strokeMask(abs(d - v_r), swFile * 0.5, aa);
    col = mix(v_color, strokeCol, clamp(strokeA, 0.0, 1.0));
    cov = max(inside, strokeA);
  }
  if (cov < 0.02) discard;
  // Premultiplied. Straight SRC_ALPHA onto a cleared buffer stores a*a, so a
  // 0.16 cloud lands under the ink threshold and never shows.
  float a = cov * v_alpha;
  o = vec4(col * a, a);
}`;

const LINE_VS = `#version 300 es
layout(location = 0) in vec2 a_corner;
layout(location = 1) in vec2 a_p0;
layout(location = 2) in vec2 a_p1;
layout(location = 3) in float a_hw;
layout(location = 4) in vec4 a_color;
layout(location = 5) in vec2 a_n0;
layout(location = 6) in vec2 a_n1;
uniform vec3 u_cam;
uniform float u_dpr;
uniform vec2 u_view;
out vec4 v_color;
out float v_across;
out float v_hwpx;
void main() {
  v_color = a_color;
  // The quad grows one device pixel past the stroke so the fragment shader has
  // room to feather it. A line under a pixel wide is drawn at one pixel and
  // faded by its width instead, so hairlines do not shimmer as they thin.
  float pxPerWorld = max(abs(u_cam.z) * u_dpr, 1e-6);
  v_hwpx = a_hw * pxPerWorld;
  float outer = max(v_hwpx, 0.5) + 1.0;
  // Each end carries the mitred offset it shares with the next segment, so a
  // curve's segments meet edge to edge instead of overlapping at the joints.
  vec2 n = mix(a_n0, a_n1, a_corner.x);
  vec2 world = mix(a_p0, a_p1, a_corner.x) + n * (a_corner.y * outer / pxPerWorld);
  v_across = a_corner.y * outer;
  vec2 css = world * u_cam.z + u_cam.xy;
  vec2 px = css * u_dpr;
  vec2 bit = max(u_view * u_dpr, vec2(1.0));
  gl_Position = vec4(px.x / bit.x * 2.0 - 1.0, 1.0 - px.y / bit.y * 2.0, 0.0, 1.0);
}`;

const LINE_FS = `#version 300 es
precision highp float;
in vec4 v_color;
in float v_across;
in float v_hwpx;
out vec4 o;
void main() {
  float h = max(v_hwpx, 0.5);
  float cov = clamp(h + 0.5 - abs(v_across), 0.0, 1.0) * min(1.0, v_hwpx * 2.0);
  float a = v_color.a * cov;
  o = vec4(v_color.rgb * a, a);
}`;

function compile(gl: WebGL2RenderingContext, type: number, source: string): WebGLShader | null {
  const shader = gl.createShader(type);
  if (!shader) return null;
  gl.shaderSource(shader, source);
  gl.compileShader(shader);
  if (!gl.getShaderParameter(shader, gl.COMPILE_STATUS)) {
    console.warn(gl.getShaderInfoLog(shader));
    gl.deleteShader(shader);
    return null;
  }
  return shader;
}

function link(gl: WebGL2RenderingContext, vs: string, fs: string): WebGLProgram | null {
  const v = compile(gl, gl.VERTEX_SHADER, vs);
  const f = compile(gl, gl.FRAGMENT_SHADER, fs);
  if (!v || !f) return null;
  const program = gl.createProgram();
  if (!program) return null;
  gl.attachShader(program, v);
  gl.attachShader(program, f);
  gl.linkProgram(program);
  gl.deleteShader(v);
  gl.deleteShader(f);
  if (!gl.getProgramParameter(program, gl.LINK_STATUS)) {
    console.warn(gl.getProgramInfoLog(program));
    gl.deleteProgram(program);
    return null;
  }
  return program;
}

const NODE_CORNERS = new Float32Array([
  -1, -1, 1, -1, 1, 1,
  -1, -1, 1, 1, -1, 1,
]);

const LINE_CORNERS = new Float32Array([
  0, -1, 0, 1, 1, 1,
  0, -1, 1, 1, 1, -1,
]);

function bindDivisor(
  gl: WebGL2RenderingContext,
  index: number,
  size: number,
  stride: number,
  offset: number,
) {
  gl.enableVertexAttribArray(index);
  gl.vertexAttribPointer(index, size, gl.FLOAT, false, stride, offset);
  gl.vertexAttribDivisor(index, 1);
}

interface Prog {
  program: WebGLProgram;
  vao: WebGLVertexArrayObject;
  buf: WebGLBuffer;
  /** Shared corner quad. One per program, not per slot. */
  corners: WebGLBuffer;
  cam: WebGLUniformLocation | null;
  dpr: WebGLUniformLocation | null;
  view: WebGLUniformLocation | null;
  /**
   * Another VAO and instance buffer for this same program.
   * Slots cannot share one buffer: the last upload would be what every earlier
   * slot draws on a pan, and the contention pulse rewrites only the ring.
   */
  fork: () => Prog | null;
}

const NODE_ATTRS = [
  { index: 1, size: 2, offset: 0 },
  { index: 2, size: 1, offset: 8 },
  { index: 3, size: 1, offset: 12 },
  { index: 4, size: 3, offset: 16 },
  { index: 5, size: 1, offset: 28 },
  { index: 6, size: 1, offset: 32 },
] as const;

const LINE_ATTRS = [
  { index: 1, size: 2, offset: 0 },
  { index: 2, size: 2, offset: 8 },
  { index: 3, size: 1, offset: 16 },
  { index: 4, size: 4, offset: 20 },
  { index: 5, size: 2, offset: 36 },
  { index: 6, size: 2, offset: 44 },
] as const;

function bindInstanceGeom(
  gl: WebGL2RenderingContext,
  corners: WebGLBuffer,
  strideFloats: number,
  attrs: readonly { index: number; size: number; offset: number }[],
): { vao: WebGLVertexArrayObject; buf: WebGLBuffer } | null {
  const vao = gl.createVertexArray();
  const buf = gl.createBuffer();
  if (!vao || !buf) return null;
  gl.bindVertexArray(vao);
  gl.bindBuffer(gl.ARRAY_BUFFER, corners);
  gl.enableVertexAttribArray(0);
  gl.vertexAttribPointer(0, 2, gl.FLOAT, false, 0, 0);
  gl.vertexAttribDivisor(0, 0);
  gl.bindBuffer(gl.ARRAY_BUFFER, buf);
  const stride = strideFloats * 4;
  for (const attr of attrs) bindDivisor(gl, attr.index, attr.size, stride, attr.offset);
  gl.bindVertexArray(null);
  return { vao, buf };
}

function makeNodeProg(gl: WebGL2RenderingContext): (Prog & { k: WebGLUniformLocation | null }) | null {
  const program = link(gl, NODE_VS, NODE_FS);
  if (!program) return null;
  const corners = gl.createBuffer();
  if (!corners) return null;
  gl.bindBuffer(gl.ARRAY_BUFFER, corners);
  gl.bufferData(gl.ARRAY_BUFFER, NODE_CORNERS, gl.STATIC_DRAW);
  const geom = () => bindInstanceGeom(gl, corners, NODE_FLOATS, NODE_ATTRS);
  const first = geom();
  if (!first) return null;
  const base = {
    program,
    corners,
    cam: gl.getUniformLocation(program, "u_cam"),
    dpr: gl.getUniformLocation(program, "u_dpr"),
    view: gl.getUniformLocation(program, "u_view"),
    k: gl.getUniformLocation(program, "u_k"),
  };
  const pack = (g: { vao: WebGLVertexArrayObject; buf: WebGLBuffer }): Prog & { k: WebGLUniformLocation | null } => ({
    ...base,
    vao: g.vao,
    buf: g.buf,
    fork() {
      const next = geom();
      return next ? pack(next) : null;
    },
  });
  return pack(first);
}

function makeLineProg(gl: WebGL2RenderingContext): Prog | null {
  const program = link(gl, LINE_VS, LINE_FS);
  if (!program) return null;
  const corners = gl.createBuffer();
  if (!corners) return null;
  gl.bindBuffer(gl.ARRAY_BUFFER, corners);
  gl.bufferData(gl.ARRAY_BUFFER, LINE_CORNERS, gl.STATIC_DRAW);
  const geom = () => bindInstanceGeom(gl, corners, LINE_FLOATS, LINE_ATTRS);
  const first = geom();
  if (!first) return null;
  const base = {
    program,
    corners,
    cam: gl.getUniformLocation(program, "u_cam"),
    dpr: gl.getUniformLocation(program, "u_dpr"),
    view: gl.getUniformLocation(program, "u_view"),
  };
  const pack = (g: { vao: WebGLVertexArrayObject; buf: WebGLBuffer }): Prog => ({
    ...base,
    vao: g.vao,
    buf: g.buf,
    fork() {
      const next = geom();
      return next ? pack(next) : null;
    },
  });
  return pack(first);
}

function setFrame(gl: WebGL2RenderingContext, prog: Prog, scene: DrawScene) {
  gl.useProgram(prog.program);
  gl.uniform3f(prog.cam, scene.camera.x, scene.camera.y, scene.camera.k);
  gl.uniform1f(prog.dpr, scene.dpr);
  gl.uniform2f(prog.view, scene.width, scene.height);
}

interface Bake {
  sig: number;
  zoom: number;
  dpr: number;
  data: Float32Array;
}

function zoomClose(baked: number, next: number): boolean {
  const scale = Math.max(Math.abs(baked), 0.05);
  return Math.abs(next - baked) / scale <= ZOOM_SLACK;
}

function sameBake(bake: Bake | null, sig: number, zoom: number, dpr: number): boolean {
  return !!bake && bake.sig === sig && bake.dpr === dpr && zoomClose(bake.zoom, zoom);
}

function drawBake(
  gl: WebGL2RenderingContext,
  prog: Prog,
  bake: Bake,
  floatsPer: number,
  upload: boolean,
) {
  const count = bake.data.length / floatsPer;
  if (count < 1) return;
  gl.bindVertexArray(prog.vao);
  if (upload) {
    gl.bindBuffer(gl.ARRAY_BUFFER, prog.buf);
    gl.bufferData(gl.ARRAY_BUFFER, bake.data, gl.DYNAMIC_DRAW);
  }
  gl.drawArraysInstanced(gl.TRIANGLES, 0, 6, count);
  gl.bindVertexArray(null);
}

function pushNode(
  out: Float32Array,
  o: number,
  x: number,
  y: number,
  r: number,
  extent: number,
  color: string,
  kind: number,
  alpha: number,
): number {
  const [cr, cg, cb] = parseHex(color);
  out[o++] = x;
  out[o++] = y;
  out[o++] = r;
  out[o++] = extent;
  out[o++] = cr;
  out[o++] = cg;
  out[o++] = cb;
  out[o++] = kind;
  out[o++] = alpha;
  return o;
}

function nodeSig(nodes: DrawNode[]): number {
  let h = nodes.length | 0;
  for (let i = 0; i < nodes.length; i++) {
    const n = nodes[i];
    h = Math.imul(h ^ ((n.x * 100) | 0), 16777619) ^ ((n.y * 100) | 0);
    h = Math.imul(h ^ ((n.r * 100) | 0), 33) ^ Math.round(n.alpha * 1000) ^ (n.square ? 3 : 1);
    h = Math.imul(h, 33) ^ n.color.charCodeAt(1) ^ n.kind.charCodeAt(0);
  }
  return h;
}

/** Positions and colour only. Radius is 3/zoom, so the zoom slack covers it. */
function dotSigOf(dots: Dot[]): number {
  let h = dots.length | 0;
  for (let i = 0; i < dots.length; i++) {
    const d = dots[i];
    h = Math.imul(h ^ ((d.x * 100) | 0), 16777619) ^ ((d.y * 100) | 0);
    h = Math.imul(h, 33) ^ d.color.charCodeAt(1);
  }
  return h;
}

function ringSig(rings: DrawScene["rings"]): number {
  let h = rings.length | 0;
  for (let i = 0; i < rings.length; i++) {
    const r = rings[i];
    h = Math.imul(h ^ ((r.x * 100) | 0), 16777619) ^ ((r.y * 100) | 0);
    h = Math.imul(h ^ ((r.r * 100) | 0), 33) ^ r.kind.charCodeAt(0) ^ r.color.charCodeAt(1);
    h = Math.imul(h, 33) ^ Math.round((r.alpha ?? 0.9) * 1000);
  }
  return h;
}

/** Identity of the cloud layer. A solo or a zoom that changes the blobs misses. */
export function cloudSig(clouds: readonly Cloud[]): number {
  let h = clouds.length | 0;
  for (let i = 0; i < clouds.length; i++) {
    const c = clouds[i];
    h = Math.imul(h ^ ((c.x * 100) | 0), 16777619) ^ ((c.y * 100) | 0);
    h = Math.imul(h ^ ((c.r * 100) | 0), 33) ^ Math.round(c.alpha * 1000);
    h = Math.imul(h, 33) ^ c.color.charCodeAt(1) ^ (c.predicted ? 5 : 1);
  }
  return h;
}

/** Mean per-channel delta of a coarse grid. Identical frames score 0. */
export function diffBitmaps(
  a: Uint8ClampedArray,
  b: Uint8ClampedArray,
  width: number,
  height: number,
): { blockMae: number; inkA: number; inkB: number } {
  let inkA = 0;
  let inkB = 0;
  const n = Math.min(a.length, b.length);
  for (let i = 3; i < n; i += 4) {
    if (a[i] > 20) inkA += 1;
    if (b[i] > 20) inkB += 1;
  }
  const cols = 24;
  const rows = 16;
  const bw = Math.max(1, Math.floor(width / cols));
  const bh = Math.max(1, Math.floor(height / rows));
  const stride = width * 4;
  let acc = 0;
  let blocks = 0;
  for (let by = 0; by < rows; by++) {
    for (let bx = 0; bx < cols; bx++) {
      let delta = 0;
      let samples = 0;
      const x0 = bx * bw;
      const y0 = by * bh;
      for (let y = y0; y < y0 + bh && y < height; y++) {
        for (let x = x0; x < x0 + bw && x < width; x++) {
          const i = y * stride + x * 4;
          delta += Math.abs(a[i] - b[i]) + Math.abs(a[i + 1] - b[i + 1]) + Math.abs(a[i + 2] - b[i + 2]) + Math.abs(a[i + 3] - b[i + 3]);
          samples += 1;
        }
      }
      if (samples > 0) {
        acc += delta / samples / 4;
        blocks += 1;
      }
    }
  }
  return { blockMae: blocks ? acc / blocks : 0, inkA, inkB };
}

/**
 * Draw `scene` on the 2D canvas and hide the lost WebGL canvas before the
 * browser presents the cleared buffer. Returns the fallback, or null when
 * 2D is missing too.
 */
export function swapToCanvas2D(
  glCanvas: HTMLCanvasElement,
  d2: HTMLCanvasElement,
  scene: DrawScene | null,
): Renderer | null {
  const fallback = createCanvas2D(d2);
  if (fallback && scene) fallback.draw(scene);
  glCanvas.hidden = true;
  d2.hidden = !fallback;
  return fallback;
}

export function createWebGL(
  canvas: HTMLCanvasElement,
  opts?: { onLost?: () => void },
): Renderer | null {
  let gl: WebGL2RenderingContext | null = null;
  try {
    // antialias stays off. On a GPU-less CI runner WebGL is software-rendered,
    // and MSAA of 6,000 edge quads is what pushed dense pan past 16ms.
    gl = canvas.getContext("webgl2", {
      alpha: true,
      antialias: false,
      depth: false,
      stencil: false,
      // The shaders write premultiplied colour. The page compositor must
      // treat the buffer the same way, or a translucent cloud is darkened twice.
      premultipliedAlpha: true,
      preserveDrawingBuffer: false,
    });
  } catch {
    return null;
  }
  if (!gl) return null;
  const nodes = makeNodeProg(gl);
  const lines = makeLineProg(gl);
  if (!nodes || !lines) return null;
  // Clouds, dots, nodes and rings share the node program. Edges, dashes, the
  // halo and the glow share the line program. Each slot still needs its own
  // instance buffer: a hit on pan skips the upload, and the buffer would
  // otherwise still hold whichever slot drew last.
  const cloudProg = nodes.fork();
  const dotProg = nodes.fork();
  const ringProg = nodes.fork();
  const dashProg = lines.fork();
  const glowProg = lines.fork();
  const haloProg = lines.fork();
  if (!cloudProg || !dotProg || !ringProg || !dashProg || !glowProg || !haloProg) return null;
  gl.enable(gl.BLEND);
  gl.blendFunc(gl.ONE, gl.ONE_MINUS_SRC_ALPHA);
  gl.disable(gl.DEPTH_TEST);
  let dead = false;
  let lineBake: Bake | null = null;
  let nodeBake: Bake | null = null;
  let dotBake: Bake | null = null;
  let ringBake: Bake | null = null;
  let glowBake: Bake | null = null;
  let haloBake: Bake | null = null;
  let cloudBake: Bake | null = null;
  let dashBake: Bake | null = null;

  const onLost = () => {
    if (dead) return;
    dead = true;
    opts?.onLost?.();
  };
  canvas.addEventListener("webglcontextlost", onLost);

  const paintLines = (scene: DrawScene, strokes: Stroke[], alpha: number, slot: "lines" | "glow" | "halo" | "dash") => {
    const zoom = scene.camera.k;
    const sig = geometrySig(strokes, alpha);
    const current = slot === "lines" ? lineBake : slot === "glow" ? glowBake : slot === "dash" ? dashBake : haloBake;
    // Captured before the assign. A hit means this frame must not bufferData.
    const hit = sameBake(current, sig, zoom, scene.dpr);
    const bake: Bake = hit && current
      ? current
      : { sig, zoom, dpr: scene.dpr, data: bakeLines(strokes, zoom, alpha) };
    if (slot === "lines") lineBake = bake;
    else if (slot === "glow") glowBake = bake;
    else if (slot === "dash") dashBake = bake;
    else haloBake = bake;
    if (bake.data.length === 0) return;
    const prog = slot === "lines" ? lines : slot === "glow" ? glowProg : slot === "dash" ? dashProg : haloProg;
    setFrame(gl!, prog, scene);
    drawBake(gl!, prog, bake, LINE_FLOATS, !hit);
  };

  const paintNodes = (
    scene: DrawScene,
    sig: number,
    slot: "nodes" | "dots" | "rings" | "clouds",
    build: () => Float32Array,
  ) => {
    const zoom = scene.camera.k;
    const current = slot === "nodes" ? nodeBake : slot === "dots" ? dotBake : slot === "clouds" ? cloudBake : ringBake;
    const hit = sameBake(current, sig, zoom, scene.dpr);
    const bake: Bake = hit && current ? current : { sig, zoom, dpr: scene.dpr, data: build() };
    if (slot === "nodes") nodeBake = bake;
    else if (slot === "dots") dotBake = bake;
    else if (slot === "clouds") cloudBake = bake;
    else ringBake = bake;
    if (bake.data.length === 0) return;
    const prog = slot === "nodes" ? nodes : slot === "dots" ? dotProg : slot === "clouds" ? cloudProg : ringProg;
    setFrame(gl!, prog, scene);
    gl!.uniform1f(nodes.k, zoom);
    drawBake(gl!, prog, bake, NODE_FLOATS, !hit);
  };

  const api: Renderer = {
    kind: "webgl",
    draw(scene: DrawScene) {
      if (dead || gl!.isContextLost()) return;
      const pxW = Math.max(1, Math.round(scene.width * scene.dpr));
      const pxH = Math.max(1, Math.round(scene.height * scene.dpr));
      if (canvas.width !== pxW) canvas.width = pxW;
      if (canvas.height !== pxH) canvas.height = pxH;
      gl!.viewport(0, 0, canvas.width, canvas.height);
      gl!.clearColor(0, 0, 0, 0);
      gl!.clear(gl!.COLOR_BUFFER_BIT);

      // Bottom layer. Same list Canvas 2D fills, so the pixel diff sees both.
      const clouds = presenceClouds(scene);
      const cloudPad = 1 / Math.max(Math.abs(scene.camera.k) * scene.dpr, 0.001);
      paintNodes(scene, cloudSig(clouds), "clouds", () => {
        const data = new Float32Array(clouds.length * NODE_FLOATS);
        let o = 0;
        for (const cloud of clouds) {
          o = pushNode(data, o, cloud.x, cloud.y, cloud.r, cloud.r + cloudPad, cloud.color, 6, cloud.alpha);
        }
        return data;
      });
      const dashes = predictedStrokes(clouds);
      if (dashes.length) paintLines(scene, dashes, 0.45, "dash");

      const zoom = scene.camera.k;
      const aa = 1 / Math.max(Math.abs(zoom) * scene.dpr, 0.001);
      paintLines(scene, scene.strokes, scene.edgesAlpha, "lines");

      // Build.dc.html draws the focus glow after the edges and before the nodes, so a
      // node covers the ends of its own glow instead of being crossed by it.
      if (scene.glow.length) {
        const fade = scene.focusMix ?? 1;
        const wash: Stroke[] = scene.glow.map((g) => ({ ...g, widthPx: GLOW_WASH_PX }));
        paintLines(scene, wash, GLOW_WASH_ALPHA * fade, "halo");
        paintLines(scene, scene.glow, GLOW_CORE_ALPHA * fade, "glow");
      }

      const dots = markersOf(scene.strokes, zoom);
      paintNodes(scene, dotSigOf(dots), "dots", () => {
        const data = new Float32Array(dots.length * NODE_FLOATS);
        let o = 0;
        for (const dot of dots) o = pushNode(data, o, dot.x, dot.y, dot.r, dot.r + aa, dot.color, 5, 1);
        return data;
      });

      paintNodes(scene, nodeSig(scene.nodes), "nodes", () => {
        const data = new Float32Array(scene.nodes.length * NODE_FLOATS);
        let o = 0;
        for (const node of scene.nodes) {
          o = pushNode(
            data,
            o,
            node.x,
            node.y,
            node.r,
            nodeExtent(node, zoom, scene.dpr),
            node.color,
            shapeKind(node),
            node.alpha,
          );
        }
        return data;
      });

      const order = ["contention", "pin", "selection"] as const;
      const ordered = order.flatMap((kind) => scene.rings.filter((ring) => ring.kind === kind));
      const ringPad = 1.5 / Math.max(Math.abs(zoom), 0.05) / 2 + aa;
      paintNodes(scene, ringSig(ordered), "rings", () => {
        const data = new Float32Array(ordered.length * NODE_FLOATS);
        let o = 0;
        for (const ring of ordered) {
          o = pushNode(data, o, ring.x, ring.y, ring.r, ring.r + ringPad, ring.color, 4, ring.alpha ?? 0.9);
        }
        return data;
      });

    },
    destroy() {
      dead = true;
      canvas.removeEventListener("webglcontextlost", onLost);
      gl!.deleteProgram(nodes.program);
      gl!.deleteProgram(lines.program);
      gl!.deleteBuffer(nodes.corners);
      gl!.deleteBuffer(lines.corners);
      for (const slot of [nodes, cloudProg, dotProg, ringProg, lines, dashProg, glowProg, haloProg]) {
        gl!.deleteBuffer(slot.buf);
        gl!.deleteVertexArray(slot.vao);
      }
    },
  };
  return api;
}
