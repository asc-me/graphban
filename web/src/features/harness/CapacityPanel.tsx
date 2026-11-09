import { useHarnessCapacity, useMe } from "@/lib/queries";
import { consoleHref } from "@/lib/routes";
import { Ladder } from "./Ladder";
import { RoutingReason } from "./RoutingReason";
import { ReviewReserve } from "./ReviewReserve";
import { RoutingDecisions } from "./RoutingDecisions";
import type { HarnessRoutingDecisions, HarnessCapacityEntitlement, HarnessCapacityRunner } from "@/lib/types";

/**
 * PRD-62 S5/D8 — the Capacity tab: header, org rollup, then one row per entitlement. Every
 * absence says why: a projection with no date gives its reason, an unreported spend is a count
 * (and makes the rollup share a floor), a rollup number the supervisor does not record reads
 * "not recorded" rather than 0, and a day with no spend report is a gap in the sparkline. A 402
 * (org off a capacity plan) shows the server's own message and hint, never a hidden tab.
 */
const runnerName = (r: HarnessCapacityRunner) => r.label || r.agent_id;
const entitlementsHref = (org: string) => consoleHref(`/orgs/${encodeURIComponent(org)}/settings/entitlements`);
const when = (iso: string) => new Date(iso).toLocaleString();
const day = (iso: string) => new Date(iso).toLocaleDateString();

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

const GLYPH: Record<HarnessCapacityEntitlement["state"], string> = {
  available: "●", unknown: "○", exhausted: "◒", broken: "✕", unreachable: "⋯",
};

/** Trailing-7-days bars. A null day is drawn as a gap (a dotted baseline mark), never as a zero-height bar. */
function Sparkline({ days }: { days: (number | null)[] }) {
  const max = Math.max(1, ...days.map((d) => d ?? 0));
  const w = 8, gap = 3, h = 24;
  return (
    <svg data-testid="capacity-spark" width={days.length * (w + gap)} height={h} role="img"
         aria-label="tokens per day, trailing 7 days">
      {days.map((d, i) =>
        d === null ? (
          <line key={i} data-testid="capacity-spark-gap" data-day={i} x1={i * (w + gap)} x2={i * (w + gap) + w}
                y1={h - 1} y2={h - 1} stroke="currentColor" strokeDasharray="1 2" opacity={0.5} />
        ) : (
          <rect key={i} data-testid="capacity-spark-bar" data-day={i} data-tokens={d} x={i * (w + gap)}
                width={w} y={h - Math.max(1, (d / max) * h)} height={Math.max(1, (d / max) * h)} fill="currentColor" />
        ),
      )}
    </svg>
  );
}

function Row({ e }: { e: HarnessCapacityEntitlement }) {
  const budget = e.token_budget === null ? "no burn-down" : `${e.token_budget} tokens`;
  return (
    <tr key={e.id} data-testid="capacity-row" data-state={e.state} className="align-top">
      <td className="py-2 pr-3">
        <div className="font-mono">{e.id}</div>
        <div className="text-small text-faint">{e.vendor} · {e.kind}{e.plan_label ? ` · ${e.plan_label}` : ""}</div>
      </td>
      <td className="py-2 pr-3 font-mono" data-testid="capacity-runner-cell">
        {e.runner?.label ?? "unknown"}
      </td>
      <td className="py-2 pr-3" title={e.vendor_text}>
        <div className="font-mono text-small">{GLYPH[e.state]} {e.state.toUpperCase()}</div>
        {e.state === "exhausted" && (
          <div data-testid="capacity-exhausted" className="text-small text-muted">
            “{e.vendor_text}”{e.seen_at && ` · seen ${when(e.seen_at)}`}
          </div>
        )}
        {e.state === "broken" && (
          <div data-testid="capacity-runner" className="text-small text-faint">
            “{e.vendor_text}” reported by {e.reported_by ? runnerName(e.reported_by) : "unknown runner"}
            {e.declared_by.length > 0 && ` · declared by ${e.declared_by.map(runnerName).join(", ")}`}
          </div>
        )}
        {e.state === "unknown" && (
          <div data-testid="capacity-unknown" className="text-small text-muted">
            Never measured. Ranked below available.
          </div>
        )}
        {e.state === "unreachable" && (
          <div data-testid="capacity-unreachable" className="text-small text-muted">
            {e.runner?.label ?? "The runner"} has gone quiet.{" "}
            <span className="rounded-md border border-line-2 px-1 font-mono text-meta">PLANNED · last-known state</span>
          </div>
        )}
      </td>
      <td className="py-2 pr-3 text-muted">
        <Sparkline days={e.daily_tokens} />
        <div className="font-mono text-small">
          {e.week.tokens}
          {e.week.unreported > 0 && ` (+${e.week.unreported} unreported)`}
        </div>
      </td>
      <td className="py-2 pr-3" title={e.projected_exhaustion.basis}>
        <div data-testid="capacity-budget" className="font-mono text-small">{budget}</div>
        <div data-testid="capacity-projection" className="text-small text-muted">
          {e.projected_exhaustion.at
            ? `runs out ~${when(e.projected_exhaustion.at)}`
            : e.projected_exhaustion.reason}
        </div>
      </td>
      <td className="py-2">
        <div>{e.reset_cadence ?? "not stated"}</div>
        <span className="rounded-md border border-line-2 px-1 font-mono text-meta text-muted">
          PLANNED · forecast reset
        </span>
      </td>
    </tr>
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
  if (isError) {
    const { message, hint } = serverRefusal(error);
    return (
      <section data-testid="harness-capacity-refused" className="rounded-[10px] border border-line-2 bg-surface-2 px-3.5 py-3">
        <div className="text-[13px] font-medium">Capacity</div>
        <p data-testid="capacity-refusal-message" className="mt-1 text-body">{message}</p>
        {hint && <p data-testid="capacity-refusal-hint" className="mt-1 text-body text-muted">{hint}</p>}
        <a data-testid="capacity-attach" href={consoleHref("/orgs")} className="mt-2 inline-block text-body text-accent">
          Attach to an org
        </a>
      </section>
    );
  }
  if (!data) return null;
  const u = data.utilization;
  const count = (c: { count: number | null }) => (c.count === null ? "not recorded" : c.count);
  const href = entitlementsHref(data.org.name || data.org_id);
  const noWeek = u.entitlements_with_budget + u.entitlements_without_budget === 0;
  return (
    <section data-testid="harness-capacity" className="flex flex-col gap-3 rounded-[10px] border border-line-2 bg-surface-2 px-3.5 py-3">
      <header>
        <div className="flex items-baseline justify-between">
          <div>
            <span className="text-[13px] font-medium">Capacity</span>{" "}
            <span data-testid="capacity-org" className="font-mono text-small text-muted">
              {data.org.name} · {data.org.entitlement_count} entitlements
            </span>
          </div>
          <a data-testid="capacity-manage" href={href} className="text-body text-accent">Manage entitlements →</a>
        </div>
        <p className="mt-1 text-body text-muted">
          Headroom on every entitlement the org has declared. Credentials stay on their runners. Graphban sends the work, never the credential.
        </p>
      </header>

      <div>
        <div className="text-small text-faint">
          Last full week · {day(u.week_start)} – {day(u.week_end)}
        </div>
        {noWeek ? (
          <div data-testid="capacity-rollup" className="mt-1 text-body text-muted">
            <span className="font-mono text-small">○ NOT ENOUGH DATA</span>{" "}
            “no completed week since capacity routing was enabled”
            <div className="text-small">Waves ended by capacity: {count(u.waves_ended_by_capacity)}. Items deferred to a reset: {count(u.items_deferred_to_reset)}.</div>
          </div>
        ) : (
          <div data-testid="capacity-rollup" className="mt-1 text-body text-muted">
            Headroom used last week:{" "}
            {u.share_used === null
              ? "unknown — no entitlement states a token budget"
              : u.share_is_lower_bound
                ? `≥${Math.round(u.share_used * 100)}% of ${u.budget} tokens (+${u.unreported_spends} unreported)`
                : `${Math.round(u.share_used * 100)}% of ${u.budget} tokens`}
            . Tokens routed: {u.tokens_used}. Waves ended by capacity: {count(u.waves_ended_by_capacity)}. Items deferred to a reset:{" "}
            {count(u.items_deferred_to_reset)}.
          </div>
        )}
      </div>

      {data.entitlements.length === 0 ? (
        <div data-testid="capacity-empty" className="text-body text-muted">
          <div className="font-mono text-small">○ NO ENTITLEMENTS DECLARED</div>
          No work is routed by capacity until an entitlement is declared on a runner.{" "}
          <a data-testid="capacity-declare" href={href} className="text-accent">Declare one</a>
        </div>
      ) : (
        <table className="w-full text-left text-body">
          <thead className="text-small text-faint">
            <tr>
              <th>ENTITLEMENT</th><th>RUNNER</th><th>HEADROOM</th><th>TRAILING 7 DAYS</th><th>BUDGET · RUNS OUT</th><th>RESET</th>
            </tr>
          </thead>
          <tbody>{data.entitlements.map((e) => <Row key={e.id} e={e} />)}</tbody>
        </table>
      )}
      <RoutingDecisions decisions={data.routing_decisions} />
      <ReviewReserve reserve={data.review_reserve} />
      <Ladder projectId={projectId} name={me?.name || me?.email || undefined} />
      <RoutingReason defaultItem={latestRoutedClaim(data.routing_decisions)} />
    </section>
  );
}
