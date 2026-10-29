import { useQueryClient } from "@tanstack/react-query";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";

import { PlaceHeader } from "@/components/shell/PlaceHeader";
import { FETCH_FAILED, PlannerError } from "@/components/planner/PlannerStates";
import { useProjectCtx } from "@/features/ProjectContext";
import { useTriageFeed } from "@/lib/queries";
import { TYPE_META } from "@/lib/meta";
import type { RequestType, TriageFeedRow } from "@/lib/types";

import { TABS, actionsFor, formatAge, inTab, type TriageAction, type Undo } from "./actions";
import { TriageDetail } from "./TriageDetail";

/**
 * Triage — one keyboard-driven queue across bugs, requests, blockers and collisions
 * (GRPH-1148, PRD-60 D11). Per-project: collision clustering reasons over one project's code
 * graph only.
 *
 * Three states are kept apart on purpose, because each reads as "nothing waiting" if merged:
 * still loading, a source that did not answer (`failed_sources`), and a queue that is truly
 * clear. Only the last may say "Triage clear".
 */

interface Processed {
  row: TriageFeedRow;
  result: string;
  undo: Undo;
}

const KIND_COLOR: Record<string, string> = {
  blocker: "#ff6b6b",
  collision: "#e0b34a",
};
const kindColor = (r: TriageFeedRow) => KIND_COLOR[r.kind] ?? (r.kind in TYPE_META ? TYPE_META[r.kind as RequestType].color : undefined) ?? "#8b949e";
const kindLabel = (r: TriageFeedRow) => r.kind.toUpperCase();

function metaOf(r: TriageFeedRow): string {
  if (r.kind === "blocker")
    return `${r.owner ?? "unclaimed"} · ${r.decision ? "needs a decision" : r.waiting_on ? `waiting on ${r.waiting_on.id}` : "blocked"}`;
  if (r.kind === "collision") return `${r.members.length} items · ${r.risk}`;
  return `↑ ${r.votes} · ${r.by || "anonymous"}${r.duplicate ? " · dup?" : ""}`;
}

export function TriageView() {
  const { active } = useProjectCtx();
  const projectId = active?.id ?? "";
  const qc = useQueryClient();
  const feedQ = useTriageFeed(projectId);

  const [tab, setTab] = useState<(typeof TABS)[number]["id"]>("all");
  const [selId, setSelId] = useState<string | null>(null);
  const [processed, setProcessed] = useState<Processed[]>([]);
  const [confirming, setConfirming] = useState<TriageAction | null>(null);
  const [error, setError] = useState<string | null>(null);
  const busy = useRef(false);

  const hidden = useMemo(() => new Set(processed.map((p) => p.row.id)), [processed]);
  const live = useMemo(
    () => (feedQ.data?.rows ?? []).filter((r) => !hidden.has(r.id)),
    [feedQ.data, hidden],
  );
  const shown = useMemo(() => live.filter((r) => inTab(r, tab)), [live, tab]);
  const sel = shown.find((r) => r.id === selId) ?? shown[0] ?? null;
  const actions = useMemo(() => (sel ? actionsFor(sel, projectId) : []), [sel, projectId]);

  const refresh = useCallback(
    () => qc.invalidateQueries({ queryKey: ["triage-feed", projectId] }),
    [qc, projectId],
  );

  const run = useCallback(
    async (action: TriageAction) => {
      if (!sel || busy.current) return;
      busy.current = true;
      setConfirming(null);
      setError(null);
      const i = shown.indexOf(sel);
      const next = shown[i + 1] ?? shown[i - 1] ?? null;
      try {
        const { result, undo } = await action.run();
        setProcessed((p) => [...p, { row: sel, result, undo }]);
        setSelId(next?.id ?? null);
        void refresh();
      } catch (e) {
        // The row stays: a failed action must not look processed.
        setError(`${action.label} failed — ${e instanceof Error ? e.message : "the request failed"}`);
      } finally {
        busy.current = false;
      }
    },
    [sel, shown, refresh],
  );

  const trigger = useCallback(
    (action: TriageAction) => (action.confirm ? setConfirming(action) : void run(action)),
    [run],
  );

  const undo = useCallback(async () => {
    const last = processed[processed.length - 1];
    if (!last || busy.current) return;
    busy.current = true;
    setError(null);
    try {
      await last.undo();
      setProcessed((p) => p.slice(0, -1));
      setTab("all");
      setSelId(last.row.id);
      void refresh();
    } catch (e) {
      setError(`Undo failed — ${e instanceof Error ? e.message : "the request failed"}`);
    } finally {
      busy.current = false;
    }
  }, [processed, refresh]);

  const move = useCallback(
    (dir: number) => {
      if (!shown.length) return;
      const i = Math.max(0, sel ? shown.indexOf(sel) : 0);
      setSelId(shown[Math.min(shown.length - 1, Math.max(0, i + dir))].id);
    },
    [shown, sel],
  );

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.metaKey || e.ctrlKey || e.altKey) return;
      const t = e.target as HTMLElement | null;
      // Letter keys never fire while focus is in a text field.
      if (t && (/^(input|textarea|select)$/i.test(t.tagName) || t.isContentEditable)) return;
      const k = e.key.toLowerCase();
      if (confirming) {
        if (e.key === "Enter") { if (t?.tagName !== "BUTTON") { e.preventDefault(); void run(confirming); } }
        else if (e.key === "Escape") setConfirming(null);
        return;
      }
      if (k === "j" || e.key === "ArrowDown") { move(1); e.preventDefault(); return; }
      if (k === "k" || e.key === "ArrowUp") { move(-1); e.preventDefault(); return; }
      if (e.key === "Backspace" || k === "z") { void undo(); e.preventDefault(); return; }
      const tabHit = TABS.find((x) => x.key === e.key);
      if (tabHit) { setTab(tabHit.id); setSelId(null); return; }
      if (e.key === "Enter") {
        // A focused button handles its own Enter; running the primary too would fire twice.
        if (t?.tagName === "BUTTON") return;
        const primary = actions.find((a) => a.primary);
        if (primary) { e.preventDefault(); trigger(primary); }
        return;
      }
      if (k === "c") {
        const ta = document.querySelector<HTMLTextAreaElement>("textarea[data-triage-comment]");
        if (ta) { ta.focus(); e.preventDefault(); }
        return;
      }
      const hit = actions.find((a) => a.key === k);
      if (hit) { e.preventDefault(); trigger(hit); }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [actions, confirming, move, run, trigger, undo]);

  const loading = feedQ.isLoading;
  const failedRead = feedQ.isError && !feedQ.data;
  const failedSources = feedQ.data?.failed_sources ?? [];
  const total = live.length + processed.length;

  return (
    <div className="flex h-full min-h-0 flex-col">
      <PlaceHeader
        viewName="Triage"
        purpose={
          <span className="flex items-center gap-2.5">
            What is waiting on a person. Clustering reasons over this project's code graph only — overlaps across repos are not computed.
            <span className="rounded-full border border-st-next/30 bg-st-next/[0.07] px-2 py-0.5 font-mono text-meta uppercase tracking-[0.06em] text-st-next">
              per-project · {active?.tag ?? "—"}
            </span>
          </span>
        }
      />

      <div className="flex flex-wrap items-center gap-4 border-b border-line px-6 py-2.5 font-mono text-meta text-muted">
        <span>
          <span className="text-fg-2">{loading || failedRead ? "—" : live.length}</span> waiting ·{" "}
          <span className="text-fg-2">{processed.length}</span> processed this session
        </span>
        <div
          className="h-1 w-40 overflow-hidden rounded-full bg-surface-3"
          role="progressbar"
          aria-label="Processed this session"
          aria-valuemin={0}
          aria-valuemax={total}
          aria-valuenow={processed.length}
        >
          <div
            className="h-full bg-accent"
            style={{ width: total ? `${(processed.length / total) * 100}%` : "0%" }}
          />
        </div>
        <span className="flex-1" />
        <span className="text-faint-2">J K move · 1–5 tabs · A…Z act · ⌫ undo</span>
      </div>

      {failedSources.length > 0 && (
        <div role="alert" className="border-b border-st-review/30 bg-st-review/[0.07] px-6 py-2 text-small text-st-review">
          Did not answer: {failedSources.join(", ")}. Those rows are missing from this queue —
          it is not clear.
        </div>
      )}
      {error && (
        <div role="alert" className="border-b border-st-blocked/30 bg-st-blocked/[0.07] px-6 py-2 text-small text-st-blocked">
          {error}
        </div>
      )}

      <div className="grid min-h-0 flex-1 lg:grid-cols-[360px_1fr]">
        <div className="flex min-h-0 flex-col border-r border-line">
          <div className="flex gap-1 border-b border-line px-3 py-2">
            {TABS.map((t) => {
              const n = feedQ.data?.counts[t.id];
              const on = tab === t.id;
              return (
                <button
                  key={t.id}
                  onClick={() => { setTab(t.id); setSelId(null); }}
                  aria-pressed={on}
                  className={`flex items-center gap-1.5 rounded-md border px-2 py-1 font-mono text-meta ${
                    on ? "border-control bg-surface-3 text-fg" : "border-transparent text-muted hover:text-fg-2"
                  }`}
                >
                  {t.dot && <span className="h-1.5 w-1.5 rounded-full" style={{ background: t.dot }} />}
                  {t.label}
                  <span className="text-faint-2">{n ?? "·"}</span>
                </button>
              );
            })}
          </div>

          <div className="min-h-0 flex-1 overflow-y-auto" role="listbox" aria-label="Triage queue">
            {failedRead ? (
              <PlannerError message={FETCH_FAILED} onRetry={() => void feedQ.refetch()} />
            ) : loading ? (
              <p className="px-4 py-6 font-mono text-meta text-muted" aria-busy="true">
                Loading the triage queue…
              </p>
            ) : shown.length === 0 ? (
              <EmptyState tab={tab} processed={processed.length} partial={failedSources.length > 0} />
            ) : (
              shown.map((r) => {
                const on = r.id === sel?.id;
                return (
                  <div
                    key={r.id}
                    role="option"
                    aria-selected={on}
                    onClick={() => setSelId(r.id)}
                    className={`cursor-pointer border-b border-line px-3.5 py-2.5 ${on ? "bg-surface-3" : "hover:bg-surface-2"}`}
                  >
                    <div className="flex items-start gap-2.5">
                      <span
                        className="mt-0.5 shrink-0 rounded border px-1.5 py-px font-mono text-micro uppercase tracking-[0.04em]"
                        style={{ color: kindColor(r), borderColor: `${kindColor(r)}44` }}
                      >
                        {kindLabel(r)}
                      </span>
                      <span className={`min-w-0 flex-1 text-body leading-snug ${on ? "text-fg" : "text-fg-2"}`}>{r.title}</span>
                      <span className="shrink-0 font-mono text-meta text-faint-2">{formatAge(r.age_seconds)}</span>
                    </div>
                    <div className="mt-1 font-mono text-meta text-faint-2">
                      {r.kind} · {r.id} · {metaOf(r)}
                    </div>
                  </div>
                );
              })
            )}
          </div>

          <div className="border-t border-line px-3.5 py-2.5">
            <div className="flex items-center gap-2 font-mono text-micro uppercase tracking-[0.07em] text-faint-2">
              <span className="flex-1">Processed</span>
              <button
                onClick={() => void undo()}
                disabled={!processed.length}
                className="rounded border border-control px-1.5 py-px normal-case tracking-normal text-muted enabled:hover:text-fg disabled:opacity-40"
              >
                Undo last <kbd>⌫</kbd>
              </button>
            </div>
            <ul className="mt-1.5 space-y-1">
              {processed.slice(-5).reverse().map((p) => (
                <li key={p.row.id} className="truncate text-small text-muted">
                  <span className="font-mono text-faint-2">{p.row.id}</span> {p.result}
                </li>
              ))}
            </ul>
          </div>
        </div>

        <div className="min-h-0 overflow-y-auto">
          {sel ? (
            <TriageDetail row={sel} actions={actions} onAction={trigger} />
          ) : (
            <p className="px-6 py-10 text-small text-muted">Nothing selected.</p>
          )}
        </div>
      </div>

      {confirming && (
        <div role="dialog" aria-modal="true" aria-label={`Confirm ${confirming.label}`}
          className="fixed inset-0 z-50 flex items-center justify-center bg-black/50">
          <div className="w-[380px] rounded-[13px] border border-line-2 bg-surface-2 p-5">
            <div className="text-lead font-semibold">{confirming.label}?</div>
            <p className="mt-2 text-small leading-relaxed text-muted">{confirming.confirm}</p>
            <div className="mt-4 flex justify-end gap-2">
              <button onClick={() => setConfirming(null)}
                className="h-7 rounded-md border border-control px-3 text-small text-muted hover:text-fg">
                Cancel <kbd className="font-mono">esc</kbd>
              </button>
              <button autoFocus onClick={() => void run(confirming)}
                className="h-7 rounded-md bg-accent px-3 text-small font-semibold text-bg">
                {confirming.label} <kbd className="font-mono">↵</kbd>
              </button>
            </div>
          </div>
        </div>
      )}
    </div>
  );
}

function EmptyState({ tab, processed, partial }: { tab: string; processed: number; partial: boolean }) {
  if (tab !== "all") {
    const name = TABS.find((t) => t.id === tab)?.label.toLowerCase();
    return <p className="px-4 py-6 text-small text-muted">No {name} waiting.</p>;
  }
  return (
    <div className="px-4 py-8 text-center">
      <div className="text-[13px] font-semibold text-fg-2">{partial ? "Nothing to show" : "Triage clear"}</div>
      <p className="mx-auto mt-1.5 max-w-[34ch] text-[11.5px] leading-relaxed text-muted">
        {partial
          ? "Some sources did not answer, so this is not a clear queue."
          : processed
            ? `You processed ${processed} this session. Nothing else is waiting.`
            : "Nothing is waiting. History lives in the tracker."}
      </p>
    </div>
  );
}
