import type {
  MemoryQueueKey,
  ScoredCandidate,
  Shard,
  ShardCluster,
  StaleShardRow,
} from "@/lib/types";

/**
 * The Memory triage console's model (GRPH-1176): five queues, one row shape, and the
 * search / filter / sort that runs over them. Pure, so the page stays about rendering.
 */

export type QueueId = "needs" | "conflicts" | "dupes" | "stale" | "unvetted";

export interface QueueDef {
  id: QueueId;
  /** The key `/memory/queues` counts it under (GRPH-1175). */
  key: MemoryQueueKey;
  label: string;
  hint: string;
  dot: string;
  ring: string;
  tag: string;
}

export const QUEUES: QueueDef[] = [
  {
    id: "needs", key: "needs_decision", label: "Needs a decision",
    hint: "Candidates the scorer left to a person",
    dot: "bg-accent", ring: "border-accent/45", tag: "border-accent/45 bg-accent/10 text-accent",
  },
  {
    id: "conflicts", key: "conflicts", label: "Conflicts",
    hint: "Contradicts published memory",
    dot: "bg-st-blocked", ring: "border-st-blocked/40", tag: "border-st-blocked/30 bg-st-blocked/[0.07] text-ty-bug",
  },
  {
    id: "dupes", key: "duplicate_clusters", label: "Duplicate clusters",
    hint: "Near-duplicate candidates, merged once",
    dot: "bg-st-review", ring: "border-st-review/40", tag: "border-st-review/30 bg-st-review/[0.08] text-st-review",
  },
  {
    id: "stale", key: "stale_or_wrong", label: "Stale or wrong",
    hint: "Uncorroborated, reported wrong, or code gone",
    dot: "bg-muted", ring: "border-line-hover", tag: "border-line-2 text-muted",
  },
  {
    id: "unvetted", key: "published_unreviewed", label: "Published, unreviewed",
    hint: "Auto-published · no human look",
    dot: "bg-purple", ring: "border-purple/40", tag: "border-purple/35 bg-purple/[0.08] text-purple-2",
  },
];

export const QUEUE_BY_ID = Object.fromEntries(QUEUES.map((q) => [q.id, q])) as Record<QueueId, QueueDef>;

/** One row in any queue. `shard` is what an action acts on — a cluster's representative. */
export interface Row {
  id: string;
  queue: QueueId;
  shard: Shard;
  text: string;
  agent: string;
  source: string;
  conf: number | null;
  createdMs: number | null;
  scored?: ScoredCandidate;
  cluster?: ShardCluster;
  stale?: StaleShardRow;
}

function createdMs(s: Shard): number | null {
  if (!s.created_at) return null;
  const t = new Date(s.created_at).getTime();
  return Number.isNaN(t) ? null : t;
}

function base(shard: Shard, queue: QueueId, conf: number | null): Row {
  return {
    id: shard.id, queue, shard, text: shard.text, agent: shard.origin || "agent",
    source: shard.source || shard.item_id || "", conf, createdMs: createdMs(shard),
  };
}

export interface QueueSources {
  scored?: ScoredCandidate[];
  clusters?: ShardCluster[];
  stale?: StaleShardRow[];
  auto?: Shard[];
}

/** Rows per queue; `undefined` means that queue's source has not answered (or failed). */
export function buildRows(src: QueueSources): Record<QueueId, Row[] | undefined> {
  const scoreById = new Map((src.scored ?? []).map((s) => [s.shard.id, s]));
  const fromScored = (s: ScoredCandidate, q: QueueId): Row => ({ ...base(s.shard, q, s.confidence), scored: s });
  return {
    // Same population `/memory/queues` counts: every scored candidate; conflicts are a subset.
    needs: src.scored?.map((s) => fromScored(s, "needs")),
    conflicts: src.scored?.filter((s) => (s.conflicts?.length ?? 0) > 0).map((s) => fromScored(s, "conflicts")),
    dupes: src.clusters?.map((c) => ({
      ...base(c.representative, "dupes", scoreById.get(c.representative.id)?.confidence ?? null),
      cluster: c,
      scored: scoreById.get(c.representative.id),
    })),
    stale: src.stale?.map((r) => ({ ...base(r.shard, "stale", r.shard.auto_confidence), stale: r })),
    unvetted: src.auto
      ?.filter((s) => s.status === "published")
      .map((s) => base(s, "unvetted", s.auto_confidence)),
  };
}

/** "Everywhere": one list across every queue that answered, each row once. The most specific
 *  queue wins — a conflicting candidate is also in Needs a decision, and reads as a conflict. */
const SEARCH_ORDER: QueueId[] = ["conflicts", "dupes", "stale", "unvetted", "needs"];

export function everywhere(rows: Record<QueueId, Row[] | undefined>): Row[] {
  const seen = new Set<string>();
  const out: Row[] = [];
  for (const q of SEARCH_ORDER) {
    for (const r of rows[q] ?? []) {
      if (seen.has(r.id)) continue;
      seen.add(r.id);
      out.push(r);
    }
  }
  return out;
}

export const CONF_BANDS = ["≥ 0.90", "0.55–0.90", "< 0.55"] as const;
export const AGE_BANDS = ["< 24h", "1–7d", "> 7d"] as const;
export type ConfBand = (typeof CONF_BANDS)[number];
export type AgeBand = (typeof AGE_BANDS)[number];

/** A row the scorer never scored has no band — it matches no confidence chip. */
export function confBand(c: number | null): ConfBand | null {
  if (c == null) return null;
  return c >= 0.9 ? "≥ 0.90" : c >= 0.55 ? "0.55–0.90" : "< 0.55";
}

const DAY = 86_400_000;

export function ageBand(ms: number | null, now: number): AgeBand | null {
  if (ms == null) return null;
  const d = (now - ms) / DAY;
  return d < 1 ? "< 24h" : d <= 7 ? "1–7d" : "> 7d";
}

export function formatAge(ms: number | null, now: number): string {
  if (ms == null) return "—";
  const h = (now - ms) / 3_600_000;
  return h < 24 ? `${Math.max(1, Math.round(h))}h` : `${Math.round(h / 24)}d`;
}

export interface Facets {
  agent?: string;
  conf?: ConfBand;
  age?: AgeBand;
}

export type SortId = "conf" | "new" | "old";

export function filterRows(rows: Row[], text: string, f: Facets, now: number): Row[] {
  const needle = text.trim().toLowerCase();
  return rows.filter(
    (r) =>
      (!needle || `${r.text} ${r.id} ${r.agent} ${r.source}`.toLowerCase().includes(needle)) &&
      (!f.agent || r.agent === f.agent) &&
      (!f.conf || confBand(r.conf) === f.conf) &&
      (!f.age || ageBand(r.createdMs, now) === f.age),
  );
}

/** Unknown confidence or age sorts last either way: it is not the lowest value, it is none. */
export function sortRows(rows: Row[], sort: SortId): Row[] {
  const last = (v: number | null, dir: number) => (v == null ? Infinity : dir * v);
  const out = [...rows];
  if (sort === "conf") out.sort((a, b) => last(a.conf, -1) - last(b.conf, -1));
  else if (sort === "new") out.sort((a, b) => last(a.createdMs, -1) - last(b.createdMs, -1));
  else out.sort((a, b) => last(a.createdMs, 1) - last(b.createdMs, 1));
  return out;
}

/** Published with nobody looking (AL-280 trusted / AL-282 agent). */
export const UNVETTED_SOURCES = ["trusted", "agent"];

export const SOURCE_LABEL: Record<string, string> = {
  trusted: "no review",
  agent: "agent + judge",
  llm: "llm scorer",
  similarity: "similarity",
  decider: "decider",
};

/** What graded a candidate, in words — an ungraded row must not read as a low score. */
export function judgeWords(s: ScoredCandidate | undefined): string | null {
  if (!s) return null;
  if (s.judged) {
    return [
      s.judge_source === "decider" ? "decider" : null,
      s.grounded ? "grounded" : "ungrounded",
      s.ready ? "ready" : "not ready",
    ].filter(Boolean).join(" · ");
  }
  const why = s.ungraded_reason ?? "";
  if (!why) return null;
  if (why.startsWith("not scored yet")) return "not scored yet";
  if (why.includes("abstain band")) return "undecided";
  return `not judged — ${why}`;
}
