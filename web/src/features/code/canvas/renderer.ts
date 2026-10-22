/**
 * The renderer seam (PRD-52 D5). Canvas 2D implements it in this slice.
 * WebGL2 (S10) implements the same interface over the same layer modules.
 */

import type { DrawScene } from "./scene";

export interface Renderer {
  readonly kind: "canvas2d" | "webgl";
  draw(scene: DrawScene): void;
  destroy(): void;
}
