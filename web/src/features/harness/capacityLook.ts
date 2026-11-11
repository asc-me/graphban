import type { HarnessCapacityEntitlement } from "@/lib/types";

export type CapState = HarnessCapacityEntitlement["state"];

/**
 * The five headroom states, worst first. The border style carries meaning as well as colour:
 * dashed = never measured, dotted = gone quiet, solid = the server heard something.
 */
export const STATE: Record<CapState, { glyph: string; word: string; text: string; pill: string; detail: string; rank: number }> = {
  broken: { glyph: "✕", word: "BROKEN", text: "text-st-blocked", pill: "border-solid border-[rgba(255,107,107,0.6)]", detail: "spawn failed, not quota", rank: 0 },
  exhausted: { glyph: "◒", word: "EXHAUSTED", text: "text-[#ff9f5a]", pill: "border-solid border-[rgba(255,159,90,0.55)]", detail: "the vendor said so", rank: 1 },
  unreachable: { glyph: "⋯", word: "UNREACHABLE", text: "text-muted-2", pill: "border-dotted border-[#5c656e]", detail: "runner gone quiet", rank: 2 },
  unknown: { glyph: "○", word: "UNKNOWN", text: "text-muted-2", pill: "border-dashed border-[#5c656e]", detail: "never measured", rank: 3 },
  available: { glyph: "●", word: "AVAILABLE", text: "text-st-done", pill: "border-solid border-[rgba(95,208,122,0.5)]", detail: "measured headroom", rank: 4 },
};

/** 0.9M, 6.4M, 25M, 12.3k, 640. */
export function fmtTokens(n: number): string {
  const a = Math.abs(n);
  if (a >= 1_000_000) {
    const m = n / 1_000_000;
    return `${Math.abs(m) >= 10 ? m.toFixed(0) : m.toFixed(1)}M`;
  }
  if (a >= 1_000) {
    const k = n / 1_000;
    return `${Math.abs(k) >= 10 ? k.toFixed(0) : k.toFixed(1)}k`;
  }
  return String(Math.round(n));
}

const sameDay = (a: Date, b: Date) => a.toDateString() === b.toDateString();

/** "13:55" today, "Nov 9 14:00" otherwise. */
export function shortWhen(iso: string, now: Date = new Date()): string {
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return iso;
  const hm = d.toLocaleTimeString("en-US", { hour: "2-digit", minute: "2-digit", hour12: false });
  return sameDay(d, now) ? hm : `${shortDay(iso)} ${hm}`;
}

/** "Nov 9". */
export function shortDay(iso: string): string {
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return iso;
  return d.toLocaleDateString("en-US", { month: "short", day: "numeric" });
}
