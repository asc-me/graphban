import * as React from "react";

import type { PlanLimits, Usage } from "@/lib/types";

/**
 * Shared vocabulary for the operator plane (PRD-21).
 *
 * The console is drawn from a handful of primitives — pill, stat card, meter, drawer —
 * so four screens read as one surface. They live here rather than in `components/ui`
 * because they carry the operator plane's cooler chrome (`op-*` tokens), which exists
 * precisely so a cross-tenant view can never be mistaken for a tenant one.
 */

/** The plans the server accepts (`services/quotas.PLANS`), in ascending order. */
export const PLANS = ["free", "pro", "team", "enterprise"] as const;

// Each plan's chip/selected-button treatment. `free` carries a tint of its own rather
// than the bare border it shares with an unselected button — otherwise the *current*
// plan of a free org is indistinguishable from the three it is not on.
export const PLAN_TONE: Record<string, string> = {
  free: "text-op-fg-2 border-op-muted/40 bg-op-muted/[0.12]",
  pro: "text-st-next border-st-next/30 bg-st-next/[0.07]",
  team: "text-accent border-accent/30 bg-accent/[0.07]",
  enterprise: "text-purple border-purple/30 bg-purple/[0.07]",
};

/** Short label for a plan chip — "enterprise" is too wide for a 9px pill. */
export function planLabel(plan: string): string {
  return plan === "enterprise" ? "ENT" : plan.toUpperCase();
}

// ── time ───────────────────────────────────────────────────────────────────
/**
 * Parse a server timestamp, treating a zone-less one as UTC.
 *
 * Postgres returns `…+00:00`, but SQLite hands the same column back naive — and JS
 * reads a zone-less ISO string as *local* time. West of UTC that puts every timestamp
 * in the future, so `Date.now() - then` goes negative and the whole console reports
 * "0s ago" for rows that are hours old. Found by running the plane against a live
 * hosted instance; no test would have caught it, because the fixtures all carry a Z.
 */
function parseTs(iso: string): number {
  const zoned = /(Z|[+-]\d{2}:?\d{2})$/.test(iso);
  return new Date(zoned ? iso : `${iso}Z`).getTime();
}

/**
 * Relative time, or null when there is no timestamp.
 *
 * Returning null rather than a dash is deliberate: every caller then has to decide
 * what the absence means in its own column, instead of all of them rendering the same
 * neutral character for "never happened", "not recorded", and "not applicable".
 */
export function relTime(iso: string | null | undefined): string | null {
  if (!iso) return null;
  const s = Math.max(0, Math.floor((Date.now() - parseTs(iso)) / 1000));
  if (s < 60) return `${s}s ago`;
  if (s < 3600) return `${Math.floor(s / 60)}m ago`;
  if (s < 86400) return `${Math.floor(s / 3600)}h ago`;
  return `${Math.floor(s / 86400)}d ago`;
}

export function shortDate(iso: string | null | undefined): string {
  if (!iso) return "—";
  return new Date(parseTs(iso)).toISOString().slice(0, 10);
}

export function compact(n: number): string {
  if (n >= 1_000_000) return `${Math.round(n / 100_000) / 10}M`;
  if (n >= 1_000) return `${Math.round(n / 1000)}k`;
  return String(n);
}

// ── quota meters ───────────────────────────────────────────────────────────
export type Level = "ok" | "near" | "at";

/** Where a counter sits against its cap. At-cap is a refusal, not a warning. */
export function level(used: number, limit: number): Level {
  if (limit <= 0) return "ok";
  const pct = (used / limit) * 100;
  return pct >= 100 ? "at" : pct >= 80 ? "near" : "ok";
}

export const LEVEL_TEXT: Record<Level, string> = {
  ok: "text-op-muted-2",
  near: "text-st-review",
  at: "text-st-blocked",
};

const LEVEL_BAR: Record<Level, string> = {
  ok: "bg-st-done",
  near: "bg-st-review",
  at: "bg-st-blocked",
};

export function Meter({ used, limit, className = "" }: { used: number; limit: number; className?: string }) {
  const pct = limit > 0 ? Math.min(100, Math.round((used / limit) * 100)) : 0;
  return (
    <span className={`block h-1 overflow-hidden rounded-sm bg-op-line-2 ${className}`}>
      <span className={`block h-full ${LEVEL_BAR[level(used, limit)]}`} style={{ width: `${pct}%` }} />
    </span>
  );
}

/** The four counters that exist. Named once so no screen can invent a fifth. */
export const COUNTERS: { key: keyof Usage; limit: keyof PlanLimits; label: string }[] = [
  { key: "seats", limit: "max_seats", label: "SEATS" },
  { key: "projects", limit: "max_projects", label: "PROJECTS" },
  { key: "shards", limit: "max_shards", label: "SHARDS" },
  { key: "calls_this_month", limit: "max_calls_per_month", label: "MCP / MO" },
];

/** The worst level across every counter — what puts an org on the home page's watch. */
export function worstLevel(usage: Usage, limits: PlanLimits): Level {
  let worst: Level = "ok";
  for (const c of COUNTERS) {
    const l = level(usage[c.key], limits[c.limit]);
    if (l === "at") return "at";
    if (l === "near") worst = "near";
  }
  return worst;
}

// ── chrome ─────────────────────────────────────────────────────────────────
export function Card({ children, className = "" }: { children: React.ReactNode; className?: string }) {
  return (
    <section className={`rounded-[13px] border border-op-line bg-op-card ${className}`}>{children}</section>
  );
}

export function CardHead({
  icon,
  title,
  right,
}: {
  icon?: React.ReactNode;
  title: string;
  right?: React.ReactNode;
}) {
  return (
    <div className="flex items-center gap-2.5 border-b border-op-line-2 px-4 py-3">
      {icon}
      <h2 className="flex-1 text-[14px] font-semibold">{title}</h2>
      {right}
    </div>
  );
}

export function Pill({
  tone,
  label,
  dashed = false,
}: {
  tone: string;
  label: string;
  dashed?: boolean;
}) {
  return (
    <span
      className={`inline-flex items-center gap-1.5 rounded-full border px-2 py-0.5 ${
        dashed ? "border-dashed" : ""
      } ${tone}`}
    >
      <span className="h-[5px] w-[5px] shrink-0 rounded-full bg-current" />
      <span className="font-mono text-[9px] uppercase tracking-[0.05em]">{label}</span>
    </span>
  );
}

/**
 * An empty state that names which kind of empty it is.
 *
 * `title` says what is absent and `body` says why that is or is not a problem — the
 * product's own rule, applied to its console: "nothing here" and "nobody has looked"
 * are different facts and must never render the same way.
 */
export function Empty({ title, body }: { title: string; body: React.ReactNode }) {
  return (
    <div className="rounded-[13px] border border-op-line bg-op-card px-5 py-8 text-center">
      <div className="text-[14px] font-semibold">{title}</div>
      <p className="mx-auto mt-1.5 max-w-[50ch] text-[12px] leading-relaxed text-op-muted-2">{body}</p>
    </div>
  );
}

export function PageHead({
  title,
  chip,
  lede,
  right,
}: {
  title: string;
  chip?: React.ReactNode;
  lede?: React.ReactNode;
  right?: React.ReactNode;
}) {
  return (
    <div className="mb-4 flex items-start gap-3">
      <div className="min-w-0 flex-1">
        <div className="flex flex-wrap items-center gap-2.5">
          <h1 className="text-[19px] font-semibold tracking-[-0.3px]">{title}</h1>
          {chip}
        </div>
        {lede && (
          <p className="mt-1.5 max-w-[78ch] text-[12.5px] leading-relaxed text-op-muted-2">{lede}</p>
        )}
      </div>
      {right && <div className="shrink-0">{right}</div>}
    </div>
  );
}

/** Table shell: one horizontal scroll container, one min-width, set by the caller. */
export function Table({ minWidth, children }: { minWidth: number; children: React.ReactNode }) {
  return (
    <div className="overflow-x-auto rounded-[13px] border border-op-line bg-op-card">
      <div style={{ minWidth }}>{children}</div>
    </div>
  );
}

export function Th({ children, className = "" }: { children?: React.ReactNode; className?: string }) {
  return <span className={`font-mono text-[9px] uppercase tracking-[0.07em] ${className}`}>{children}</span>;
}

export function Callout({
  tone = "info",
  icon,
  children,
}: {
  tone?: "info" | "warn";
  icon?: React.ReactNode;
  children: React.ReactNode;
}) {
  const cls =
    tone === "warn"
      ? "border-st-review/30 bg-st-review/[0.06]"
      : "border-op-line-2 bg-op-inset";
  return (
    <div className={`flex gap-3 rounded-[13px] border px-3.5 py-3 ${cls}`}>
      {icon && <span className="mt-0.5 shrink-0">{icon}</span>}
      <div className="min-w-0 flex-1 text-[12px] leading-relaxed text-op-muted-2">{children}</div>
    </div>
  );
}

/** Deterministic avatar tint from a handle — identity you can scan for, not decoration. */
const AVATAR_TINTS = ["#c6f24e", "#7ca2ff", "#a78bfa", "#e0b34a", "#5fd07a", "#c9b8ff"];

export function tintFor(seed: string): string {
  let n = 0;
  for (let i = 0; i < seed.length; i++) n = (n * 31 + seed.charCodeAt(i)) >>> 0;
  return AVATAR_TINTS[n % AVATAR_TINTS.length];
}

function initialsOf(name: string, fallback: string): string {
  const parts = name.trim().split(/\s+/).filter(Boolean);
  if (!parts.length) return fallback.replace(/^@/, "").slice(0, 2).toUpperCase();
  return parts.map((w) => w[0]).join("").slice(0, 2).toUpperCase();
}

export function Avatar({ name, handle }: { name: string; handle: string }) {
  const tint = tintFor(handle || name);
  return (
    <span
      className="flex h-[26px] w-[26px] shrink-0 items-center justify-center rounded-full border font-mono text-[10px] font-semibold"
      style={{ background: `${tint}1f`, borderColor: `${tint}4d`, color: tint }}
    >
      {initialsOf(name, handle)}
    </span>
  );
}

export const ROLE_TONE: Record<string, string> = {
  owner: "text-accent",
  admin: "text-st-review",
  member: "text-op-muted-2",
};
