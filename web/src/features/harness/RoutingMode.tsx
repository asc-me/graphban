import { useEffect, useMemo, useState } from "react";
import { ChevronDown, ChevronUp } from "lucide-react";

import { cn } from "@/lib/cn";
import { useFleetPolicy, useSaveFleetPolicy } from "@/lib/queries";
import type { FleetPolicy, HarnessCapacityEntitlement } from "@/lib/types";
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
  "Each item goes to the highest subscription in this list that has headroom. Entries that are "
  + "exhausted, broken or quiet are skipped, not removed — they take work again when they report "
  + "headroom. Org API keys and local follow on the fallback ladder."
);

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

/**
 * PRD-76 — the capacity routing rule on project policy: next available (S4 + D6) or a custom
 * subscription stack-rank, plus (GRPH-1505) the tie-break the router's last sort key uses and
 * whether an item falls down the ladder or holds until a reset when the top rung is spent.
 * Only what the router does is offered: no simulated outcomes (replay is GRPH-1712).
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
    || (mode === "custom_hierarchy" && JSON.stringify(order) !== JSON.stringify(savedOrder));

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

  const apply = () => {
    const body: FleetPolicy = {
      ...saved,
      routing_mode: mode,
      subscription_order: mode === "custom_hierarchy" ? order : null,
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
  };

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
            <ol className="flex max-w-[720px] flex-col gap-1">
              {order.map((id, i) => {
                const e = byId.get(id);
                if (!e) return null;
                const s = STATE[e.state];
                const live = e.state === "available";
                // The router probes unknown and broken with one child; exhausted and unreachable wait.
                return (
                  <li
                    key={id}
                    data-testid="routing-subscription-row"
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
                      <button type="button" className={arrow} disabled={i === 0} onClick={() => move(i, -1)} aria-label={`Move ${e.id} up`}>
                        <ChevronUp size={11} strokeWidth={2.4} />
                      </button>
                      <button type="button" className={arrow} disabled={i === order.length - 1} onClick={() => move(i, 1)} aria-label={`Move ${e.id} down`}>
                        <ChevronDown size={11} strokeWidth={2.4} />
                      </button>
                    </span>
                  </li>
                );
              })}
            </ol>
          )}
        </div>
      )}

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
