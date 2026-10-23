/**
 * WebGL2 renderer (PRD-52 D5, S10). Primary path. Canvas 2D is the fallback.
 *
 * The scene already applied every D5 rule (LOD, the 6,000 cap, bundle offsets,
 * square-under-2.2px, DPR ≤ 2). This module only rasterises that scene:
 * instanced quads for nodes (shape in the fragment shader) and instanced line
 * segments batched per colour. Per-node alpha from `writeAlpha` is uploaded
 * as an instance attribute — the culled subset of that buffer, not a second copy.
 *
 * Z-order matches Canvas 2D: clouds, edges, markers, nodes, rings, glow.
 */

import type { Renderer } from "./renderer";
import type { DrawNode, DrawScene } from "./scene";
import type { Stroke } from "./edges";
import { quadPoint } from "./edges";
import { presenceClouds } from "./clouds";
import { createCanvas2D } from "./canvas2d";

const STROKE = [10 / 255, 12 / 255, 14 / 255] as const;
const GLOW = [198 / 255, 242 / 255, 78 / 255] as const;
const NODE_FLOATS = 9;
const LINE_FLOATS = 5;

export function parseHex(color: string): [number, number, number] {
  let h = color.trim();
  if (h.startsWith("#")) h = h.slice(1);
  if (h.length === 3) h = `${h[0]}${h[0]}${h[1]}${h[1]}${h[2]}${h[2]}`;
  const n = Number.parseInt(h.slice(0, 6), 16);
  if (!Number.isFinite(n) || h.length < 6) return [0.55, 0.58, 0.62];
  return [((n >> 16) & 255) / 255, ((n >> 8) & 255) / 255, (n & 255) / 255];
}

/** 0 file circle, 1 symbol diamond, 2 module ring, 3 square. */
export function shapeKind(node: DrawNode): number {
  if (node.square || node.kind === "doc" || node.kind === "config") return 3;
  if (node.kind === "symbol") return 1;
  if (node.kind === "module") return 2;
  return 0;
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
  // Half a CSS pixel of overlap so tessellated joints do not gap.
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

/** One run per colour, in first-seen order. Drawing is one instanced call per run. */
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
  } else {
    float inside = 1.0 - smoothstep(v_r - aa, v_r + aa, d);
    float strokeA = strokeMask(abs(d - v_r), swFile * 0.5, aa);
    col = mix(v_color, strokeCol, clamp(strokeA, 0.0, 1.0));
    cov = max(inside, strokeA);
  }
  if (cov < 0.02) discard;
  o = vec4(col, cov * v_alpha);
}`;

const LINE_VS = `#version 300 es
layout(location = 0) in vec2 a_corner;
layout(location = 1) in vec2 a_p0;
layout(location = 2) in vec2 a_p1;
layout(location = 3) in float a_hw;
uniform vec3 u_cam;
uniform float u_dpr;
uniform vec2 u_view;
void main() {
  vec2 dir = a_p1 - a_p0;
  float len = length(dir);
  vec2 n = len < 1e-5 ? vec2(0.0, 1.0) : vec2(-dir.y, dir.x) / len;
  vec2 world = mix(a_p0, a_p1, a_corner.x) + n * (a_corner.y * a_hw);
  vec2 css = world * u_cam.z + u_cam.xy;
  vec2 px = css * u_dpr;
  vec2 bit = max(u_view * u_dpr, vec2(1.0));
  gl_Position = vec4(px.x / bit.x * 2.0 - 1.0, 1.0 - px.y / bit.y * 2.0, 0.0, 1.0);
}`;

const LINE_FS = `#version 300 es
precision highp float;
uniform vec4 u_color;
out vec4 o;
void main() { o = u_color; }`;

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
  cam: WebGLUniformLocation | null;
  dpr: WebGLUniformLocation | null;
  view: WebGLUniformLocation | null;
}

function makeNodeProg(gl: WebGL2RenderingContext): (Prog & { k: WebGLUniformLocation | null }) | null {
  const program = link(gl, NODE_VS, NODE_FS);
  if (!program) return null;
  const vao = gl.createVertexArray();
  const buf = gl.createBuffer();
  const corners = gl.createBuffer();
  if (!vao || !buf || !corners) return null;
  gl.bindVertexArray(vao);
  gl.bindBuffer(gl.ARRAY_BUFFER, corners);
  gl.bufferData(gl.ARRAY_BUFFER, NODE_CORNERS, gl.STATIC_DRAW);
  gl.enableVertexAttribArray(0);
  gl.vertexAttribPointer(0, 2, gl.FLOAT, false, 0, 0);
  gl.vertexAttribDivisor(0, 0);
  gl.bindBuffer(gl.ARRAY_BUFFER, buf);
  const stride = NODE_FLOATS * 4;
  bindDivisor(gl, 1, 2, stride, 0);
  bindDivisor(gl, 2, 1, stride, 8);
  bindDivisor(gl, 3, 1, stride, 12);
  bindDivisor(gl, 4, 3, stride, 16);
  bindDivisor(gl, 5, 1, stride, 28);
  bindDivisor(gl, 6, 1, stride, 32);
  gl.bindVertexArray(null);
  return {
    program,
    vao,
    buf,
    cam: gl.getUniformLocation(program, "u_cam"),
    dpr: gl.getUniformLocation(program, "u_dpr"),
    view: gl.getUniformLocation(program, "u_view"),
    k: gl.getUniformLocation(program, "u_k"),
  };
}

function makeLineProg(gl: WebGL2RenderingContext): (Prog & { color: WebGLUniformLocation | null }) | null {
  const program = link(gl, LINE_VS, LINE_FS);
  if (!program) return null;
  const vao = gl.createVertexArray();
  const buf = gl.createBuffer();
  const corners = gl.createBuffer();
  if (!vao || !buf || !corners) return null;
  gl.bindVertexArray(vao);
  gl.bindBuffer(gl.ARRAY_BUFFER, corners);
  gl.bufferData(gl.ARRAY_BUFFER, LINE_CORNERS, gl.STATIC_DRAW);
  gl.enableVertexAttribArray(0);
  gl.vertexAttribPointer(0, 2, gl.FLOAT, false, 0, 0);
  gl.vertexAttribDivisor(0, 0);
  gl.bindBuffer(gl.ARRAY_BUFFER, buf);
  const stride = LINE_FLOATS * 4;
  bindDivisor(gl, 1, 2, stride, 0);
  bindDivisor(gl, 2, 2, stride, 8);
  bindDivisor(gl, 3, 1, stride, 16);
  gl.bindVertexArray(null);
  return {
    program,
    vao,
    buf,
    cam: gl.getUniformLocation(program, "u_cam"),
    dpr: gl.getUniformLocation(program, "u_dpr"),
    view: gl.getUniformLocation(program, "u_view"),
    color: gl.getUniformLocation(program, "u_color"),
  };
}

function setFrame(
  gl: WebGL2RenderingContext,
  prog: Prog,
  scene: DrawScene,
) {
  gl.useProgram(prog.program);
  gl.uniform3f(prog.cam, scene.camera.x, scene.camera.y, scene.camera.k);
  gl.uniform1f(prog.dpr, scene.dpr);
  gl.uniform2f(prog.view, scene.width, scene.height);
}

function drawInstances(
  gl: WebGL2RenderingContext,
  prog: Prog,
  data: Float32Array,
  floatsPer: number,
) {
  const count = data.length / floatsPer;
  if (count < 1) return;
  gl.bindVertexArray(prog.vao);
  gl.bindBuffer(gl.ARRAY_BUFFER, prog.buf);
  gl.bufferData(gl.ARRAY_BUFFER, data, gl.DYNAMIC_DRAW);
  gl.drawArraysInstanced(gl.TRIANGLES, 0, 6, count);
  gl.bindVertexArray(null);
}

function pushNode(
  out: number[],
  x: number,
  y: number,
  r: number,
  extent: number,
  color: string,
  kind: number,
  alpha: number,
) {
  const [cr, cg, cb] = parseHex(color);
  out.push(x, y, r, extent, cr, cg, cb, kind, alpha);
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
    gl = canvas.getContext("webgl2", {
      alpha: true,
      antialias: true,
      depth: false,
      stencil: false,
      premultipliedAlpha: false,
      preserveDrawingBuffer: false,
    });
  } catch {
    return null;
  }
  if (!gl) return null;
  const nodes = makeNodeProg(gl);
  const lines = makeLineProg(gl);
  if (!nodes || !lines) return null;
  gl.enable(gl.BLEND);
  gl.blendFunc(gl.SRC_ALPHA, gl.ONE_MINUS_SRC_ALPHA);
  gl.disable(gl.DEPTH_TEST);
  let dead = false;

  const onLost = () => {
    if (dead) return;
    dead = true;
    opts?.onLost?.();
  };
  canvas.addEventListener("webglcontextlost", onLost);

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

      // Bottom layer. S5 draws clouds here once presenceClouds returns any.
      void presenceClouds(scene);

      const zoom = scene.camera.k;
      const paintLines = (strokes: Stroke[], rgba: [number, number, number, number] | null) => {
        for (const run of groupStrokes(strokes, zoom)) {
          if (run.data.length === 0) continue;
          setFrame(gl!, lines, scene);
          const [cr, cg, cb] = rgba ? [rgba[0], rgba[1], rgba[2]] : parseHex(run.color);
          const alpha = rgba ? rgba[3] : 0.85;
          gl!.uniform4f(lines.color, cr, cg, cb, alpha);
          drawInstances(gl!, lines, new Float32Array(run.data), LINE_FLOATS);
        }
      };
      paintLines(scene.strokes, null);

      const dots = markersOf(scene.strokes, zoom);
      if (dots.length) {
        const data: number[] = [];
        const aa = 1 / Math.max(Math.abs(zoom) * scene.dpr, 0.001);
        for (const dot of dots) pushNode(data, dot.x, dot.y, dot.r, dot.r + aa, dot.color, 5, 1);
        setFrame(gl!, nodes, scene);
        gl!.uniform1f(nodes.k, zoom);
        drawInstances(gl!, nodes, new Float32Array(data), NODE_FLOATS);
      }

      if (scene.nodes.length) {
        const data: number[] = [];
        for (const node of scene.nodes) {
          pushNode(
            data,
            node.x,
            node.y,
            node.r,
            nodeExtent(node, zoom, scene.dpr),
            node.color,
            shapeKind(node),
            node.alpha,
          );
        }
        setFrame(gl!, nodes, scene);
        gl!.uniform1f(nodes.k, zoom);
        drawInstances(gl!, nodes, new Float32Array(data), NODE_FLOATS);
      }

      const order = ["contention", "pin", "selection"] as const;
      const rings: number[] = [];
      const aa = 1 / Math.max(Math.abs(zoom) * scene.dpr, 0.001);
      const ringPad = 1.5 / Math.max(Math.abs(zoom), 0.05) / 2 + aa;
      for (const kind of order) {
        for (const ring of scene.rings) {
          if (ring.kind !== kind) continue;
          pushNode(rings, ring.x, ring.y, ring.r, ring.r + ringPad, ring.color, 4, 0.9);
        }
      }
      if (rings.length) {
        setFrame(gl!, nodes, scene);
        gl!.uniform1f(nodes.k, zoom);
        drawInstances(gl!, nodes, new Float32Array(rings), NODE_FLOATS);
      }

      if (scene.glow.length) {
        const halo: Stroke[] = scene.glow.map((g) => ({ ...g, widthPx: g.widthPx + 16, color: "#c6f24e" }));
        paintLines(halo, [GLOW[0], GLOW[1], GLOW[2], 0.35]);
        for (const run of groupStrokes(scene.glow, zoom)) {
          setFrame(gl!, lines, scene);
          const [cr, cg, cb] = parseHex(run.color);
          gl!.uniform4f(lines.color, cr, cg, cb, 0.95);
          drawInstances(gl!, lines, new Float32Array(run.data), LINE_FLOATS);
        }
      }
    },
    destroy() {
      dead = true;
      canvas.removeEventListener("webglcontextlost", onLost);
      gl!.deleteProgram(nodes.program);
      gl!.deleteProgram(lines.program);
      gl!.deleteBuffer(nodes.buf);
      gl!.deleteBuffer(lines.buf);
      gl!.deleteVertexArray(nodes.vao);
      gl!.deleteVertexArray(lines.vao);
    },
  };
  return api;
}
