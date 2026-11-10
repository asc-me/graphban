/**
 * Shared look for the Harness page: per-row accent colours, the rate tiers, and the model
 * family and vendor a harness : model pair is filed under. None of these are served — they
 * are derived from the pair's names, the same way on every tab, so a row keeps its colour
 * from the grid to the curve to the guidance table.
 */

/** The design's row swatches. Assigned per pair, not per harness: sonnet and opus differ. */
const PALETTE = ["#c6f24e", "#a78bfa", "#7ca2ff", "#e0b34a", "#5fd07a", "#ff8f8f", "#5fd0c8", "#f0a35e"];

function hash(s: string): number {
  let x = 2166136261;
  for (let i = 0; i < s.length; i++) {
    x ^= s.charCodeAt(i);
    x = Math.imul(x, 16777619);
  }
  return x >>> 0;
}

/**
 * A stable colour per `vendor:model`. Each pair starts at its hash's slot and walks to the
 * next free one, in sorted-key order, so the same set of pairs always gets the same colours
 * and two pairs only share one when there are more pairs than swatches.
 */
export function accentMap(keys: string[]): Map<string, string> {
  const out = new Map<string, string>();
  const used = new Set<number>();
  for (const key of [...new Set(keys)].sort()) {
    let slot = hash(key) % PALETTE.length;
    if (used.size < PALETTE.length) {
      while (used.has(slot)) slot = (slot + 1) % PALETTE.length;
    }
    used.add(slot);
    out.set(key, PALETTE[slot]);
  }
  return out;
}

/** `vendor:model`, the key every tab uses for a pair (routing rows already speak it). */
export const pairKey = (vendor: string, model: string) => `${vendor}:${model}`;

/**
 * The model family a pair belongs to: the model name's leading word, digits kept when they
 * are part of the name (`qwen3-coder:30b` → qwen3, `sonnet-4.5` → sonnet, `grok-4` → grok).
 * A leading `claude-` is the vendor, not the family. No model → the harness name.
 */
export function modelFamily(vendor: string, model: string): string {
  const name = (model || vendor || "").toLowerCase().replace(/^claude-/, "");
  const m = /^[a-z]+\d*(?=[^a-z0-9]|$)|^[a-z]+/.exec(name);
  return m ? m[0] : name || "unknown";
}

const VENDOR_BY_FAMILY: [RegExp, string][] = [
  [/^(sonnet|opus|haiku|claude)/, "Anthropic"],
  [/^(gpt|o\d|codex)/, "OpenAI"],
  [/^grok/, "xAI"],
  [/^gemini/, "Google"],
  [/^qwen/, "Alibaba"],
  [/^deepseek/, "DeepSeek"],
  [/^(composer|auto|cursor)/, "Cursor"],
  [/^(llama)/, "Meta"],
  [/^(mistral|codestral|devstral)/, "Mistral"],
];
const VENDOR_BY_HARNESS: Record<string, string> = {
  claude: "Anthropic", grok: "xAI", agy: "Google", dsh: "DeepSeek", "qwen-code": "Alibaba",
  "cursor-agent": "Cursor", cursor: "Cursor", codex: "OpenAI",
};

/** Who makes the model: from the model family first, then the harness, else the harness name. */
export function modelVendor(vendor: string, model: string): string {
  const fam = modelFamily(vendor, model);
  for (const [re, name] of VENDOR_BY_FAMILY) if (re.test(fam)) return name;
  return VENDOR_BY_HARNESS[vendor] ?? (vendor || "unknown");
}

export type Tier = "s" | "good" | "warn" | "bad" | "floor";

/** Above the floor: >95 S, 80–95 good, 65–79 borderline, under 65 failing. */
export function tierOf(rate: number | null, belowFloor: boolean): Tier {
  if (belowFloor || rate === null) return "floor";
  if (rate > 0.95) return "s";
  if (rate >= 0.8) return "good";
  if (rate >= 0.65) return "warn";
  return "bad";
}

export const TIER: Record<Tier, { cell: string; text: string; bar: string; swatch: string; label: string }> = {
  s: {
    cell: "border-[rgba(167,139,250,0.5)] bg-[linear-gradient(135deg,rgba(167,139,250,0.22),rgba(95,208,122,0.16))]",
    text: "text-purple",
    bar: "bg-[linear-gradient(90deg,#a78bfa,#5fd07a)]",
    swatch: "border-[rgba(167,139,250,0.5)] bg-[linear-gradient(135deg,rgba(167,139,250,0.22),rgba(95,208,122,0.16))]",
    label: "S · >95",
  },
  good: {
    cell: "border-[rgba(95,208,122,0.38)] bg-[rgba(95,208,122,0.12)]",
    text: "text-st-done",
    bar: "bg-st-done",
    swatch: "border-[rgba(95,208,122,0.38)] bg-[rgba(95,208,122,0.12)]",
    label: "good · 80–95",
  },
  warn: {
    cell: "border-[rgba(240,180,80,0.38)] bg-[rgba(240,180,80,0.1)]",
    text: "text-[#f0b450]",
    bar: "bg-[#f0b450]",
    swatch: "border-[rgba(240,180,80,0.38)] bg-[rgba(240,180,80,0.1)]",
    label: "borderline · 65–79",
  },
  bad: {
    cell: "border-[rgba(255,107,107,0.38)] bg-[rgba(255,107,107,0.1)]",
    text: "text-st-blocked",
    bar: "bg-st-blocked",
    swatch: "border-[rgba(255,107,107,0.38)] bg-[rgba(255,107,107,0.1)]",
    label: "failing · <65",
  },
  floor: {
    cell: "border-dashed border-line bg-surface-3/40",
    text: "text-faint",
    bar: "bg-faint-2/50",
    swatch: "border-dashed border-line bg-surface-3/40",
    label: "unmeasured",
  },
};

export const pct = (r: number | null) => (r === null ? "—" : `${Math.round(r * 100)}%`);
