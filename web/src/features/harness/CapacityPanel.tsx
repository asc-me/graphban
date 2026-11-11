import { useState } from "react";

import { cn } from "@/lib/cn";
import { useHarnessCapacity, useMe } from "@/lib/queries";
import { consoleHref } from "@/lib/routes";
import type { HarnessCapacity, HarnessCapacityEntitlement, HarnessCapacityRunner, HarnessRoutingDecisions } from "@/lib/types";
import { BurnCard, StatePill, burnVerdict } from "./BurnDown";
import { STATE, fmtTokens, shortDay, shortWhen, type CapState } from "./capacityLook";
import { Ladder } from "./Ladder";
import { ReviewReserve } from "./ReviewReserve";
import { RoutingDecisions } from "./RoutingDecisions";
import { RoutingMode } from "./RoutingMode";
import { RoutingReason } from "./RoutingReason";

/**
 * PRD-62 S5/D8 — the Capacity tab: a header, a summary strip, then three views. Overview is the
 * burn-down per budgeted entitlement; Entitlements is the table, worst first; Routing is the
 * rule, what the router did, the review reserve, the ladder and one item's reason.
 *
 * Every absence says why: a period the server cannot place gives its reason, an unreported
 * spend is a count (and makes figures a floor), a count the supervisor does not record reads
 * "not recorded" rather than 0. A 402 (org off a capacity plan) shows the server's own message
 * and hint, never a hidden tab. Forecasts and verdicts come from the server; this only draws them.
 */
const runnerName = (r: HarnessCapacityRunner) => r.label || r.agent_id;
const entitlementsHref = (org: string) => consoleHref(`/orgs/${encodeURIComponent(org)}/settings/entitlements`);

/** The 402 envelope is `{detail, rule_id, hint}`; `request()` throws with the raw body as message. */
function serverRefusal(err: unknown): { message: string; hint: string | null } {
  const raw = err && typeof err === "object" && "message" in err ? String((err as { message: unknown }).message) : "";
  try {
    const p = JSON.parse(raw);
    if (p && typeof p.detail === "string") return { message: p.detail, hint: typeof p.hint === "string" ? p.hint : null };
  } catch {
    /* not JSON */
  }
  return { message: raw || "Capacity is not available.", hint: null };
}

type Placed = Extract<NonNullable<HarnessCapacityEntitlement["burn_down"]>, { available: true }>;
const placed = (e: HarnessCapacityEntitlement): e is HarnessCapacityEntitlement & { burn_down: Placed } =>
  e.burn_down?.available === true;
const runsOutEarly = (e: HarnessCapacityEntitlement) =>
  placed(e) && (e.burn_down.forecast.verdict === "runs_out_early" || e.burn_down.forecast.verdict === "run_out");

/** The one line beside the state pill. Quotes are the vendor's words; nothing is paraphrased. */
function stateNote(e: HarnessCapacityEntitlement): { testid: string; text: string } {
  switch (e.state) {
    case "exhausted":
      return { testid: "capacity-exhausted", text: `“${e.vendor_text}”${e.seen_at ? ` · seen ${shortWhen(e.seen_at)}` : ""}` };
    case "broken":
      return {
        testid: "capacity-runner",
        text: `“${e.vendor_text}” · reported by ${e.reported_by ? runnerName(e.reported_by) : "unknown runner"}`
          + (e.declared_by.length > 0 ? ` · declared by ${e.declared_by.map(runnerName).join(", ")}` : ""),
      };
    case "unknown":
      return { testid: "capacity-unknown", text: "Never measured. Ranked below available." };
    case "unreachable": {
      const last = e.last_known_state ? `last known ${STATE[e.last_known_state].word}` : "last known state not served";
      return { testid: "capacity-unreachable", text: `${e.runner?.label ?? "The runner"} has gone quiet · ${last}` };
    }
    default:
      return { testid: "capacity-measured", text: "measured" };
  }
}

/** When it next resets: the burn-down's placed end, else the owner's stated time, else the cadence alone. */
function resetText(e: HarnessCapacityEntitlement): { text: string; stated: boolean } {
  const next = placed(e) ? e.burn_down.end : e.next_reset_at ?? null;
  const cadence = e.reset_cadence ?? "";
  if (next) return { text: `${cadence ? `${cadence} · ` : ""}${shortWhen(next)}`, stated: true };
  return { text: cadence ? `${cadence} · next reset not stated` : "no reset stated", stated: false };
}

const COLS = "minmax(0,1.5fr) minmax(0,1.7fr) 88px minmax(0,1fr) minmax(0,0.9fr)";

function Row({ e }: { e: HarnessCapacityEntitlement }) {
  const note = stateNote(e);
  const reset = resetText(e);
  const sub = [e.kind, e.plan_label, e.runner?.label].filter(Boolean).join(" · ");
  const b = placed(e) ? e.burn_down : null;
  const early = runsOutEarly(e);
  const projection = b
    ? burnVerdict(b).text
    : e.burn_down && !e.burn_down.available
      ? e.burn_down.reason
      : e.projected_exhaustion.at
        ? `runs out ~${shortWhen(e.projected_exhaustion.at)}`
        : e.projected_exhaustion.reason;
  return (
    <div
      role="row"
      data-testid="capacity-row"
      data-state={e.state}
      className="grid items-center gap-3.5 border-t border-line-2/70 px-3.5 py-2.5"
      style={{ gridTemplateColumns: COLS }}
    >
      <span role="cell" className="flex min-w-0 flex-col gap-0.5">
        <span className="break-all font-mono text-fg">{e.id}</span>
        <span data-testid="capacity-runner-cell" className="font-mono text-[10.5px] text-faint">{sub || e.vendor}</span>
      </span>
      <span role="cell" className="flex min-w-0 items-center gap-2">
        <StatePill state={e.state} />
        <span data-testid={note.testid} title={note.text} className="truncate text-small text-muted">{note.text}</span>
      </span>
      <span role="cell" data-testid="capacity-week" className={cn("font-mono", e.week.reported ? "text-fg" : "text-faint")}>
        {e.week.reported ? fmtTokens(e.week.tokens) : "—"}
        {e.week.unreported > 0 && <span className="block text-meta text-faint">+{e.week.unreported} unreported</span>}
      </span>
      <span role="cell" className="flex min-w-0 flex-col gap-1 font-mono" title={e.projected_exhaustion.basis}>
        <span data-testid="capacity-budget" className={e.token_budget === null ? "text-faint" : "text-fg"}>
          {b ? `${fmtTokens(b.left)} of ${fmtTokens(b.budget)}` : e.token_budget === null ? "no budget stated" : `${fmtTokens(e.token_budget)} budget`}
        </span>
        {b && (
          <span className="block h-[3px] overflow-hidden rounded-[2px] bg-line-2">
            <span
              data-testid="capacity-budget-bar"
              className={cn("block h-full", e.state === "broken" ? "bg-[#5c656e]" : early ? "bg-[#f0b450]" : "bg-st-done")}
              style={{ width: `${Math.max(0, Math.min(1, b.left / b.budget)) * 100}%` }}
            />
          </span>
        )}
        <span data-testid="capacity-projection" className="truncate font-sans text-meta text-faint" title={projection}>{projection}</span>
      </span>
      <span role="cell" data-testid="capacity-reset" className={cn("font-mono text-small leading-[1.45]", reset.stated ? "text-fg-2" : "text-muted")}>
        {reset.text}
      </span>
    </div>
  );
}

function Summary({ data, noWeek }: { data: HarnessCapacity; noWeek: boolean }) {
  const ents = data.entitlements;
  const count = (s: CapState) => ents.filter((e) => e.state === s).length;
  const early = ents.filter(runsOutEarly).length;
  const u = data.utilization;
  type Tile = { k: string; g: string; c: string; v: number | string; d: string; vc?: string };
  const routed: Tile = noWeek
    ? { k: "ROUTED LAST WEEK", g: "Σ", c: "text-faint", v: "—", d: "no completed week yet", vc: "text-faint" }
    : u.items_routed === undefined
      ? { k: "ROUTED LAST WEEK", g: "Σ", c: "text-faint", v: "—", d: "not served by this server", vc: "text-faint" }
      : { k: "ROUTED LAST WEEK", g: "Σ", c: "text-muted", v: u.items_routed,
          d: `${u.items_routed === 1 ? "item" : "items"} · ${shortDay(u.week_start)} – ${shortDay(u.week_end)}` };
  const tiles: Tile[] = [
    { k: "AVAILABLE", g: "●", c: "text-st-done", v: count("available"), d: "measured headroom" },
    { k: "RUNS OUT EARLY", g: "↘", c: "text-[#f0b450]", v: early, d: "before its reset", vc: early ? "text-[#f0b450]" : undefined },
    { k: "EXHAUSTED", g: "◒", c: "text-[#ff9f5a]", v: count("exhausted"), d: "vendor limit hit" },
    { k: "BROKEN", g: "✕", c: "text-st-blocked", v: count("broken"), d: "spawns failing", vc: count("broken") ? "text-st-blocked" : undefined },
    { k: "NOT MEASURED", g: "○", c: "text-muted-2", v: count("unknown") + count("unreachable"), d: "unknown or quiet" },
    routed,
  ];
  return (
    <div data-testid="capacity-summary" className="grid grid-cols-[repeat(auto-fit,minmax(130px,1fr))] overflow-hidden rounded-[12px] border border-line-2 bg-surface">
      {tiles.map((t) => (
        <div key={t.k} data-testid="capacity-summary-tile" data-key={t.k} className="flex flex-col gap-[3px] border-r border-line-2/70 px-3.5 py-3 last:border-r-0">
          <span className={cn("flex items-center gap-1.5 font-mono text-meta tracking-[0.6px]", t.c)}>
            <span aria-hidden="true">{t.g}</span>
            {t.k}
          </span>
          <span data-testid="capacity-summary-value" className={cn("font-mono text-title font-medium tracking-[-0.01em]", t.vc ?? "text-fg")}>
            {t.v}
          </span>
          <span className="text-small text-faint">{t.d}</span>
        </div>
      ))}
    </div>
  );
}

/** Last full week, in words: share of stated headroom used and what the supervisor did not record. */
function Rollup({ data, noWeek }: { data: HarnessCapacity; noWeek: boolean }) {
  const u = data.utilization;
  const count = (c: { count: number | null }) => (c.count === null ? "not recorded" : c.count);
  const tail = `Waves ended by capacity: ${count(u.waves_ended_by_capacity)}. Items deferred to a reset: ${count(u.items_deferred_to_reset)}.`;
  if (noWeek) {
    return (
      <p data-testid="capacity-rollup" className="px-0.5 text-small text-muted">
        <span className="font-mono text-meta tracking-[0.6px] text-fg-2">○ NOT ENOUGH DATA</span>{" "}
        “no completed week since capacity routing was enabled” · {tail}
      </p>
    );
  }
  const share = u.share_used === null
    ? "unknown — no entitlement states a token budget"
    : u.share_is_lower_bound
      ? `≥${Math.round(u.share_used * 100)}% of ${fmtTokens(u.budget ?? 0)} tokens (+${u.unreported_spends} unreported)`
      : `${Math.round(u.share_used * 100)}% of ${fmtTokens(u.budget ?? 0)} tokens`;
  return (
    <p data-testid="capacity-rollup" className="px-0.5 text-small text-muted">
      Last full week · {shortDay(u.week_start)} – {shortDay(u.week_end)} · headroom used: {share}. Tokens on budgeted entitlements: {fmtTokens(u.tokens_used)}. {tail}
    </p>
  );
}

type View = "overview" | "ents" | "routing";

function Views({ view, setView, early, ents, draft }: { view: View; setView: (v: View) => void; early: number; ents: number; draft: boolean }) {
  const warn = "border-[rgba(240,180,80,0.4)] text-[#f0b450]";
  const defs: { id: View; label: string; badge: string; tone: string }[] = [
    { id: "overview", label: "Overview", badge: early ? `${early} early` : "", tone: warn },
    { id: "ents", label: "Entitlements", badge: String(ents), tone: "border-line text-muted" },
    { id: "routing", label: "Routing", badge: draft ? "draft" : "", tone: warn },
  ];
  return (
    <div role="tablist" aria-label="Capacity views" className="flex w-fit gap-0.5 rounded-[9px] border border-line-2 bg-[#0d1114] p-[3px]">
      {defs.map((d) => {
        const on = view === d.id;
        return (
          <button
            key={d.id}
            type="button"
            role="tab"
            aria-selected={on}
            data-testid={`capacity-view-${d.id}`}
            onClick={() => setView(d.id)}
            className={cn("inline-flex items-center gap-[7px] rounded-[7px] px-3 py-1.5 text-[12px] font-medium", on ? "bg-line-2 text-fg" : "text-muted hover:text-fg-2")}
          >
            {d.label}
            {d.badge && <span data-testid="capacity-view-badge" className={cn("rounded-full border px-[5px] font-mono text-meta", d.tone)}>{d.badge}</span>}
          </button>
        );
      })}
    </div>
  );
}

function Overview({ data, href }: { data: HarnessCapacity; href: string | null }) {
  const burns = data.entitlements.filter(placed);
  const unplaced = data.entitlements.filter((e) => !placed(e));
  return (
    <div data-testid="capacity-overview" className="flex flex-col gap-2.5">
      <div className="flex flex-wrap items-baseline gap-x-2.5 gap-y-1 px-0.5">
        <h3 className="text-[13.5px] font-semibold">Burn-down</h3>
        <span className="text-[12px] text-muted">
          Budget left in the current period. Dashed is the server’s forecast at the recent rate.
        </span>
        <span className="flex-1" />
        <span className="inline-flex items-center gap-1.5 font-mono text-[10px] text-faint">
          <span className="w-3.5 border-t-2 border-muted-2" />actual
          <span className="ml-1.5 w-3.5 border-t-2 border-dashed border-muted-2" />forecast
          <span className="ml-1.5 w-3.5 border-t border-dashed border-[#2c353c]" />even burn
        </span>
      </div>
      {burns.length > 0 ? (
        <div className="grid grid-cols-[repeat(auto-fill,minmax(270px,1fr))] gap-2.5">
          {burns.map((e) => <BurnCard key={e.id} e={e} />)}
        </div>
      ) : (
        <p data-testid="capacity-burn-none" className="px-0.5 text-[12px] text-muted">
          No entitlement has a period the server can place yet — that needs a token budget, a stated next reset, and a daily, weekly or monthly cadence.
        </p>
      )}
      {unplaced.length > 0 && (
        <div data-testid="capacity-no-budget" className="flex flex-wrap items-center gap-2 rounded-[10px] border border-dashed border-line px-3 py-2">
          <span className="text-[12px] text-muted">No burn-down for:</span>
          {unplaced.map((e) => {
            const s = STATE[e.state];
            const why = e.burn_down && !e.burn_down.available ? e.burn_down.reason : e.projected_exhaustion.reason;
            return (
              <span
                key={e.id}
                data-testid="capacity-no-budget-chip"
                title={`${s.word} · ${why}`}
                className="inline-flex items-center gap-[5px] rounded-[6px] border border-line-2 bg-surface px-[7px] py-0.5 font-mono text-[10.5px] text-fg-2"
              >
                <span aria-hidden="true" className={s.text}>{s.glyph}</span>
                {e.id}
                <span className="text-faint">· {why}</span>
              </span>
            );
          })}
          <span className="flex-1" />
          <a href={href ?? undefined} className="text-small text-accent">State a budget →</a>
        </div>
      )}
    </div>
  );
}

function Entitlements({ data, href }: { data: HarnessCapacity; href: string | null }) {
  const rows = data.entitlements
    .map((e, i) => ({ e, i }))
    .sort((a, b) => STATE[a.e.state].rank - STATE[b.e.state].rank || a.i - b.i)
    .map((r) => r.e);
  return (
    <div className="flex flex-col gap-2">
      <div className="flex flex-wrap items-baseline gap-x-2.5 gap-y-1 px-0.5">
        <h3 className="text-[13.5px] font-semibold">Entitlements</h3>
        <span className="text-[12px] text-muted">Needs attention first.</span>
        <span className="flex-1" />
        <span data-testid="capacity-legend" className="flex flex-wrap gap-2.5">
          {(Object.keys(STATE) as CapState[]).reverse().map((k) => (
            <span key={k} title={STATE[k].detail} className={cn("inline-flex items-center gap-1 font-mono text-meta tracking-[0.4px]", STATE[k].text)}>
              <span aria-hidden="true">{STATE[k].glyph}</span>
              {STATE[k].word}
            </span>
          ))}
        </span>
      </div>
      <div className="overflow-x-auto rounded-[12px] border border-line-2 bg-surface">
        <div role="table" aria-label="Entitlements" className="min-w-[820px] text-[12px]">
          <div role="row" className="grid gap-3.5 border-b border-line-2/70 bg-[#0d1114] px-3.5 py-2 font-mono text-meta tracking-[0.6px] text-muted" style={{ gridTemplateColumns: COLS }}>
            <span role="columnheader">ENTITLEMENT</span>
            <span role="columnheader">STATUS</span>
            <span role="columnheader">7 DAYS</span>
            <span role="columnheader">BUDGET LEFT</span>
            <span role="columnheader">RESET</span>
          </div>
          {rows.length === 0 ? (
            <div data-testid="capacity-empty" className="m-3.5 flex flex-col gap-[3px] rounded-[10px] border-[1.5px] border-dashed border-line p-3">
              <span className="font-mono text-[10px] font-semibold tracking-[0.6px] text-fg-2">○ NO ENTITLEMENTS DECLARED</span>
              <span className="text-[12px] text-muted">
                No work is routed by capacity until an entitlement is declared on a runner.{" "}
                <a data-testid="capacity-declare" href={href ?? undefined} className="text-accent">Declare one</a>
              </span>
            </div>
          ) : (
            rows.map((e) => <Row key={e.id} e={e} />)
          )}
        </div>
      </div>
    </div>
  );
}

/** The most recent claim the router made in the window, by time: the item the reason defaults to. */
function latestRoutedClaim(d: HarnessRoutingDecisions): string | null {
  const rows = d.groups.filter((g) => g.verdict === "claimed" || g.verdict === "next_rung").flatMap((g) => g.recent);
  rows.sort((a, b) => b.at.localeCompare(a.at));
  return rows[0]?.item_key ?? null;
}

export function CapacityPanel({ projectId }: { projectId?: string }) {
  const { data, error, isError } = useHarnessCapacity(projectId);
  const { data: me } = useMe();
  const [view, setView] = useState<View>("overview");
  const [draft, setDraft] = useState(false);
  if (isError) {
    const { message, hint } = serverRefusal(error);
    return (
      <section data-testid="harness-capacity-refused" className="rounded-[12px] border border-line-2 bg-surface px-4 py-3.5">
        <h2 className="text-[16px] font-semibold">Capacity</h2>
        <p data-testid="capacity-refusal-message" className="mt-1 text-body">{message}</p>
        {hint && <p data-testid="capacity-refusal-hint" className="mt-1 text-body text-muted">{hint}</p>}
        <a data-testid="capacity-attach" href={consoleHref("/orgs") ?? undefined} className="mt-2 inline-block text-body text-accent">
          Attach to an org
        </a>
      </section>
    );
  }
  if (!data) return null;
  const u = data.utilization;
  const href = entitlementsHref(data.org.name || data.org_id);
  const noWeek = u.entitlements_with_budget + u.entitlements_without_budget === 0;
  const early = data.entitlements.filter(runsOutEarly).length;
  return (
    <section data-testid="harness-capacity" className="flex flex-col gap-5">
      <header className="flex flex-col gap-1">
        <div className="flex flex-wrap items-baseline gap-2.5">
          <h2 className="text-[16px] font-semibold tracking-[-0.01em]">Capacity</h2>
          <span data-testid="capacity-org" className="font-mono text-[10.5px] text-muted">
            {data.org.name} · {data.org.entitlement_count} entitlements · as of {shortWhen(data.as_of)}
          </span>
          <span className="flex-1" />
          <a data-testid="capacity-manage" href={href ?? undefined} className="text-[12px] text-accent">Manage entitlements →</a>
        </div>
        <p className="max-w-[720px] text-body leading-[1.5] text-muted">
          Headroom on every entitlement the org has declared. Credentials stay on their runners. Graphban sends the work, never the credential.
        </p>
      </header>

      <div className="flex flex-col gap-2">
        <Summary data={data} noWeek={noWeek} />
        <Rollup data={data} noWeek={noWeek} />
      </div>

      <Views view={view} setView={setView} early={early} ents={data.entitlements.length} draft={draft} />

      {view === "overview" && <Overview data={data} href={href} />}
      {view === "ents" && <Entitlements data={data} href={href} />}
      {/* Kept mounted while hidden: an unapplied routing draft survives a look at another view. */}
      <div data-testid="capacity-routing-view" hidden={view !== "routing"} className={cn("flex-col gap-4", view === "routing" ? "flex" : "hidden")}>
        <RoutingMode projectId={projectId} entitlements={data.entitlements} onDraft={setDraft} />
        <div className="grid grid-cols-[repeat(auto-fit,minmax(340px,1fr))] items-start gap-3">
          <RoutingDecisions decisions={data.routing_decisions} wavesEnded={u.waves_ended_by_capacity} />
          <ReviewReserve reserve={data.review_reserve} />
        </div>
        <Ladder projectId={projectId} name={me?.name || me?.email || undefined} />
        <RoutingReason defaultItem={latestRoutedClaim(data.routing_decisions)} />
      </div>
    </section>
  );
}
