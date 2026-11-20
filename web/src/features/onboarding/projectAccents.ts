/* Twelve accents, in hue order. Left out on purpose: operator orange (#ff8a3d) marks the
   operator plane, and the five status tokens — --color-st-done #5fd07a, --color-st-next
   #7ca2ff, --color-st-review #e0b34a, --color-st-blocked #ff6b6b, --color-st-backlog
   #8b949e — so a project tile never reads as a status. Mint, Sky, Indigo, Rose and Sand
   sit next to a status hue and are kept lighter than it. Brand lime (#c6f24e) is also
   --color-st-in_progress; the design keeps that one. */
export const PROJECT_ACCENT_SWATCHES = [
  { hex: "#c6f24e", name: "Lime" },
  { hex: "#a6ebc4", name: "Mint" },
  { hex: "#4fd1c5", name: "Teal" },
  { hex: "#67d4f0", name: "Cyan" },
  { hex: "#a3cfff", name: "Sky" },
  { hex: "#9d9cff", name: "Indigo" },
  { hex: "#a78bfa", name: "Violet" },
  { hex: "#d58cf0", name: "Orchid" },
  { hex: "#f47fb4", name: "Pink" },
  { hex: "#ffb3c1", name: "Rose" },
  { hex: "#e6d2a8", name: "Sand" },
  { hex: "#efd76a", name: "Yellow" },
] as const;

export const PROJECT_ACCENTS = PROJECT_ACCENT_SWATCHES.map((swatch) => swatch.hex);
