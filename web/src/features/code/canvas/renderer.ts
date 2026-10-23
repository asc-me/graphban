/**
 * The renderer seam (PRD-52 D5). WebGL2 is primary; Canvas 2D is the fallback.
 * Both rasterise the same scene. Labels, hulls and the tooltip stay in the DOM.
 */

import type { DrawScene } from "./scene";

export interface Renderer {
  readonly kind: "canvas2d" | "webgl";
  draw(scene: DrawScene): void;
  destroy(): void;
}
