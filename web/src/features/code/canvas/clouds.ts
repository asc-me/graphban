/**
 * Presence clouds, the bottom layer (PRD-52 D5 z-order).
 *
 * S5 fills this. The module is called on every frame so a later slice adds
 * clouds without editing the renderer.
 */

import type { DrawScene } from "./scene";

export interface Cloud {
  x: number;
  y: number;
  r: number;
  color: string;
}

export function drawClouds(_ctx: CanvasRenderingContext2D, _scene: DrawScene): void {
  // No presence in S2. The call is the contract.
}

/** Bottom layer for both renderers. S5 fills this; an empty list is "no presence". */
export function presenceClouds(_scene: DrawScene): readonly Cloud[] {
  return [];
}
