import type { RequestType } from "@/lib/types";

export type FeedbackTheme = "dark" | "light" | "auto";
export type FeedbackMode = "inline" | "launcher";
export type LauncherPosition = "bottom-right" | "bottom-left";

export interface FeedbackConfig {
  accent: string; // hex incl. #
  radius: number; // px
  types: RequestType[];
  showEmail: boolean;
  projectId: string;
  theme: FeedbackTheme;
  // Copy
  title: string;
  subtitle: string;
  buttonLabel: string;
  successText: string;
  // Embed
  mode: FeedbackMode;
  launcherLabel: string;
  position: LauncherPosition;
  // Capabilities
  attachments: boolean; // allow screenshot upload
  turnstileSitekey: string; // Cloudflare Turnstile; empty → no captcha
  // PRD-43 D1: ingest token for Bearer auth on submit. Empty = legacy share-token / no auth.
  ingestToken: string;
}

export const ALL_TYPES: RequestType[] = ["bug", "feature", "enhancement", "feedback"];

export const DEFAULT_CONFIG: FeedbackConfig = {
  accent: "#c6f24e",
  radius: 10,
  types: ALL_TYPES,
  showEmail: true,
  projectId: "", // filled with the active project by the Feedback Kit; empty → server default
  theme: "auto",
  title: "Send feedback",
  subtitle: "Found a bug or have an idea? Tell us.",
  buttonLabel: "Send feedback",
  successText: "Thanks — we got it.",
  mode: "inline",
  launcherLabel: "Feedback",
  position: "bottom-right",
  attachments: true,
  turnstileSitekey: "",
  ingestToken: "",
};

// Params the /embed/feedback iframe reads (widget-affecting only; launcher options
// live in the snippet, not the iframe URL). Defaults are omitted to keep URLs short.
export function toParams(cfg: FeedbackConfig): string {
  const p = new URLSearchParams();
  p.set("accent", cfg.accent.replace("#", ""));
  p.set("radius", String(cfg.radius));
  p.set("types", cfg.types.join(","));
  p.set("email", cfg.showEmail ? "1" : "0");
  if (cfg.projectId) p.set("project", cfg.projectId);
  if (cfg.theme !== DEFAULT_CONFIG.theme) p.set("theme", cfg.theme);
  if (cfg.title !== DEFAULT_CONFIG.title) p.set("title", cfg.title);
  if (cfg.subtitle !== DEFAULT_CONFIG.subtitle) p.set("subtitle", cfg.subtitle);
  if (cfg.buttonLabel !== DEFAULT_CONFIG.buttonLabel) p.set("btn", cfg.buttonLabel);
  if (cfg.successText !== DEFAULT_CONFIG.successText) p.set("done", cfg.successText);
  if (!cfg.attachments) p.set("att", "0");
  if (cfg.turnstileSitekey) p.set("ts", cfg.turnstileSitekey);
  if (cfg.ingestToken) p.set("it", cfg.ingestToken);
  return p.toString();
}

export function fromParams(search: URLSearchParams): FeedbackConfig {
  const accent = search.get("accent");
  const radius = search.get("radius");
  const types = search.get("types");
  const theme = search.get("theme");
  const isTheme = (v: string | null): v is FeedbackTheme =>
    v === "dark" || v === "light" || v === "auto";
  return {
    ...DEFAULT_CONFIG,
    accent: accent ? `#${accent.replace("#", "")}` : DEFAULT_CONFIG.accent,
    radius: radius ? Number(radius) : DEFAULT_CONFIG.radius,
    types: types
      ? (types.split(",").filter((t) => ALL_TYPES.includes(t as RequestType)) as RequestType[])
      : DEFAULT_CONFIG.types,
    showEmail: search.get("email") !== "0",
    projectId: search.get("project") ?? DEFAULT_CONFIG.projectId,
    theme: isTheme(theme) ? theme : DEFAULT_CONFIG.theme,
    title: search.get("title") ?? DEFAULT_CONFIG.title,
    subtitle: search.get("subtitle") ?? DEFAULT_CONFIG.subtitle,
    buttonLabel: search.get("btn") ?? DEFAULT_CONFIG.buttonLabel,
    successText: search.get("done") ?? DEFAULT_CONFIG.successText,
    attachments: search.get("att") !== "0",
    turnstileSitekey: search.get("ts") ?? "",
    ingestToken: search.get("it") ?? "",
  };
}

// The palette the widget paints itself with, so it blends on light or dark hosts.
export const THEME_TOKENS: Record<"dark" | "light", Record<string, string>> = {
  dark: {
    surface: "#12171b",
    inputBg: "#101418",
    border: "#1e242a",
    borderHover: "#2a333a",
    text: "#e6e9ec",
    muted: "#8b949e",
    faint: "#5c656e",
  },
  light: {
    surface: "#ffffff",
    inputBg: "#f6f7f9",
    border: "#e4e7ea",
    borderHover: "#cfd5da",
    text: "#1a1f24",
    muted: "#5c656e",
    faint: "#98a0a8",
  },
};

export function resolveTheme(theme: FeedbackTheme): "dark" | "light" {
  if (theme !== "auto") return theme;
  if (typeof window !== "undefined" && window.matchMedia) {
    return window.matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light";
  }
  return "dark";
}

/** Dark text on light accents, light text on dark accents. */
export function readableOn(hex: string): string {
  const m = hex.replace("#", "");
  const n = m.length === 3 ? m.split("").map((c) => c + c).join("") : m;
  const r = parseInt(n.slice(0, 2), 16) || 0;
  const g = parseInt(n.slice(2, 4), 16) || 0;
  const b = parseInt(n.slice(4, 6), 16) || 0;
  const lum = (0.299 * r + 0.587 * g + 0.114 * b) / 255;
  return lum > 0.6 ? "#0a0c0e" : "#ffffff";
}
