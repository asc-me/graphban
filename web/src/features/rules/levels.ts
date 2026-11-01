/**
 * Rules map model (GRPH-1162, `docs/design/rules-view-design.md`). Pure: no React, no fetch.
 *
 * The server decides each stage's state and each rule's `used` (`services/rules.py:flow`).
 * Nothing here invents a rule, a stage or a count; it only turns the payload into what the
 * map draws.
 */
import type { RuleEnforcement, RuleFlowRule, RuleStage, RulesFlow } from "@/lib/types";

/** What a rule or stage is drawn as. */
export type Level = RuleEnforcement | "not_used";

export interface LevelMeta {
  word: string;
  /** Text and icon colour. */
  color: string;
  /** Card border, stage-card border. */
  border: string;
  stageBorder: string;
  bg: string;
  pillBorder: string;
  pillBg: string;
  /** Tally-bar fill. */
  bar: string;
  /** Lower is weaker. Not used is excluded from weakness, so it sorts last. */
  rank: number;
}

const HATCH =
  "repeating-linear-gradient(135deg,transparent 0 7px,rgba(255,107,107,.09) 7px 8px),#0f1317";

/** Treatments match `Rules.dc.html` exactly; icon and word go with every one of them. */
export const LEVELS: Record<Level, LevelMeta> = {
  enforced: {
    word: "ENFORCED", color: "#c6f24e", border: "1px solid #2a333a", stageBorder: "1.5px solid #2f3a42",
    bg: "#12171b", pillBorder: "1px solid rgba(198,242,78,.32)", pillBg: "rgba(198,242,78,.09)",
    bar: "#c6f24e", rank: 3,
  },
  checked: {
    word: "CHECKED", color: "#7ca2ff", border: "1px solid rgba(124,162,255,.42)",
    stageBorder: "1.5px solid rgba(124,162,255,.5)", bg: "#0f1317",
    pillBorder: "1px solid rgba(124,162,255,.4)", pillBg: "rgba(124,162,255,.08)", bar: "#7ca2ff", rank: 2,
  },
  external: {
    word: "EXTERNAL", color: "#c9b8ff", border: "3px double rgba(167,139,250,.5)",
    stageBorder: "3px double rgba(167,139,250,.6)", bg: "#0f1317",
    pillBorder: "3px double rgba(167,139,250,.5)", pillBg: "rgba(167,139,250,.08)",
    bar: "#c9b8ff", rank: 1.5,
  },
  advisory: {
    word: "ADVISORY", color: "#e0b34a", border: "1.5px dashed rgba(224,179,74,.7)",
    stageBorder: "2px dashed rgba(224,179,74,.85)", bg: "rgba(15,19,23,.6)",
    pillBorder: "1px dashed rgba(224,179,74,.6)", pillBg: "rgba(224,179,74,.07)",
    bar: "repeating-linear-gradient(90deg,#e0b34a 0 4px,transparent 4px 6px)", rank: 1,
  },
  unmeasured: {
    word: "UNMEASURED", color: "#ff6b6b", border: "1.5px dotted #ff6b6b", stageBorder: "2px dotted #ff6b6b",
    bg: HATCH, pillBorder: "1px dotted #ff6b6b", pillBg: "rgba(255,107,107,.06)",
    bar: "repeating-linear-gradient(90deg,#ff6b6b 0 2px,transparent 2px 4px)", rank: 0,
  },
  not_used: {
    word: "NOT USED", color: "#8b949e", border: "1.5px dashed #2a333a", stageBorder: "1.5px dashed #2a333a",
    bg: "#0f1317", pillBorder: "1px dashed #2a333a", pillBg: "transparent", bar: "#2a333a", rank: 9,
  },
};

/** Legend and tally order, strongest first; not used last. */
export const LEVEL_ORDER: Level[] = ["enforced", "checked", "external", "advisory", "unmeasured", "not_used"];

/**
 * The collapsed "Agent instructions (Graphban pack)" node, by the id `rules.flow` gives it. Not
 * by `skills`: the response model sends `skills: null` on every rule, and null is also the
 * pack's own "list not readable" answer.
 */
export const PACK_ID = "agents.skills_pack";

export function isPack(rule: RuleFlowRule): boolean {
  return rule.id === PACK_ID;
}

export function ruleLevel(rule: RuleFlowRule): Level {
  // A library template the pack loader refused is UNMEASURED, not "off": it was never
  // adoptable, and NOT USED would read as a choice someone made (PRD-P63 D12).
  if (rule.unmeasured_reason) return "unmeasured";
  return rule.used ? rule.enforcement : "not_used";
}

/** The rules a stage card tallies — the pack is drawn off the flow, on its own. */
export function stageRules(stage: RuleStage): RuleFlowRule[] {
  return stage.rules.filter((r) => !isPack(r));
}

/** Weakest first, as the inspector lists them; not used last. Stable within a level. */
export function weakestFirst(rules: RuleFlowRule[]): RuleFlowRule[] {
  return [...rules].sort((a, b) => LEVELS[ruleLevel(a)].rank - LEVELS[ruleLevel(b)].rank);
}

/**
 * What the stage card is drawn as: the server's state, and never stronger than the weakest
 * used rule it lists. The second half is a guard, not a recomputation — if the payload ever
 * disagreed with its own rules, the weaker reading wins, because a drawn rule must never read
 * as an enforced one. `null` is a stage with nothing registered.
 */
export function stageLevel(stage: RuleStage): Level | null {
  if (stage.rules.length === 0) return null;
  let level: Level = stage.state;
  for (const r of stage.rules) {
    const drawn = ruleLevel(r);
    if (drawn === "not_used") continue;
    if (level === "not_used" || LEVELS[drawn].rank < LEVELS[level].rank) level = drawn;
  }
  return level;
}

export interface Tally {
  level: Level;
  n: number;
}

export function tally(rules: RuleFlowRule[]): Tally[] {
  return LEVEL_ORDER.map((level) => ({ level, n: rules.filter((r) => ruleLevel(r) === level).length }))
    .filter((t) => t.n > 0);
}

export function tallyText(t: Tally): string {
  return `${t.n} ${t.level === "not_used" ? "not used" : t.level}`;
}

/** Legend counts: every registry rule once, by the level it is drawn at. The pack is not a registry rule. */
export function legendCounts(flow: RulesFlow): Record<Level, number> {
  const out = Object.fromEntries(LEVEL_ORDER.map((l) => [l, 0])) as Record<Level, number>;
  for (const s of flow.stages) for (const r of stageRules(s)) out[ruleLevel(r)] += 1;
  return out;
}

export function packOf(flow: RulesFlow): RuleFlowRule | null {
  for (const s of flow.stages) for (const r of s.rules) if (isPack(r)) return r;
  return null;
}

// ── Enforcer and per-row copy ──────────────────────────────────────────────────

const KIND_LABEL: Record<string, string> = {
  graphban_source: "SOURCE",
  ci_job: "CI",
  scan: "SCAN",
  skill: "SKILL",
  doc: "DOC",
  context: "CONTEXT",
  forge: "FORGE",
  supervisor: "SUPERVISOR",
};

export function enforcerKindLabel(kind: string): string {
  return KIND_LABEL[kind] ?? kind.toUpperCase();
}

export function enforcerText(rule: RuleFlowRule, version: string): string {
  const e = rule.enforcer;
  if (e.kind === "graphban_source") return `graphban · ${e.ref} @ v${version}`;
  if (e.kind === "context") return `get_context · ${e.ref.replace(/^get_context\./, "")}`;
  return e.ref;
}

/**
 * The quiet line under a row: where the rule's truth lives and what holds it. Only said where
 * it is true — a CHECKED rule in a repo file gets its result instead, and an EXTERNAL rule its
 * NOT VERIFIED box.
 */
export function quietLine(rule: RuleFlowRule): string | null {
  if (!rule.used) return null;
  if (rule.enforcement === "external") return null;
  if (rule.enforcement === "checked" && rule.truth !== "repo_file") return null;
  if (rule.channel === "context" && rule.enforcement === "advisory") {
    return "Told to agents in every get_context. Nothing refuses a violation.";
  }
  if (rule.truth === "repo_file" && rule.enforcement === "checked") {
    return `Checked from ${rule.enforcer.ref || "a repo file"}, so it is changed there, not here.`;
  }
  if (rule.truth === "code") return "Held in code. Change it with a PR, not here.";
  if (rule.truth === "repo_file" && rule.enforcement === "advisory") {
    if (rule.enforcer.kind === "skill") return "Written in the review skill. Nothing checks it.";
    return `Written in ${rule.enforcer.ref}. Nothing checks it.`;
  }
  return null;
}

export function externalText(rule: RuleFlowRule): string {
  if (rule.enforcer.kind === "supervisor") {
    return "Held by the gbfleet supervisor on the agent's machine. Graphban can't verify it — some vendor rows can't honour a cap at all.";
  }
  if (rule.enforcer.kind === "forge") {
    return "Held by the forge's branch protection. Graphban can't verify the forge would refuse a merge without it.";
  }
  return "Held outside Graphban. Graphban can't verify it.";
}

export function sourceLabel(source: string | null): string {
  if (source === "project") return "PROJECT";
  if (source === "org") return "ORG";
  return "UNMEASURED";
}

/** A library rule's toggle source (PRD-P63 D4). Never UNMEASURED: the platform default is a value. */
export function toggleSourceLabel(source: string | null | undefined): string {
  if (source === "project") return "PROJECT";
  if (source === "org") return "ORG";
  return "PLATFORM DEFAULT";
}

export function valueText(value: string | boolean | null): string | null {
  if (value === null || value === "") return null;
  return typeof value === "boolean" ? String(value) : value;
}

// ── Layout (fixed, from `Rules.dc.html`) ───────────────────────────────────────

export const STAGE_W = 208;
export const STAGE_H = 128;
export const STAGE_Y = 56;
export const CHIP_W = 192;
export const CHIP_H = 50;
export const CHIP_Y = 208;
export const CHIP_STEP = 58;
export const CHIPS_SHOWN = 3;
export const PACK_W = 368;
export const PACK_H = 64;
/** Below this zoom the cards drop to stage name and pill (frame 1d). */
export const OUTLINE_ZOOM = 0.55;

export const stageX = (i: number) => 24 + i * 240;

export function columnBottom(n: number): number {
  if (n === 0) return STAGE_Y + STAGE_H;
  if (n > CHIPS_SHOWN) return CHIP_Y + CHIPS_SHOWN * CHIP_STEP + 26;
  return CHIP_Y + (n - 1) * CHIP_STEP + CHIP_H;
}

export interface MapLayout {
  width: number;
  height: number;
  /** Top-left of the pack node, under the stage it hangs from. */
  pack: { x: number; y: number; stageIndex: number } | null;
}

export function mapLayout(flow: RulesFlow): MapLayout {
  const n = flow.stages.length;
  const width = Math.max(stageX(n) - 8, 600);
  const bottom = Math.max(STAGE_Y + STAGE_H, ...flow.stages.map((s) => columnBottom(stageRules(s).length)));
  const pack = packOf(flow);
  if (!pack) return { width, height: bottom + 40, pack: null };
  const i = Math.max(0, flow.stages.findIndex((s) => s.id === pack.stage));
  const x = Math.min(Math.max(16, stageX(i) + STAGE_W / 2 - PACK_W / 2), width - PACK_W - 16);
  const y = bottom + 62;
  return { width, height: y + PACK_H + 26, pack: { x, y, stageIndex: i } };
}
