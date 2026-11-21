import { describe, expect, it } from "vitest";

import { PROJECT_PATTERNS, projectFill } from "@/lib/projectFill";

const ACCENT = "#c6f24e";
const ACCENT2 = "#67d4f0";
const INK = "rgba(10,11,13,.34)";
const OMBRE = `linear-gradient(135deg, ${ACCENT} 12%, ${ACCENT2} 88%)`;

function decodedSvg(fill: string): { svg: string; w: number; h: number; d: string } {
  const encoded = fill.match(/data:image\/svg\+xml,([^']+)/);
  expect(encoded, fill).not.toBeNull();
  const svg = decodeURIComponent(encoded![1]);
  const w = Number(svg.match(/width="([\d.]+)"/)?.[1]);
  const h = Number(svg.match(/height="([\d.]+)"/)?.[1]);
  const d = svg.match(/d="([^"]+)"/)?.[1] ?? "";
  return { svg, w, h, d };
}

/** Control-point bounds of the path, plus the circle each ring arc draws. */
function pathBounds(d: string): { minX: number; minY: number; maxX: number; maxY: number } {
  const tokens = d.match(/[a-zA-Z]|-?\d*\.?\d+(?:e[-+]?\d+)?/g) ?? [];
  let i = 0;
  let x = 0;
  let y = 0;
  let minX = Infinity;
  let minY = Infinity;
  let maxX = -Infinity;
  let maxY = -Infinity;
  const bump = (px: number, py: number) => {
    minX = Math.min(minX, px);
    minY = Math.min(minY, py);
    maxX = Math.max(maxX, px);
    maxY = Math.max(maxY, py);
  };
  const read = () => Number(tokens[i++]);
  while (i < tokens.length) {
    const cmd = tokens[i++];
    if (cmd === "M") {
      x = read();
      y = read();
      bump(x, y);
    } else if (cmd === "c") {
      while (i < tokens.length && !/[a-zA-Z]/.test(tokens[i])) {
        const x1 = x + read();
        const y1 = y + read();
        const x2 = x + read();
        const y2 = y + read();
        x += read();
        y += read();
        bump(x1, y1);
        bump(x2, y2);
        bump(x, y);
      }
    } else if (cmd === "a") {
      while (i < tokens.length && !/[a-zA-Z]/.test(tokens[i])) {
        const rx = read();
        const ry = read();
        read();
        read();
        read();
        const nx = x + read();
        const ny = y + read();
        // Two semicircle arcs make the eye. The center sits on the chord.
        const cx = (x + nx) / 2;
        const cy = (y + ny) / 2;
        bump(cx - rx, cy - ry);
        bump(cx + rx, cy + ry);
        x = nx;
        y = ny;
      }
    } else if (cmd === "z" || cmd === "Z") {
      // close
    } else {
      throw new Error(`unparsed path command ${cmd}`);
    }
  }
  return { minX, minY, maxX, maxY };
}

describe("projectFill", () => {
  it("returns the solid accent when accent2 is null and the pattern is none", () => {
    expect(projectFill({ accent: ACCENT, accent2: null, pattern: "none" })).toBe(ACCENT);
  });

  it("returns a gradient when accent2 differs", () => {
    expect(projectFill({ accent: ACCENT, accent2: ACCENT2, pattern: "none" })).toBe(OMBRE);
  });

  it("returns a solid fill when accent2 equals accent, including case", () => {
    expect(projectFill({ accent: ACCENT, accent2: ACCENT, pattern: "none" })).toBe(ACCENT);
    expect(projectFill({ accent: ACCENT, accent2: ACCENT.toUpperCase(), pattern: "stripes" })).toBe(
      projectFill({ accent: ACCENT, accent2: null, pattern: "stripes" }),
    );
  });

  it("lays each pattern over the solid base and over the ombre", () => {
    const patterned = PROJECT_PATTERNS.filter((p) => p.id !== "none");
    expect(patterned.length).toBe(7);
    for (const { id } of patterned) {
      const solid = projectFill({ accent: ACCENT, accent2: null, pattern: id });
      expect(solid.endsWith(ACCENT), id).toBe(true);
      expect(solid, id).not.toBe(ACCENT);
      // SVG layers encode the ink. Gradient layers leave it as written.
      const readable = solid.includes("svg+xml") ? decodeURIComponent(solid) : solid;
      expect(readable, id).toContain(INK);
      const ombre = projectFill({ accent: ACCENT, accent2: ACCENT2, pattern: id });
      expect(ombre.endsWith(OMBRE), id).toBe(true);
      expect(ombre.startsWith(solid.slice(0, solid.length - ACCENT.length)), id).toBe(true);
    }
  });

  it("exports the pattern list with the design's ids and names", () => {
    expect(PROJECT_PATTERNS).toEqual([
      { id: "none", name: "None" },
      { id: "speckled", name: "Speckled" },
      { id: "crosshatch", name: "Cross hatch" },
      { id: "chevron", name: "Chevron" },
      { id: "herringbone", name: "Herringbone" },
      { id: "stripes", name: "Diagonal stripes" },
      { id: "paisley", name: "Paisley" },
      { id: "bubbles", name: "Bubbles" },
    ]);
  });

  it("ports the design's ink, stroke and motif paths", () => {
    const chevron = decodedSvg(projectFill({ accent: ACCENT, pattern: "chevron" }));
    expect(chevron.svg).toContain(`stroke="${INK}"`);
    expect(chevron.svg).toContain('stroke-width="1.4"');
    expect(chevron.svg).toContain('stroke-linecap="round"');
    expect(chevron.d).toContain("M0 7L6 1L12 7");
    expect(chevron.w).toBe(12);
    expect(chevron.h).toBe(8);

    const bone = decodedSvg(projectFill({ accent: ACCENT, pattern: "herringbone" }));
    expect(bone.d).toContain("M0 2L8 -6M0 8L8 0M0 14L8 6M8 -3L16 5M8 3L16 11M8 9L16 17");

    const bubbles = decodedSvg(projectFill({ accent: ACCENT, pattern: "bubbles" }));
    expect(bubbles.w).toBe(22);
    expect(bubbles.h).toBe(22);
    // The four rings from the design, radii unchanged: 3.2, 1.6, 4.6, 1.8.
    expect(bubbles.d).toContain("a3.2 3.2");
    expect(bubbles.d).toContain("a1.6 1.6");
    expect(bubbles.d).toContain("a4.6 4.6");
    expect(bubbles.d).toContain("a1.8 1.8");
  });

  it("fits a whole paisley inside a 28px swatch", () => {
    const { svg, w, h, d } = decodedSvg(projectFill({ accent: ACCENT, pattern: "paisley" }));
    // The design's 32px tile does not fit a 28px swatch. This repeat does.
    expect(w).toBeLessThanOrEqual(28);
    expect(h).toBeLessThanOrEqual(28);
    expect(svg).not.toContain('width="32"');
    // Two teardrops, each closed. A dropped motif would still be a small tile.
    expect(d.match(/z/g)).toHaveLength(2);
    const box = pathBounds(d);
    const stroke = 0.7;
    expect(box.minX - stroke).toBeGreaterThanOrEqual(0);
    expect(box.minY - stroke).toBeGreaterThanOrEqual(0);
    expect(box.maxX + stroke).toBeLessThanOrEqual(w);
    expect(box.maxY + stroke).toBeLessThanOrEqual(h);
  });
});
