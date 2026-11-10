import { useEffect, useMemo, useState } from "react";
import { useFleetPolicy, useSaveFleetPolicy, useHarnessLadder } from "@/lib/queries";
import type { FleetPolicy, HarnessCapacityEntitlement } from "@/lib/types";

const btn = "rounded-md border border-control px-2 py-0.5 text-small hover:bg-surface-3 disabled:opacity-50";

function defaultPolicy(): FleetPolicy {
  return {
    local_only: false,
    reviewer_cross_vendor: false,
    allowed_harnesses: [],
    routing_mode: "next_available",
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
  };
}

const RULE_NEXT = (
  "Subscription with headroom, then an org API key within budget, then local. Among available "
  + "entitlements, soonest reset wins; unknown follows; exhausted, broken, and unreachable are skipped."
);

/**
 * PRD-76 — routing mode beside the fallback ladder: next available (S4 + D6) or a custom
 * subscription stack-rank on project policy. No replay, cheapest, or hold-until-reset controls.
 */
export function RoutingMode({
  projectId,
  entitlements,
}: {
  projectId?: string;
  entitlements: HarnessCapacityEntitlement[];
}) {
  const { data: policyRead } = useFleetPolicy(projectId ?? "");
  const { data: ladder } = useHarnessLadder(projectId);
  const save = useSaveFleetPolicy(projectId ?? "");

  const saved = mergePolicy(policyRead?.policy);
  const writable = policyRead?.writable !== false;

  const subscriptions = useMemo(
    () => entitlements.filter((e) => e.kind === "subscription"),
    [entitlements],
  );

  const [mode, setMode] = useState<"next_available" | "custom_hierarchy">("next_available");
  const [order, setOrder] = useState<string[]>([]);

  useEffect(() => {
    setMode(saved.routing_mode === "custom_hierarchy" ? "custom_hierarchy" : "next_available");
    const ids = saved.subscription_order?.length
      ? saved.subscription_order
      : subscriptions.map((e) => e.id);
    setOrder(ids.filter((id) => subscriptions.some((e) => e.id === id)));
  }, [saved.routing_mode, saved.subscription_order, subscriptions]);

  const dirty =
    mode !== (saved.routing_mode ?? "next_available")
    || (mode === "custom_hierarchy"
      && JSON.stringify(order) !== JSON.stringify(saved.subscription_order ?? []));

  const localUnreachable = entitlements.some(
    (e) => e.kind === "local" && e.state === "unreachable",
  );
  const localOnLadder = ladder?.effective.some((r) => r.rung === "local" && !r.removed) ?? false;

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
    };
    save.mutate(body);
  };

  const discard = () => {
    setMode(saved.routing_mode === "custom_hierarchy" ? "custom_hierarchy" : "next_available");
    setOrder(
      saved.subscription_order?.length
        ? saved.subscription_order
        : subscriptions.map((e) => e.id),
    );
  };

  return (
    <div data-testid="capacity-routing-mode">
      <div className="text-[13px] font-medium">Routing</div>
      <p className="text-body text-muted">
        How work picks an entitlement when several have headroom. Profile overrides still reorder
        or drop ladder rungs; they cannot add a rung the policy removed.
      </p>

      {localOnLadder && localUnreachable && (
        <p data-testid="capacity-local-unreachable" className="mt-2 text-body text-muted" role="status">
          Local is unreachable — spawns on the local rung do not start until a runner reports again.
        </p>
      )}

      <div className="mt-2 flex flex-wrap gap-3 text-body">
        <label className="flex items-center gap-1.5">
          <input
            type="radio"
            name="routing-mode"
            data-testid="routing-mode-next"
            checked={mode === "next_available"}
            onChange={() => setMode("next_available")}
          />
          Next available
        </label>
        <label className="flex items-center gap-1.5">
          <input
            type="radio"
            name="routing-mode"
            data-testid="routing-mode-custom"
            checked={mode === "custom_hierarchy"}
            onChange={() => setMode("custom_hierarchy")}
          />
          Custom hierarchy
        </label>
      </div>

      {mode === "next_available" ? (
        <p data-testid="routing-rule-in-force" className="mt-2 text-body text-muted">
          {RULE_NEXT}
        </p>
      ) : (
        <div data-testid="routing-subscription-stack" className="mt-2">
          <div className="text-small text-faint">Subscription preference (project policy)</div>
          {subscriptions.length === 0 ? (
            <p className="text-body text-muted">No subscription entitlements to rank.</p>
          ) : (
            <ul className="mt-1">
              {order.map((id, i) => {
                const e = subscriptions.find((x) => x.id === id);
                if (!e) return null;
                return (
                  <li
                    key={id}
                    data-testid="routing-subscription-row"
                    data-entitlement={id}
                    className="flex items-center gap-2 py-1"
                  >
                    <span className="w-5 font-mono text-small text-faint">{i + 1}</span>
                    <span className="font-mono text-small">{e.id}</span>
                    <span className="text-small text-muted">{e.vendor}</span>
                    <button type="button" className={btn} disabled={i === 0} onClick={() => move(i, -1)} aria-label="Move up">↑</button>
                    <button type="button" className={btn} disabled={i === order.length - 1} onClick={() => move(i, 1)} aria-label="Move down">↓</button>
                  </li>
                );
              })}
            </ul>
          )}
        </div>
      )}

      <div className="mt-2 flex gap-2">
        <button
          type="button"
          data-testid="routing-apply"
          className={btn}
          disabled={!writable || !dirty || save.isPending}
          onClick={apply}
        >
          Apply
        </button>
        <button
          type="button"
          data-testid="routing-discard"
          className={btn}
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
      </div>

    </div>
  );
}
