/**
 * Activity's pure helpers (PRD-47 S10 / GRPH-961) — kept out of the components so they
 * can be tested without rendering, and so the words a row shows come from the payload
 * rather than from a second copy of the backend's enums.
 */
import type {
  ActivityHistogram,
  ActivityLens,
  ActivityRange,
  Event,
  HistogramBucket,
} from "@/lib/types";

export const RANGE_CHOICES: { id: ActivityRange; label: string }[] = [
  { id: "1h", label: "1h" },
  { id: "24h", label: "24h" },
  { id: "7d", label: "7d" },
  { id: "30d", label: "30d" },
];

/** The stacked histogram's series, in stack order. `system` is last and only drawn when
 *  non-zero: auto-triage and webhooks record `actor_type="system"`, and putting a
 *  machine's decision in the human bar would attribute it to a person. */
export const SERIES = [
  { key: "agent", label: "Agent", bar: "bg-purple" },
  { key: "human", label: "Human", bar: "bg-st-done" },
  { key: "rejected", label: "Rejected", bar: "bg-st-blocked" },
  { key: "system", label: "System", bar: "bg-st-review" },
] as const;

export type SeriesKey = (typeof SERIES)[number]["key"];

export function bucketTotal(b: HistogramBucket): number {
  return SERIES.reduce((n, s) => n + b[s.key], 0);
}

/**
 * The refusal actions the ledger records — read from the payload, never copied here.
 *
 * `null` means the server did not say, which is NOT the same as an empty list: with no
 * coverage declared, every verdict below is `unknown` rather than a confident `ok`.
 */
export function refusalActions(lenses: ActivityLens[] | undefined): string[] | null {
  const rejected = lenses?.find((l) => l.id === "rejected");
  return rejected?.covers ?? null;
}

export type Verdict = "ok" | "rejected" | "unknown";

export function verdictOf(e: Event, refusals: string[] | null): Verdict {
  if (refusals === null) return "unknown";
  return refusals.includes(e.action) ? "rejected" : "ok";
}

export const VERDICT_TITLE: Record<Verdict, string> = {
  ok: "Recorded as an accepted mutation. Only refusals the ledger records appear as rejected.",
  rejected: "One of the refusal kinds the ledger records.",
  unknown: "The payload did not declare which actions count as refusals, so this row's verdict is unknown rather than ok.",
};

// ── Grouping ────────────────────────────────────────────────────────────────

export type GroupBy = "time" | "target";

export interface EventGroup {
  key: string;
  label: string;
  rows: Event[];
}

/** Rows arrive newest-first and grouping preserves that order — it labels runs, it does
 *  not re-sort them, so a group header always describes the rows under it. */
export function groupEvents(rows: Event[], by: GroupBy, range: ActivityRange): EventGroup[] {
  const out: EventGroup[] = [];
  let current: EventGroup | null = null;
  for (const row of rows) {
    const key = by === "time" ? timeKey(row, range) : targetKey(row);
    const label = by === "time" ? timeLabel(row, range) : targetLabel(row);
    if (!current || current.key !== key) {
      current = { key, label, rows: [] };
      out.push(current);
    }
    current.rows.push(row);
  }
  return out;
}

/** 1h buckets by minute — a whole hour under one "today" header is not a grouping. */
function timeParts(e: Event, range: ActivityRange): { key: string; label: string } | null {
  if (!e.ts) return null;
  const d = new Date(e.ts);
  if (Number.isNaN(d.getTime())) return null;
  const day = d.toLocaleDateString(undefined, { month: "short", day: "numeric" });
  if (range === "1h") {
    const minute = d.toLocaleTimeString(undefined, { hour: "numeric", minute: "2-digit" });
    return { key: `${day} ${minute}`, label: minute };
  }
  const hour = d.toLocaleTimeString(undefined, { hour: "numeric" });
  return {
    key: range === "24h" ? `${day} ${hour}` : day,
    label: range === "24h" ? `${day}, ${hour}` : day,
  };
}

function timeKey(e: Event, range: ActivityRange): string {
  return timeParts(e, range)?.key ?? "undated";
}

function timeLabel(e: Event, range: ActivityRange): string {
  // An event with no readable timestamp still gets a group, named for what it is rather
  // than folded silently into the run above it.
  return timeParts(e, range)?.label ?? "No readable timestamp";
}

/** `target_type` is empty for most MCP writes. That is a value, not a missing row. */
export function targetLabel(e: Event): string {
  const kind = e.target_type || "untyped";
  return e.target_id ? `${kind} · ${e.target_id}` : kind;
}

function targetKey(e: Event): string {
  return `${e.target_type}\u0000${e.target_id}`;
}

// ── The row's summary line ──────────────────────────────────────────────────

const META_DETAIL_MAX = 120;

function truncateMeta(s: string): string {
  return s.length <= META_DETAIL_MAX ? s : `${s.slice(0, META_DETAIL_MAX - 1)}…`;
}

function isRecord(v: unknown): v is Record<string, unknown> {
  return typeof v === "object" && v !== null && !Array.isArray(v);
}

/** Evidence receipts and other nested meta values — never `String(object)`. */
function formatMetaValue(v: unknown): string {
  if (Array.isArray(v)) return v.map(formatMetaValue).join(", ");
  if (isRecord(v)) {
    const kind = typeof v.kind === "string" ? v.kind : "";
    const detail = typeof v.detail === "string" ? v.detail : "";
    if (kind || detail) {
      if (kind && detail) return `${kind} — ${truncateMeta(detail)}`;
      return truncateMeta(kind || detail);
    }
    return Object.entries(v)
      .map(([k, val]) => `${k}: ${formatMetaValue(val)}`)
      .join(", ");
  }
  return String(v);
}

export function summarizeMeta(meta: Record<string, unknown> | null): string {
  if (!meta) return "";
  return Object.entries(meta)
    .filter(([k]) => k !== "principal" && k !== "origin" && k !== "agent_id") // shown in the header
    .map(([k, v]) => `${k}: ${formatMetaValue(v)}`)
    .join(" · ");
}

// ── Time ────────────────────────────────────────────────────────────────────

export function relTime(iso: string | null): string {
  if (!iso) return "";
  const then = new Date(iso).getTime();
  if (Number.isNaN(then)) return "";
  const s = Math.max(0, Math.floor((Date.now() - then) / 1000));
  if (s < 60) return `${s}s ago`;
  if (s < 3600) return `${Math.floor(s / 60)}m ago`;
  if (s < 86400) return `${Math.floor(s / 3600)}h ago`;
  return `${Math.floor(s / 86400)}d ago`;
}

export function absTime(iso: string | null): string {
  if (!iso) return "no timestamp recorded";
  const d = new Date(iso);
  return Number.isNaN(d.getTime()) ? "unreadable timestamp" : d.toLocaleString();
}

/** One bar's width in words, for the axis and the bucket chip. */
export function bucketWidthLabel(seconds: number | null): string {
  if (!seconds || seconds < 1) return "";
  if (seconds < 60) return `${seconds}s`;
  if (seconds < 3600) return `${Math.round(seconds / 60)} min`;
  if (seconds < 86400) return `${trim(seconds / 3600)}h`;
  return `${trim(seconds / 86400)}d`;
}

function trim(n: number): string {
  return Number.isInteger(n) ? String(n) : n.toFixed(1);
}

export function bucketRangeLabel(b: HistogramBucket): string {
  const from = new Date(b.start);
  const to = new Date(b.end);
  if (Number.isNaN(from.getTime()) || Number.isNaN(to.getTime())) return `bar ${b.index + 1}`;
  const fmt = (d: Date) =>
    d.toLocaleString(undefined, { month: "short", day: "numeric", hour: "numeric", minute: "2-digit" });
  return `${fmt(from)} → ${fmt(to)}`;
}

/** What the histogram's coverage line says. Absent is not the same as quiet. */
export function coverageNote(h: ActivityHistogram): string | null {
  if (h.coverage === "not_requested") return "No range selected — there are no bars to draw.";
  if (h.coverage === "partial")
    return `Bars cover the newest ${h.scanned.toLocaleString()} events in range; the window holds more than one read returns.`;
  return null;
}

// ── CSV export ──────────────────────────────────────────────────────────────

const CSV_COLUMNS = [
  "id", "ts", "verdict", "actor_type", "actor_id", "actor_label", "principal", "agent",
  "surface", "action", "target_type", "target_id", "project_id", "summary",
] as const;

function csvCell(v: string | number | null | undefined): string {
  const s = v === null || v === undefined ? "" : String(v);
  return /[",\n\r]/.test(s) ? `"${s.replace(/"/g, '""')}"` : s;
}

/**
 * The rows in the view, as CSV. `refusals` is passed through so the exported verdict
 * column can say `unknown` when the payload never declared its coverage — an export that
 * quietly stamped every row `ok` would carry the tile's defect into a spreadsheet.
 */
export function toCsv(rows: Event[], refusals: string[] | null): string {
  const lines = [CSV_COLUMNS.join(",")];
  for (const e of rows) {
    lines.push([
      e.id, e.ts ?? "", verdictOf(e, refusals), e.actor_type, e.actor_id, e.actor_label,
      e.principal ?? "", e.agent ?? "", e.surface, e.action, e.target_type, e.target_id,
      e.project_id ?? "", summarizeMeta(e.meta),
    ].map(csvCell).join(","));
  }
  return `${lines.join("\r\n")}\r\n`;
}
