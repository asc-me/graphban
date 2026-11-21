import { useEffect, useMemo, useState } from "react";
import { ChevronDown, ChevronUp } from "lucide-react";

import { cn } from "@/lib/cn";
import { useFleetPolicy, useHarnessReplay, useSaveFleetPolicy } from "@/lib/queries";
import type {
  FleetPolicy, HarnessCapacityEntitlement, LadderRungId, RoutingReplayProposal, RungPreference,
} from "@/lib/types";
import { STATE } from "./capacityLook";

function defaultPolicy(): FleetPolicy {
  return {
    local_only: false,
    reviewer_cross_vendor: false,
    allowed_harnesses: [],
    routing_mode: "next_available",
    tie_break: "soonest_reset",
    when_exhausted: "fall_through",
  };
}

function mergePolicy(saved: FleetPolicy | null | undefined): FleetPolicy {
  if (!saved) return defaultPolicy();
  return {
    local_only: saved.local_only,
    reviewer_cross_vendor: saved.reviewer_cross_vendor,
    allowed_harnesses: saved.allowed_harnesses ?? [],
    caps: saved.caps ?? null,
    cluster_ceiling: saved.cluster_ceiling ?? null,
    ladder: saved.ladder ?? null,
    routing_mode: saved.routing_mode ?? "next_available",
    subscription_order: saved.subscription_order ?? null,
    rung_preference: saved.rung_preference ?? null,
    tie_break: saved.tie_break ?? "soonest_reset",
    when_exhausted: saved.when_exhausted ?? "fall_through",
  };
}

type TieBreak = "soonest_reset" | "most_headroom" | "cheapest";
type WhenExhausted = "fall_through" | "hold_until_reset";

const TIE_BREAKS: { id: TieBreak; label: string; rule: string; testid: string }[] = [
  { id: "soonest_reset", label: "Soonest reset", rule: "soonest reset wins", testid: "routing-tie-soonest" },
  { id: "most_headroom", label: "Most headroom", rule: "most token budget left this week wins", testid: "routing-tie-headroom" },
  { id: "cheapest", label: "Cheapest", rule: "lowest list price wins (prepaid first)", testid: "routing-tie-cheapest" },
];
const WHEN_EXHAUSTED: { id: WhenExhausted; label: string; testid: string }[] = [
  { id: "fall_through", label: "Fall through", testid: "routing-exhausted-fall" },
  { id: "hold_until_reset", label: "Hold until a reset", testid: "routing-exhausted-hold" },
];

function ruleNext(tie: TieBreak, exhausted: WhenExhausted): string {
  const rule = TIE_BREAKS.find((t) => t.id === tie)?.rule ?? "soonest reset wins";
  const head = exhausted === "hold_until_reset"
    ? "Subscription with headroom only: when that rung has none, the item waits for the earliest stated reset instead of falling to an API key or local."
    : "Subscription with headroom, then an org API key within budget, then local.";
  return `${head} Among available entitlements on one rung, ${rule}; unknown follows; exhausted, broken, and unreachable are skipped.`;
}
const RULE_CUSTOM = (
  "Each item goes to the highest subscription in this list that has headroom, then the highest "
  + "API key within budget, then the highest local model. Entries that are exhausted, broken or "
  + "quiet are skipped, not removed — they take work again when they report headroom."
);

/** GRPH-1713: rungs whose order lives in `rung_preference` (the subscription rung's is `subscription_order`). */
type OrderRung = "api_key" | "local";
const ORDER_RUNGS: { id: OrderRung; label: string; empty: string }[] = [
  { id: "api_key", label: "API keys", empty: "No API key entitlements to rank." },
  { id: "local", label: "Local models", empty: "No local entitlements to rank." },
];
const RUNG_LABELS: { id: LadderRungId; label: string }[] = [
  { id: "subscription", label: "Subscription" },
  { id: "api_key", label: "API key" },
  { id: "local", label: "Local" },
];
type RungTies = Record<LadderRungId, TieBreak | "">;
type RungOrders = Record<OrderRung, string[]>;

/** `cloud` rides the api_key rung, as the router reads it. */
function rungOfKind(kind: string): LadderRungId | null {
  if (kind === "subscription") return "subscription";
  if (kind === "api_key" || kind === "cloud") return "api_key";
  if (kind === "local") return "local";
  return null;
}

function savedRungTies(pref: FleetPolicy["rung_preference"]): RungTies {
  return {
    subscription: pref?.subscription?.tie_break ?? "",
    api_key: pref?.api_key?.tie_break ?? "",
    local: pref?.local?.tie_break ?? "",
  };
}

function buildRungPreference(
  orders: RungOrders,
  ties: RungTies,
  custom: boolean,
): FleetPolicy["rung_preference"] {
  const out: Partial<Record<LadderRungId, RungPreference>> = {};
  for (const r of RUNG_LABELS) {
    const entry: RungPreference = {};
    if (custom && r.id !== "subscription" && orders[r.id as OrderRung].length) {
      entry.order = orders[r.id as OrderRung];
    }
    if (ties[r.id]) entry.tie_break = ties[r.id] as TieBreak;
    if (Object.keys(entry).length) out[r.id] = entry;
  }
  return Object.keys(out).length ? out : null;
}

/** Saved order first; any subscription not yet in policy sorts after. */
function editableSubscriptionOrder(
  savedOrder: string[] | null | undefined,
  subscriptionIds: string[],
): string[] {
  if (!savedOrder?.length) return subscriptionIds;
  const known = savedOrder.filter((id) => subscriptionIds.includes(id));
  const added = subscriptionIds.filter((id) => !savedOrder.includes(id));
  return [...known, ...added];
}

type Mode = "next_available" | "custom_hierarchy";
const MODES: { id: Mode; label: string; sub: string; testid: string }[] = [
  { id: "next_available", label: "Next available", sub: "first entitlement with headroom", testid: "routing-mode-next" },
  { id: "custom_hierarchy", label: "Preferred hierarchy", sub: "your order, top first", testid: "routing-mode-custom" },
];

const arrow = "flex size-6 items-center justify-center rounded-[6px] border border-control text-fg-2 hover:bg-surface-3 disabled:text-line-3 disabled:hover:bg-transparent";

/** A small labelled radio group: one of a fixed set of policy values. */
function Choice<T extends string>({
  label,
  testid,
  options,
  value,
  onChange,
}: {
  label: string;
  testid: string;
  options: { id: T; label: string; testid: string }[];
  value: T;
  onChange: (v: T) => void;
}) {
  return (
    <div className="flex flex-col gap-1">
      <span className="text-[10.5px] text-faint">{label}</span>
      <div role="radiogroup" aria-label={label} data-testid={testid} className="flex w-fit overflow-hidden rounded-[8px] border border-control">
        {options.map((o, i) => {
          const on = value === o.id;
          return (
            <button
              key={o.id}
              type="button"
              role="radio"
              aria-checked={on}
              data-testid={o.testid}
              onClick={() => onChange(o.id)}
              className={cn(
                "px-3 py-[5px] text-[12px]",
                i > 0 && "border-l border-control",
                on ? "bg-surface-3 font-medium text-fg" : "text-muted hover:bg-surface-3/50",
              )}
            >
              {o.label}
            </button>
          );
        })}
      </div>
    </div>
  );
}

/** GRPH-1713: an up/down-ranked list of one rung's entitlements. */
function RankList({ ids, byId, rowTestId, onMove }: {
  ids: string[];
  byId: Map<string, HarnessCapacityEntitlement>;
  rowTestId: string;
  onMove: (i: number, d: number) => void;
}) {
  return (
    <ol className="flex max-w-[720px] flex-col gap-1">
      {ids.map((id, i) => {
        const e = byId.get(id);
        if (!e) return null;
        const s = STATE[e.state];
        const live = e.state === "available";
        // The router probes unknown and broken with one child; exhausted and unreachable wait.
        return (
          <li
            key={id}
            data-testid={rowTestId}
            data-entitlement={id}
            className={cn("grid grid-cols-[20px_minmax(0,1fr)_auto_auto] items-center gap-2.5 rounded-[8px] border border-line-2/70 bg-[#0d1114] py-1.5 pl-2.5 pr-2", !live && "opacity-60")}
          >
            <span className="text-right font-mono text-[10.5px] text-faint">{i + 1}</span>
            <span className="flex min-w-0 flex-col gap-px">
              <span className="truncate font-mono text-[12px] text-fg">{e.id}</span>
              <span className="text-[10.5px] text-faint">{[e.kind, e.plan_label, e.runner?.label].filter(Boolean).join(" · ")}</span>
            </span>
            <span data-testid="routing-row-now" className={cn("inline-flex items-center gap-[5px] whitespace-nowrap font-mono text-[10px]", s.text)}>
              <span aria-hidden="true">{s.glyph}</span>
              {live
                ? "takes work now"
                : e.state === "unknown" || e.state === "broken"
                  ? `probed one item at a time · ${s.word.toLowerCase()}`
                  : `skipped now · ${s.word.toLowerCase()}`}
            </span>
            <span className="flex gap-[3px]">
              <button type="button" className={arrow} disabled={i === 0} onClick={() => onMove(i, -1)} aria-label={`Move ${e.id} up`}>
                <ChevronUp size={11} strokeWidth={2.4} />
              </button>
              <button type="button" className={arrow} disabled={i === ids.length - 1} onClick={() => onMove(i, 1)} aria-label={`Move ${e.id} down`}>
                <ChevronDown size={11} strokeWidth={2.4} />
              </button>
            </span>
          </li>
        );
      })}
    </ol>
  );
}

/**
 * GRPH-1712 — the rule on screen replayed over the last 7 days of routing decisions, each
 * re-ranked from the candidates it recorded then (never today's headroom). Decisions recorded
 * before candidates were stored are counted apart, and when none is replayable the line says so
 * instead of showing 0.
 */
function ReplayLine({ projectId, proposal, draft }: {
  projectId?: string;
  proposal: RoutingReplayProposal;
  draft: boolean;
}) {
  const { data, isLoading, isError } = useHarnessReplay(projectId, proposal);
  if (!projectId || isError) return null;
  const head = draft ? "Replay of this draft" : "Replay of the rule in force";
  let body: string;
  if (isLoading || !data) {
    body = "replaying the last 7 days…";
  } else if (data.replayable === 0 || data.moved === null) {
    body = data.decisions === 0
      ? `no routing decisions in the last ${data.days} days to replay`
      : `nothing in the last ${data.days} days is replayable — ${data.not_replayable} `
        + `decision${data.not_replayable === 1 ? " was" : "s were"} recorded before routing stored its candidates`;
  } else {
    const held = data.held ? ` (${data.held} held until a reset)` : "";
    const legacy = data.not_replayable
      ? ` · ${data.not_replayable} recorded before candidates were stored ${data.not_replayable === 1 ? "is" : "are"} not replayable`
      : "";
    body = `${data.moved} of ${data.replayable} decision${data.replayable === 1 ? "" : "s"} `
      + `in the last ${data.days} days would have moved${held}${legacy}`;
  }
  return (
    <p data-testid="routing-replay" role="status" className="text-[12px] text-muted">
      <span className="font-medium text-fg-2">{head}:</span> {body}
    </p>
  );
}

/**
 * PRD-76 — the capacity routing rule on project policy: next available (S4 + D6) or a custom
 * subscription stack-rank, plus (GRPH-1505) the tie-break the router's last sort key uses and
 * whether an item falls down the ladder or holds until a reset when the top rung is spent.
 * Only what the router does is offered; the replay line (GRPH-1712) re-ranks recorded decisions,
 * it does not simulate today's headroom.
 */
export function RoutingMode({
  projectId,
  entitlements,
  onDraft,
}: {
  projectId?: string;
  entitlements: HarnessCapacityEntitlement[];
  /** Told whenever the rule on screen differs from the saved one. */
  onDraft?: (draft: boolean) => void;
}) {
  const { data: policyRead } = useFleetPolicy(projectId ?? "");
  const save = useSaveFleetPolicy(projectId ?? "");

  const saved = mergePolicy(policyRead?.policy);
  const writable = policyRead?.writable !== false;

  const subscriptions = useMemo(
    () => entitlements.filter((e) => e.kind === "subscription"),
    [entitlements],
  );

  const rungEntitlements = useMemo(() => {
    const by: Record<OrderRung, HarnessCapacityEntitlement[]> = { api_key: [], local: [] };
    for (const e of entitlements) {
      const r = rungOfKind(e.kind);
      if (r === "api_key" || r === "local") by[r].push(e);
    }
    return by;
  }, [entitlements]);
  const savedPref = saved.rung_preference;
  const savedOrders = useMemo<RungOrders>(() => ({
    api_key: editableSubscriptionOrder(savedPref?.api_key?.order, rungEntitlements.api_key.map((e) => e.id)),
    local: editableSubscriptionOrder(savedPref?.local?.order, rungEntitlements.local.map((e) => e.id)),
  }), [savedPref, rungEntitlements]);
  const savedTies = useMemo(() => savedRungTies(savedPref), [savedPref]);
  const [rungOrders, setRungOrders] = useState<RungOrders>({ api_key: [], local: [] });
  const [rungTies, setRungTies] = useState<RungTies>({ subscription: "", api_key: "", local: "" });
  const savedOrdersKey = JSON.stringify(savedOrders);
  const savedTiesKey = JSON.stringify(savedTies);
  useEffect(() => { setRungOrders(savedOrders); }, [savedOrdersKey]);
  useEffect(() => { setRungTies(savedTies); }, [savedTiesKey]);

  const [mode, setMode] = useState<Mode>("next_available");
  const [order, setOrder] = useState<string[]>([]);
  const [tie, setTie] = useState<TieBreak>("soonest_reset");
  const [exhausted, setExhausted] = useState<WhenExhausted>("fall_through");

  const savedTie: TieBreak = saved.tie_break ?? "soonest_reset";
  const savedExhausted: WhenExhausted = saved.when_exhausted ?? "fall_through";
  useEffect(() => { setTie(savedTie); }, [savedTie]);
  useEffect(() => { setExhausted(savedExhausted); }, [savedExhausted]);

  useEffect(() => {
    setMode(saved.routing_mode === "custom_hierarchy" ? "custom_hierarchy" : "next_available");
    const subscriptionIds = subscriptions.map((e) => e.id);
    setOrder(editableSubscriptionOrder(saved.subscription_order, subscriptionIds));
  }, [saved.routing_mode, saved.subscription_order, subscriptions]);

  // Compared with the order as drawn on load (saved order, new subscriptions appended), so a
  // subscription the policy has not ranked yet is not a change nobody made.
  const savedOrder = editableSubscriptionOrder(saved.subscription_order, subscriptions.map((e) => e.id));
  const dirty =
    mode !== (saved.routing_mode ?? "next_available")
    || tie !== savedTie
    || exhausted !== savedExhausted
    || JSON.stringify(rungTies) !== savedTiesKey
    || (mode === "custom_hierarchy" && JSON.stringify(order) !== JSON.stringify(savedOrder))
    || (mode === "custom_hierarchy" && JSON.stringify(rungOrders) !== savedOrdersKey);

  useEffect(() => { onDraft?.(dirty); }, [dirty, onDraft]);

  const localUnreachable = entitlements.some(
    (e) => e.kind === "local" && e.state === "unreachable",
  );

  const move = (i: number, d: number) => {
    const o = order.slice();
    const j = i + d;
    if (j < 0 || j >= o.length) return;
    [o[i], o[j]] = [o[j], o[i]];
    setOrder(o);
  };
  const moveIn = (rung: OrderRung) => (i: number, d: number) => {
    const o = rungOrders[rung].slice();
    const j = i + d;
    if (j < 0 || j >= o.length) return;
    [o[i], o[j]] = [o[j], o[i]];
    setRungOrders({ ...rungOrders, [rung]: o });
  };

  const apply = () => {
    const body: FleetPolicy = {
      ...saved,
      routing_mode: mode,
      subscription_order: mode === "custom_hierarchy" ? order : null,
      rung_preference: buildRungPreference(rungOrders, rungTies, mode === "custom_hierarchy"),
      tie_break: tie,
      when_exhausted: exhausted,
    };
    save.mutate(body);
  };

  const discard = () => {
    setMode(saved.routing_mode === "custom_hierarchy" ? "custom_hierarchy" : "next_available");
    setOrder(editableSubscriptionOrder(saved.subscription_order, subscriptions.map((e) => e.id)));
    setTie(savedTie);
    setExhausted(savedExhausted);
    setRungOrders(savedOrders);
    setRungTies(savedTies);
  };

  const proposal = useMemo<RoutingReplayProposal>(() => ({
    tie_break: tie,
    when_exhausted: exhausted,
    routing_mode: mode,
    subscription_order: mode === "custom_hierarchy" ? order : null,
    rung_order: mode === "custom_hierarchy" ? rungOrders : null,
    rung_tie_break: Object.fromEntries(Object.entries(rungTies).filter(([, t]) => t)),
  }), [tie, exhausted, mode, order, rungOrders, rungTies]);

  const byId = new Map(subscriptions.map((e) => [e.id, e]));
  const topLive = order.find((id) => byId.get(id)?.state === "available");
  const title = mode === "next_available"
    ? `Route to the next available entitlement · ties by ${TIE_BREAKS.find((t) => t.id === tie)?.label.toLowerCase()}`
    : `Route by preferred hierarchy · ${topLive ?? "no subscription has headroom"}${topLive ? " first" : ""}`;

  return (
    <div
      data-testid="capacity-routing-mode"
      className={cn(
        "relative flex flex-col gap-3 overflow-hidden rounded-[11px] border bg-surface-2 py-3.5 pl-[19px] pr-4",
        dirty ? "border-[rgba(240,180,80,0.45)]" : "border-line-2",
      )}
    >
      <span aria-hidden="true" className="absolute inset-y-0 left-0 w-[3px] bg-st-next" />
      <div className="flex flex-wrap items-center gap-2">
        <span className="font-mono text-[10.5px] tracking-[0.06em] text-faint">CAPACITY ROUTING · PROJECT POLICY</span>
        <span className="flex-1" />
        <span data-testid="routing-rule-state" className={cn("text-[10.5px]", dirty ? "text-[#f0b450]" : "text-st-done")}>
          {dirty ? "draft · not applied" : "active"}
        </span>
      </div>
      <div className="flex flex-col gap-[3px]">
        <div data-testid="routing-rule-title" className="text-[13.5px] font-medium text-fg">{title}</div>
        <p data-testid={mode === "next_available" ? "routing-rule-in-force" : "routing-rule-custom"} className="max-w-[84ch] text-[12px] leading-[1.5] text-muted">
          {mode === "next_available" ? ruleNext(tie, exhausted) : RULE_CUSTOM}
        </p>
      </div>

      {localUnreachable && (
        <p data-testid="capacity-local-unreachable" className="text-[12px] text-muted" role="status">
          Local is unreachable — spawns on the local rung do not start until a runner reports again.
        </p>
      )}

      <div role="radiogroup" aria-label="Routing mode" className="flex w-fit overflow-hidden rounded-[8px] border border-control">
        {MODES.map((m, i) => {
          const on = mode === m.id;
          return (
            <button
              key={m.id}
              type="button"
              role="radio"
              aria-checked={on}
              data-testid={m.testid}
              onClick={() => setMode(m.id)}
              className={cn(
                "flex flex-col items-start gap-px px-3.5 py-[7px] text-left",
                i > 0 && "border-l border-control",
                on ? "bg-surface-3" : "hover:bg-surface-3/50",
              )}
            >
              <span className={cn("text-[12px] font-medium", on ? "text-fg" : "text-muted")}>{m.label}</span>
              <span className="text-[10.5px] text-faint">{m.sub}</span>
            </button>
          );
        })}
      </div>

      {mode === "custom_hierarchy" && (
        <div data-testid="routing-subscription-stack" className="flex flex-col gap-2">
          {subscriptions.length === 0 ? (
            <p className="text-[12px] text-muted">No subscription entitlements to rank.</p>
          ) : (
            <RankList ids={order} byId={byId} rowTestId="routing-subscription-row" onMove={move} />
          )}
        </div>
      )}

      {mode === "custom_hierarchy" && ORDER_RUNGS.map((r) => (
        <div key={r.id} data-testid="routing-rung-stack" data-rung={r.id} className="flex flex-col gap-1">
          <span className="text-[10.5px] text-faint">{r.label} · ranked among themselves</span>
          {rungEntitlements[r.id].length === 0 ? (
            <p className="text-[12px] text-muted">{r.empty}</p>
          ) : (
            <RankList
              ids={rungOrders[r.id]}
              byId={new Map(rungEntitlements[r.id].map((e) => [e.id, e]))}
              rowTestId="routing-rung-row"
              onMove={moveIn(r.id)}
            />
          )}
        </div>
      ))}

      <p data-testid="routing-ladder-fixed" className="text-small text-faint">
        The ladder order is fixed — subscription, then API key, then local. A rung&apos;s order and
        tie-break rank entitlements within that rung only; none lifts one past the ladder.
      </p>

      <div className="flex flex-wrap items-start gap-4">
        <Choice
          label="Break ties by"
          testid="routing-tie-break"
          options={TIE_BREAKS}
          value={tie}
          onChange={setTie}
        />
        <Choice
          label="When nothing above has headroom"
          testid="routing-when-exhausted"
          options={WHEN_EXHAUSTED}
          value={exhausted}
          onChange={setExhausted}
        />
      </div>

      <div data-testid="routing-rung-ties" className="flex flex-wrap items-end gap-3">
        {RUNG_LABELS.map((r) => (
          <label key={r.id} className="flex flex-col gap-1">
            <span className="text-[10.5px] text-faint">{r.label} ties by</span>
            <select
              data-testid={`routing-rung-tie-${r.id}`}
              value={rungTies[r.id]}
              onChange={(ev) => setRungTies({ ...rungTies, [r.id]: ev.target.value as TieBreak | "" })}
              className="rounded-[8px] border border-control bg-surface-2 px-2 py-[4px] text-[12px] text-fg-2"
            >
              <option value="">as above</option>
              {TIE_BREAKS.map((t) => <option key={t.id} value={t.id}>{t.label}</option>)}
            </select>
          </label>
        ))}
      </div>

      <ReplayLine projectId={projectId} proposal={proposal} draft={dirty} />

      <div className="flex flex-wrap items-center gap-1.5">
        <button
          type="button"
          data-testid="routing-apply"
          className={cn(
            "inline-flex items-center rounded-[8px] border px-[11px] py-[5px] text-[12px] font-medium disabled:cursor-default",
            dirty && writable ? "border-accent bg-accent text-bg" : "border-control text-faint",
          )}
          disabled={!writable || !dirty || save.isPending}
          onClick={apply}
        >
          Apply rule
        </button>
        <button
          type="button"
          data-testid="routing-discard"
          className={cn("rounded-[8px] border border-transparent px-2.5 py-[5px] text-[12px] hover:border-control disabled:hover:border-transparent", dirty ? "text-muted" : "text-faint/60")}
          disabled={!dirty || save.isPending}
          onClick={discard}
        >
          Discard
        </button>
        {!writable && (
          <span data-testid="routing-apply-denied" className="text-small text-muted">
            You cannot edit project policy.
          </span>
        )}
        <span className="flex-1" />
        <span className="font-mono text-[10.5px] text-faint">precedence: policy filter › routing rule › fallback ladder</span>
      </div>
    </div>
  );
}
