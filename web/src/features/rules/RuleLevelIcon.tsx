import type { Level } from "./levels";

/**
 * The enforcement-level glyph (`docs/design/Rule Level Icon.dc.html`). Always drawn next to
 * its word — never colour alone. Decorative to assistive tech; the word carries the meaning.
 */
export function RuleLevelIcon({ level, size = 12 }: { level: Level | null; size?: number }) {
  const common = {
    width: size,
    height: size,
    viewBox: "0 0 24 24",
    "aria-hidden": true,
    focusable: false,
    style: { flex: "none" },
  } as const;
  const line = {
    fill: "none",
    stroke: "currentColor",
    strokeWidth: 1.8,
    strokeLinecap: "round",
    strokeLinejoin: "round",
  } as const;
  switch (level) {
    case "enforced":
      return (
        <svg {...common} data-level="enforced">
          <path d="M12 2.5 20 6v5.5c0 5-3.4 8.7-8 10-4.6-1.3-8-5-8-10V6z" fill="currentColor" />
          <path d="m8.5 12 2.4 2.4 4.6-4.8" fill="none" stroke="#0a0c0e" strokeWidth={2.2} strokeLinecap="round" strokeLinejoin="round" />
        </svg>
      );
    case "checked":
      return (
        <svg {...common} {...line} data-level="checked">
          <circle cx="12" cy="12" r="9" />
          <path d="m8.5 12 2.4 2.4 4.6-4.8" />
        </svg>
      );
    case "external":
      return (
        <svg {...common} {...line} data-level="external">
          <path d="M14 4h6v6" />
          <path d="M20 4 11 13" />
          <path d="M18 14v5a1 1 0 0 1-1 1H5a1 1 0 0 1-1-1V7a1 1 0 0 1 1-1h5" />
        </svg>
      );
    case "advisory":
      return (
        <svg {...common} {...line} data-level="advisory">
          <path d="M2 4h6a4 4 0 0 1 4 4v13a3 3 0 0 0-3-3H2z" />
          <path d="M22 4h-6a4 4 0 0 0-4 4v13a3 3 0 0 1 3-3h7z" />
        </svg>
      );
    case "unmeasured":
      return (
        <svg {...common} {...line} data-level="unmeasured">
          <circle cx="12" cy="12" r="9" strokeDasharray="3 3" />
          <path d="M7 17 17 7" />
        </svg>
      );
    default:
      // Not used, and a stage with nothing registered.
      return (
        <svg {...common} {...line} data-level={level ?? "none"}>
          <circle cx="12" cy="12" r="9" />
          <path d="M8 12h8" />
        </svg>
      );
  }
}
