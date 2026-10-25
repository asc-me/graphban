/**
 * Which shape a code-graph node kind is (GRPH-1027).
 *
 * The server serves five kinds (`NODE_KINDS`: module, file, symbol, doc, config).
 * The legend, the inspector chip, the SVG glyph and both canvas rasterisers ask
 * this function, so one of them cannot keep drawing a circle while another
 * explains a diamond.
 *
 * `Build.dc.html` is not in the repo. The shapes already drawn here — ring,
 * circle, diamond, square — left doc and config on the same square, so that
 * pair was still colour alone (WCAG 1.4.1). Config is a flat-top hexagon.
 * That disagreement with the design is recorded in PRD-47 §1.3.
 *
 * `collapsed` is the canvas level-of-detail: under 2.2px every mark is a
 * square, whatever its kind. A readable config node is not collapsed.
 */

export type KindShape = "ring" | "circle" | "diamond" | "square" | "hexagon";

export function kindShape(kind: string, collapsed = false): KindShape {
  if (collapsed) return "square";
  if (kind === "module") return "ring";
  if (kind === "symbol" || kind === "request") return "diamond";
  if (kind === "doc") return "square";
  if (kind === "config") return "hexagon";
  return "circle";
}

/**
 * WebGL node codes. 4, 5 and 6 are the selection ring, the edge dot and the
 * presence cloud — not node kinds — so the hexagon is 7.
 */
export function shapeCode(shape: KindShape): number {
  if (shape === "diamond") return 1;
  if (shape === "ring") return 2;
  if (shape === "square") return 3;
  if (shape === "hexagon") return 7;
  return 0;
}

/** Flat-top hexagon. `r` is the circumradius: a vertex on the left and the right. */
export function hexagonPoints(r: number): [number, number][] {
  const pts: [number, number][] = [];
  for (let i = 0; i < 6; i++) {
    const a = (i * Math.PI) / 3;
    pts.push([r * Math.cos(a), r * Math.sin(a)]);
  }
  return pts;
}
