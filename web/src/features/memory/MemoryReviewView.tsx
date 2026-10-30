import { AlertTriangle, Search, SlidersHorizontal, X } from "lucide-react";
import * as React from "react";
import { Link } from "react-router-dom";

import { FETCH_FAILED, PlannerError } from "@/components/planner/PlannerStates";
import { PlaceHeader } from "@/components/shell/PlaceHeader";
import { cn } from "@/lib/cn";
import { settingsPath } from "@/lib/routes";
import { useProjectCtx } from "@/features/ProjectContext";
import {
  useAutoActions,
  useCandidateClusters,
  useJudgeShard,
  useJudgeStatus,
  useMemoryQueues,
  usePromoteCluster,
  useReviewShard,
  useScoredCandidates,
  useSetShardClass,
  useStaleActions,
  useStaleShards,
  useUndoAutoShard,
} from "@/lib/queries";
import type { CandidateJudge, JudgeStatus } from "@/lib/types";

import { MemoryDetail, TONE, type Decision } from "./MemoryDetail";
import {
  AGE_BANDS,
  CONF_BANDS,
  QUEUES,
  QUEUE_BY_ID,
  SOURCE_LABEL,
  UNVETTED_SOURCES,
  AUTO_PUBLISH_MIN,
  AUTO_REJECT_DUPE_MIN,
  buildRows,
  confBand,
  everywhere,
  filterRows,
  formatAge,
  sortRows,
  type Facets,
  type QueueId,
  type Row,
  type SortId,
} from "./queues";

export { UNVETTED_SOURCES };

const PAGE = 50;
const SWEEP_FLOOR = 0.85;
const CLEAR = "Queue clear. Pick another queue above, or search everywhere.";

interface Outcome {
  message: string;
  /** Rows to take off the list now; the refetch confirms it. */
  hide: string[];
  undo?: () => Promise<unknown>;
}

interface Toast {
  message: string;
  undo?: () => Promise<unknown>;
  hidden: string[];
}

/**
 * Memory — the triage console for agent-written memory (GRPH-1176, design Memory.dc.html).
 *
 * Five queues with their counts from `/memory/queues` (GRPH-1175). Three states stay apart
 * because each reads as "nothing waiting" if merged: still loading, a queue whose source
 * failed, and a queue that is truly clear. Only the last may say "Queue clear".
 */
export function MemoryReviewView() {
  const { activeId, active } = useProjectCtx();
  const judgeOn = Boolean(active?.memory_llm_judge);

  const queuesQ = useMemoryQueues(activeId);
  const scoredQ = useScoredCandidates(activeId);
  const clustersQ = useCandidateClusters(activeId);
  const staleQ = useStaleShards(activeId);
  const autoQ = useAutoActions(activeId);
  const { data: judgeStatus } = useJudgeStatus(activeId);

  const { publish, reject } = useReviewShard();
  const promote = usePromoteCluster();
  const undoAuto = useUndoAutoShard();
  const judge = useJudgeShard();
  const staleActs = useStaleActions();
  const setClass = useSetShardClass();

  const [queue, setQueue] = React.useState<QueueId>("needs");
  const [search, setSearch] = React.useState("");
  const [scope, setScope] = React.useState<"queue" | "all">("queue");
  const [facets, setFacets] = React.useState<Facets>({});
  const [sort, setSort] = React.useState<SortId>("conf");
  const [sel, setSel] = React.useState<Set<string>>(() => new Set());
  const [activeRowId, setActiveRowId] = React.useState<string | null>(null);
  const [hidden, setHidden] = React.useState<Set<string>>(() => new Set());
  const [canon, setCanon] = React.useState<Record<string, string>>({});
  const [asked, setAsked] = React.useState<Record<string, CandidateJudge>>({});
  const [limit, setLimit] = React.useState(PAGE);
  const [toast, setToast] = React.useState<Toast | null>(null);
  const [error, setError] = React.useState<string | null>(null);
  const busy = React.useRef(false);
  const toastTimer = React.useRef<ReturnType<typeof setTimeout>>(null);
  const now = React.useMemo(() => Date.now(), []);

  const sources: Record<QueueId, { isError: boolean; isPending: boolean; refetch: () => unknown }> = {
    needs: scoredQ, conflicts: scoredQ, dupes: clustersQ, stale: staleQ, unvetted: autoQ,
  };
  const all = React.useMemo(
    () => buildRows({ scored: scoredQ.data, clusters: clustersQ.data, stale: staleQ.data, auto: autoQ.data }),
    [scoredQ.data, clustersQ.data, staleQ.data, autoQ.data],
  );
  const live = (rows: Row[] | undefined) => rows?.filter((r) => !hidden.has(r.id));

  const searching = scope === "all" && search.trim() !== "";
  const def = QUEUE_BY_ID[queue];
  const src = sources[queue];
  const queueRows = live(all[queue]);
  // GRPH-916: a failed read is never an endless skeleton, and never an empty queue.
  const awaitingFirstPayload = !searching && queueRows === undefined && !src.isError;
  const listFailed = !searching && src.isError && queueRows === undefined;
  const pool = searching ? live(everywhere(all)) ?? [] : queueRows ?? [];
  const list = sortRows(filterRows(pool, search, facets, now), sort);
  const shown = list.slice(0, limit);
  const activeRow = shown.find((r) => r.id === activeRowId) ?? shown[0] ?? null;
  const selected = shown.filter((r) => sel.has(r.id));
  const failedSearch = searching
    ? QUEUES.filter((q) => sources[q.id].isError && all[q.id] === undefined).map((q) => q.label)
    : [];

  React.useEffect(() => setLimit(PAGE), [queue, search, scope, facets, sort]);

  function flash(t: Toast) {
    if (toastTimer.current) clearTimeout(toastTimer.current);
    setToast(t);
    toastTimer.current = setTimeout(() => setToast(null), 6000);
  }

  function pickQueue(id: QueueId) {
    setQueue(id);
    setScope("queue");
    setSel(new Set());
    setActiveRowId(null);
  }

  function toggle(id: string) {
    setSel((s) => {
      const n = new Set(s);
      if (n.has(id)) n.delete(id);
      else n.add(id);
      return n;
    });
  }

  /** Run one action over rows: per-row outcome, so a partial failure says which part failed. */
  async function each(rows: Row[], verb: string, fn: (r: Row) => Promise<unknown>, undo?: (r: Row) => Promise<unknown>): Promise<Outcome> {
    const res = await Promise.allSettled(rows.map(fn));
    const ok = rows.filter((_, i) => res[i].status === "fulfilled");
    const failed = rows.length - ok.length;
    if (!ok.length) {
      const why = res.find((r): r is PromiseRejectedResult => r.status === "rejected")?.reason;
      throw new Error(why instanceof Error ? why.message : "the request failed");
    }
    const what = ok.length === 1 ? ok[0].id : `${ok.length} notes`;
    return {
      message: `${verb} ${what}${failed ? ` · ${failed} failed` : ""}`,
      hide: ok.map((r) => r.id),
      undo: undo ? () => Promise.allSettled(ok.map(undo)) : undefined,
    };
  }

  const backToQueue = (r: Row) => undoAuto.mutateAsync(r.id);

  const runners: Record<string, (rows: Row[]) => Promise<Outcome>> = {
    publish: (rows) => each(rows, "Published", (r) => publish.mutateAsync(r.id), backToQueue),
    reject: (rows) => each(rows, "Rejected", (r) => reject.mutateAsync(r.id), backToQueue),
    judge: async (rows) => {
      const res = await Promise.allSettled(rows.map((r) => judge.mutateAsync(r.id)));
      const got: Record<string, CandidateJudge> = {};
      res.forEach((r, i) => { if (r.status === "fulfilled") got[rows[i].id] = r.value; });
      setAsked((a) => ({ ...a, ...got }));
      const n = Object.keys(got).length;
      if (!n) throw new Error("the judge did not answer");
      return { message: `Asked the judge about ${n === 1 ? rows[0].id : `${n} notes`}`, hide: [] };
    },
    merge: (rows) =>
      each(
        rows,
        "Merged",
        (r) => {
          const c = r.cluster!;
          const ids = [c.representative.id, ...c.members.map((m) => m.id)];
          const keep = canon[r.id] ?? c.representative.id;
          return promote.mutateAsync({ publishId: keep, rejectIds: ids.filter((i) => i !== keep) });
        },
        (r) => Promise.allSettled([r.cluster!.representative, ...r.cluster!.members].map((m) => undoAuto.mutateAsync(m.id))),
      ),
    apply: async (rows) => {
      const res = await staleActs.apply.mutateAsync(rows.map((r) => r.id));
      const applied = res.filter((r) => r.applied);
      const left = res.length - applied.length;
      const restorable = applied.filter((r) => r.undo === "restore").map((r) => r.shard_id);
      const one = res.length === 1 && !applied.length ? ` — ${res[0].reason}` : "";
      return {
        message: `Applied ${applied.length}${left ? ` · ${left} left for a person${one}` : ""}`,
        hide: applied.map((r) => r.shard_id),
        undo: restorable.length ? () => Promise.allSettled(restorable.map((id) => staleActs.restore.mutateAsync(id))) : undefined,
      };
    },
    retire: (rows) =>
      each(rows, "Retired", (r) => staleActs.retire.mutateAsync(r.id), (r) => staleActs.restore.mutateAsync(r.id)),
    keep: (rows) => each(rows, "Kept", (r) => staleActs.keep.mutateAsync(r.id)),
    reverify: async (rows) => runners.judge(rows),
    retract: (rows) => each(rows, "Retracted", (r) => undoAuto.mutateAsync(r.id)),
  };

  function decisionsFor(r: Row | null): Decision[] {
    if (!r) return [];
    const judgeOff = judgeOn ? undefined : "The LLM judge is off for this project";
    switch (r.queue) {
      case "needs":
      case "conflicts":
        return [
          r.queue === "needs"
            ? { id: "publish", label: "Publish", sub: "Becomes searchable ground truth for the next agent", key: "P", tone: "lime" }
            : { id: "publish", label: "Publish this anyway", sub: "Published over the contradiction — retire the old shard from Stale", key: "P", tone: "lime" },
          r.queue === "needs"
            ? { id: "reject", label: "Reject", sub: "Removed from the queue; kept for provenance", key: "R", tone: "neutral" }
            : { id: "reject", label: "Keep published · reject this", sub: "The existing shard stands", key: "R", tone: "neutral" },
          { id: "judge", label: "Ask the judge", sub: "Re-score keep/quality. Does not replace groundedness on the list.", key: "A", tone: "purple", disabled: judgeOff },
        ];
      case "dupes": {
        const drop = r.cluster ? r.cluster.members.length : 0;
        return [{ id: "merge", label: `Merge into selected · drop ${drop}`, sub: "Publish the chosen wording; reject the rest", key: "M", tone: "lime" }];
      }
      case "stale": {
        const rec = r.stale?.recommendation;
        return [
          {
            id: "apply", label: rec ? `Apply · ${rec.action}` : "Apply recommendation",
            sub: rec?.action === "retire" ? "Retire as the judge recommends. Recoverable for 14 days" : "Only a retire changes anything; the rest is reported back",
            key: "Y", tone: "lime", disabled: rec ? undefined : "Ungraded — there is no recommendation to apply",
          },
          { id: "retire", label: "Retire", sub: "Unpublish; agents stop seeing it. Recoverable for 14 days", key: "E", tone: "red" },
          { id: "reverify", label: "Re-verify", sub: "Ask the judge to re-read it now", key: "V", tone: "purple", disabled: judgeOff },
          { id: "keep", label: "Keep", sub: "Still right: stays published and leaves Stale until it ages out again", key: "K", tone: "neutral" },
        ];
      }
      case "unvetted":
        return [
          { id: "retract", label: "Retract", sub: "Unpublish and send back to Needs a decision", key: "R", tone: "red" },
          { id: "retire", label: "Retire", sub: "Unpublish; recoverable for 14 days", key: "E", tone: "neutral" },
        ];
    }
  }

  const decisions = decisionsFor(activeRow);
  // Bulk acts on one queue at a time: the same verb means different things across queues
  // (a "Reject" on a published stale row would skip its 14-day retire path).
  const selQueues = [...new Set(selected.map((r) => QUEUE_BY_ID[r.queue].label))];
  const bulk = selQueues.length === 1 ? decisionsFor(selected[0]).filter((d) => !d.disabled) : [];

  async function run(id: string, rows: Row[]) {
    if (!rows.length || busy.current) return;
    busy.current = true;
    setError(null);
    const label = decisions.concat(bulk).find((d) => d.id === id)?.label ?? id;
    const i = activeRow ? list.indexOf(activeRow) : -1;
    try {
      const out = await runners[id](rows);
      if (out.hide.length) {
        setHidden((h) => new Set([...h, ...out.hide]));
        setSel((s) => new Set([...s].filter((x) => !out.hide.includes(x))));
        const next = list.slice(i + 1).find((r) => !out.hide.includes(r.id)) ?? list.slice(0, Math.max(0, i)).reverse().find((r) => !out.hide.includes(r.id));
        setActiveRowId(next?.id ?? null);
      }
      flash({ message: out.message, undo: out.undo, hidden: out.hide });
    } catch (e) {
      // The row stays: a failed action must not look processed.
      setError(`${label} failed — ${e instanceof Error ? e.message : "the request failed"}`);
    } finally {
      busy.current = false;
    }
  }

  async function undo() {
    const t = toast;
    if (!t?.undo) return;
    setToast(null);
    try {
      await t.undo();
      setHidden((h) => new Set([...h].filter((x) => !t.hidden.includes(x))));
    } catch (e) {
      setError(`Undo failed — ${e instanceof Error ? e.message : "the request failed"}`);
    }
  }

  const keyState = React.useRef({ shown, activeRow, decisions, run, toggle });
  keyState.current = { shown, activeRow, decisions, run, toggle };
  React.useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.metaKey || e.ctrlKey || e.altKey) return;
      const t = e.target as HTMLElement | null;
      // Letter keys never fire while focus is in a text field.
      if (t && (/^(input|textarea|select)$/i.test(t.tagName) || t.isContentEditable)) return;
      const { shown: L, activeRow: cur, decisions: D, run: go, toggle: flip } = keyState.current;
      if (!L.length || !cur) return;
      const k = e.key.toLowerCase();
      const i = Math.max(0, L.indexOf(cur));
      if (k === "j" || k === "k" || e.key === "ArrowDown" || e.key === "ArrowUp") {
        const dir = k === "j" || e.key === "ArrowDown" ? 1 : -1;
        setActiveRowId(L[Math.min(L.length - 1, Math.max(0, i + dir))].id);
        e.preventDefault();
        return;
      }
      if (k === "x") { flip(cur.id); e.preventDefault(); return; }
      let hit: Decision | undefined;
      if (e.key === "Enter") {
        // A focused button handles its own Enter; running the first action too would fire twice.
        if (t?.tagName === "BUTTON") return;
        hit = D.find((d) => !d.disabled);
      } else {
        hit = D.find((d) => d.key && d.key.toLowerCase() === k && !d.disabled);
      }
      if (hit) { e.preventDefault(); void go(hit.id, [cur]); }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, []);

  // CLEANUP: the one-pass sweep each queue offers over what is clearly decided.
  const sweep = (() => {
    const rows = queueRows ?? [];
    if (searching) return null;
    if (queue === "needs") {
      const t = rows.filter((r) => r.scored?.judged && r.scored.grounded && r.scored.ready && (r.conf ?? 0) >= SWEEP_FLOOR);
      return t.length ? { text: `${t.length} ${t.length === 1 ? "is" : "are"} grounded, ready and ≥ ${SWEEP_FLOOR} — publish them in one pass.`, action: `Publish ${t.length}`, id: "publish", rows: t } : null;
    }
    if (queue === "dupes") {
      const drop = rows.reduce((n, r) => n + (r.cluster?.members.length ?? 0), 0);
      return rows.length ? { text: "Merge every cluster into its chosen wording. Dropped variants are rejected, kept for provenance.", action: `Merge ${rows.length} · drop ${drop}`, id: "merge", rows } : null;
    }
    if (queue === "stale") {
      const t = rows.filter((r) => (r.stale?.recommendation?.confidence ?? 0) >= SWEEP_FLOOR);
      if (!t.length) return null;
      const by = (a: string) => t.filter((r) => r.stale?.recommendation?.action === a).length;
      const parts = ["retire", "revise", "verify", "keep"].filter((a) => by(a)).map((a) => `${by(a)} ${a}`);
      return { text: `${t.length} have a recommendation at ≥ ${SWEEP_FLOOR} — ${parts.join(", ")}. Only retires change anything; retired shards are recoverable for 14 days.`, action: `Apply ${t.length}`, id: "apply", rows: t };
    }
    return null;
  })();

  const counts = queuesQ.data;
  const countOf = (id: QueueId): { text: string; failed: boolean; title?: string } => {
    if (queuesQ.isError) return { text: "failed", failed: true, title: "Queue counts did not load" };
    const c = counts?.[QUEUE_BY_ID[id].key];
    if (!c) return { text: "—", failed: false };
    if (c.state === "failed" || c.count == null) return { text: "failed", failed: true, title: c.error || "This queue's source failed" };
    return { text: c.count.toLocaleString(), failed: false };
  };
  const hintOf = (id: QueueId, hint: string) => {
    if (id !== "dupes" || !clustersQ.data) return hint;
    const n = clustersQ.data.reduce((sum, c) => sum + c.size, 0);
    return hint.replace(/^Notes/, `${n} ${n === 1 ? "note" : "notes"}`);
  };
  const queueTotal = (() => {
    const c = counts?.[def.key];
    return c && c.state === "ok" && c.count != null ? c.count : null;
  })();

  const agents = [...new Set(pool.map((r) => r.agent))].slice(0, 6);
  const filtering = search.trim() !== "" || !!facets.agent || !!facets.conf || !!facets.age;
  const allOn = shown.length > 0 && selected.length === shown.length;

  return (
    <div className="flex h-full min-h-0 flex-col">
      <PlaceHeader
        viewName="Memory"
        purpose={
          <>
            Triage for agent-written memory. Work the queues, clean up in bulk, and decide what the
            scorer couldn't. To browse what's already published, open{" "}
            <Link to="../lessons" relative="path">Lessons</Link>.
          </>
        }
        action={
          <Link
            to={settingsPath("project")}
            className="inline-flex h-8 items-center gap-2 rounded-[9px] border border-control bg-surface px-3 text-body text-fg-2 hover:border-control-hover hover:text-fg"
          >
            <SlidersHorizontal size={13} aria-hidden /> Memory preferences
          </Link>
        }
      />

      <PolicyStrip judgeStatus={judgeStatus} />

      <div className="flex min-h-0 flex-1 flex-col gap-3.5 overflow-y-auto px-6 pb-8 pt-3.5">
        <div role="tablist" aria-label="Memory queues" className="grid grid-cols-[repeat(auto-fit,minmax(170px,1fr))] gap-2">
          {QUEUES.map((q) => {
            const on = q.id === queue && !searching;
            const c = countOf(q.id);
            return (
              <button
                key={q.id}
                role="tab"
                aria-selected={on}
                data-testid={`queue-${q.id}`}
                onClick={() => pickQueue(q.id)}
                className={cn(
                  "flex flex-col gap-1 rounded-[12px] border px-3.5 py-3 text-left hover:border-control-hover",
                  on ? cn(q.ring, "bg-surface-2") : "border-control bg-surface",
                )}
              >
                <span className={cn("flex items-center gap-1.5 text-[12px] font-medium", on ? "text-fg" : "text-muted")}>
                  <span className={cn("h-[7px] w-[7px] rounded-[2px]", q.dot)} aria-hidden />
                  {q.label}
                </span>
                <span
                  title={c.title}
                  className={cn("text-[22px] font-semibold leading-none tracking-tight", c.failed ? "text-st-blocked" : "text-fg")}
                >
                  {c.text}
                </span>
                <span className="text-small text-faint">{c.failed ? c.title : hintOf(q.id, q.hint)}</span>
              </button>
            );
          })}
        </div>

        {judgeStatus?.falling_back && <JudgeFallbackBanner status={judgeStatus} />}
        {error && (
          <div role="alert" className="rounded-[10px] border border-st-blocked/30 bg-st-blocked/[0.07] px-3 py-2 text-small text-ty-bug">
            {error}
          </div>
        )}

        <div className="flex flex-wrap items-start gap-3.5">
          <section className="min-w-0 flex-[1_1_540px] overflow-hidden rounded-[12px] border border-line-2 bg-surface" aria-label="Queue">
            <div className="flex flex-wrap items-center gap-2 border-b border-line-2 px-3 py-2.5">
              <div className="relative min-w-[200px] flex-[1_1_240px]">
                <Search size={13} className="pointer-events-none absolute left-2.5 top-1/2 -translate-y-1/2 text-faint" aria-hidden />
                <input
                  value={search}
                  onChange={(e) => { setSearch(e.target.value); setActiveRowId(null); }}
                  placeholder={scope === "all" ? "Search every queue…" : `Search ${def.label.toLowerCase()}…`}
                  aria-label="Search memory"
                  className="h-8 w-full rounded-lg border border-control bg-bg pl-8 pr-2.5 text-body text-fg outline-none placeholder:text-faint focus:border-control-hover"
                />
              </div>
              <div className="flex overflow-hidden rounded-lg border border-control" role="group" aria-label="Search scope">
                {([["queue", "This queue"], ["all", "Everywhere"]] as const).map(([id, label]) => (
                  <button
                    key={id}
                    aria-pressed={scope === id}
                    onClick={() => { setScope(id); setSel(new Set()); setActiveRowId(null); }}
                    className={cn("h-[30px] px-2.5 text-small", scope === id ? "bg-surface-3 text-fg" : "text-faint hover:text-fg-2")}
                  >
                    {label}
                  </button>
                ))}
              </div>
              <div className="flex items-center gap-1" role="group" aria-label="Sort">
                <span className="mr-0.5 font-mono text-micro tracking-wide text-faint">SORT</span>
                {([["conf", "conf ↓"], ["new", "newest"], ["old", "oldest"]] as const).map(([id, label]) => (
                  <Chip key={id} on={sort === id} onClick={() => setSort(id)}>{label}</Chip>
                ))}
              </div>
            </div>

            <div className="flex flex-wrap items-center gap-x-4 gap-y-1.5 border-b border-line-2 px-3 py-2">
              <Facet label="AGENT" values={agents} on={facets.agent} pick={(v) => setFacets((f) => ({ ...f, agent: f.agent === v ? undefined : v }))} />
              <Facet label="CONF" values={CONF_BANDS} on={facets.conf} pick={(v) => setFacets((f) => ({ ...f, conf: f.conf === v ? undefined : v }))} />
              <Facet label="AGE" values={AGE_BANDS} on={facets.age} pick={(v) => setFacets((f) => ({ ...f, age: f.age === v ? undefined : v }))} />
            </div>

            {sweep && (
              <div className="flex flex-wrap items-center gap-x-3 gap-y-2 border-b border-line-2 bg-accent/[0.035] px-3 py-2.5" data-testid="memory-sweep">
                <span className="rounded border border-accent/35 px-1.5 py-px font-mono text-micro tracking-wide text-accent">CLEANUP</span>
                <span className="min-w-0 flex-[1_1_260px] text-[12px] leading-relaxed text-fg-2">{sweep.text}</span>
                <button
                  onClick={() => setSel(new Set(sweep.rows.map((r) => r.id)))}
                  title="Check the rows this would act on"
                  className="h-7 rounded-[7px] border border-control px-2.5 text-small text-muted hover:border-control-hover hover:text-fg"
                >
                  Preview
                </button>
                <button
                  onClick={() => void run(sweep.id, sweep.rows)}
                  className="h-7 rounded-[7px] border border-accent/45 bg-accent/10 px-3 text-small font-semibold text-accent hover:bg-accent/15"
                >
                  {sweep.action}
                </button>
              </div>
            )}

            {selected.length > 0 && (
              <div className="flex flex-wrap items-center gap-1.5 border-b border-line-2 bg-surface-3/60 px-3 py-2" role="toolbar" aria-label="Bulk actions">
                <span className="mr-1.5 text-[12px] font-semibold text-fg">{selected.length} selected</span>
                {selQueues.length > 1 && (
                  <span className="text-small text-muted" data-testid="bulk-mixed">
                    Spans {selQueues.join(", ")} — select rows from one queue to act in bulk.
                  </span>
                )}
                {bulk.map((d) => (
                  <button
                    key={d.id}
                    onClick={() => void run(d.id, selected.filter((r) => r.queue === selected[0].queue))}
                    className={cn("h-7 rounded-[7px] border px-2.5 text-small hover:brightness-125", TONE[d.tone], d.tone === "neutral" && "border-control")}
                  >
                    {d.label.split(" · ")[0]}
                  </button>
                ))}
                <span className="flex-1" />
                <button onClick={() => setSel(new Set())} className="text-small text-faint hover:text-muted">Clear</button>
              </div>
            )}

            <div className="grid grid-cols-[28px_minmax(0,1fr)_84px_52px_48px] items-center gap-2.5 border-b border-line-2 px-3 py-[7px] font-mono text-micro tracking-wide text-faint">
              <Box on={allOn} label="Select all" onClick={() => setSel(allOn ? new Set() : new Set(shown.map((r) => r.id)))} />
              <span>{searching ? "RESULTS · ALL QUEUES" : def.label.toUpperCase()}</span>
              <span>CLASS</span>
              <span className="text-right">CONF</span>
              <span className="text-right">AGE</span>
            </div>

            {failedSearch.length > 0 && (
              <p role="alert" className="border-b border-line-2 bg-st-review/[0.07] px-3 py-2 text-small text-st-review">
                Did not load: {failedSearch.join(", ")}. These results are partial.
              </p>
            )}

            {listFailed ? (
              <PlannerError title={`${def.label} unavailable`} message={FETCH_FAILED} onRetry={() => void src.refetch()} />
            ) : awaitingFirstPayload ? (
              <ListSkeleton />
            ) : shown.length === 0 ? (
              <div className="px-5 py-12 text-center text-body text-muted" data-testid="memory-empty">
                {filtering ? "Nothing matches. Clear a filter or search everywhere." : CLEAR}
              </div>
            ) : (
              <div role="listbox" aria-label={searching ? "Search results" : def.label} aria-multiselectable="true">
                {shown.map((r) => (
                  <RowView
                    key={`${r.queue}:${r.id}`}
                    row={r}
                    now={now}
                    active={r.id === activeRow?.id}
                    checked={sel.has(r.id)}
                    showQueue={searching}
                    onOpen={() => setActiveRowId(r.id)}
                    onToggle={() => toggle(r.id)}
                  />
                ))}
              </div>
            )}

            <div className="flex flex-wrap items-center gap-2.5 px-3 py-2.5 text-small text-faint">
              <span>
                {searching
                  ? `${list.length} matches`
                  : `Showing ${shown.length} of ${queueTotal != null ? queueTotal.toLocaleString() : list.length}`}
              </span>
              <span className="flex-1" />
              <span className="font-mono text-meta">J/K move · X select · ⏎ first action</span>
              {list.length > shown.length && (
                <button
                  onClick={() => setLimit((n) => n + PAGE)}
                  className="h-[26px] rounded-[7px] border border-control px-2.5 text-small text-muted hover:border-control-hover hover:text-fg"
                >
                  Load 50 more
                </button>
              )}
            </div>
          </section>

          <aside className="sticky top-0 min-w-[300px] max-w-[440px] flex-[1_1_340px] rounded-[12px] border border-line-2 bg-surface-2" aria-label="Selected memory">
            {activeRow ? (
              <MemoryDetail
                row={activeRow}
                pos={`${list.indexOf(activeRow) + 1} / ${list.length}`}
                decisions={decisions}
                onDecide={(d) => void run(d.id, [activeRow])}
                canonId={canon[activeRow.id] ?? activeRow.cluster?.representative.id ?? activeRow.id}
                onCanon={(id) => setCanon((c) => ({ ...c, [activeRow.id]: id }))}
                asked={asked[activeRow.id]}
                onClass={(c) => setClass.mutate({ id: activeRow.id, shardClass: c })}
              />
            ) : (
              <p className="px-5 py-10 text-center text-body leading-relaxed text-muted">Nothing selected.</p>
            )}
          </aside>
        </div>
      </div>

      {toast && (
        <div role="status" className="fixed bottom-5 left-1/2 z-50 flex -translate-x-1/2 items-center gap-3.5 rounded-[10px] border border-line bg-surface-3 py-2 pl-3.5 pr-3 text-body text-fg shadow-lg">
          <span>{toast.message}</span>
          {toast.undo && (
            <button onClick={() => void undo()} className="font-semibold text-accent hover:text-accent-hi">Undo</button>
          )}
          <button onClick={() => setToast(null)} aria-label="Dismiss" className="text-faint hover:text-fg">
            <X size={14} />
          </button>
        </div>
      )}
    </div>
  );
}

function RowView({
  row: r, now, active, checked, showQueue, onOpen, onToggle,
}: {
  row: Row; now: number; active: boolean; checked: boolean; showQueue: boolean; onOpen: () => void; onToggle: () => void;
}) {
  const s = r.scored;
  const flags: { label: string; cls: string }[] = [];
  if (showQueue) flags.push({ label: QUEUE_BY_ID[r.queue].label, cls: QUEUE_BY_ID[r.queue].tag });
  if (r.queue === "conflicts" || (s?.conflicts?.length ?? 0) > 0) flags.push({ label: `vs ${s?.conflicts?.[0] ?? ""}`.slice(0, 40), cls: TONE.red });
  if (r.cluster) flags.push({ label: `cluster · ${r.cluster.size}`, cls: QUEUE_BY_ID.dupes.tag });
  if (s?.judged) {
    if (s.judge_source === "decider") flags.push({ label: "decider", cls: TONE.neutral });
    if (!s.grounded) flags.push({ label: "ungrounded", cls: TONE.red });
    if (!s.ready) flags.push({ label: "not ready", cls: QUEUE_BY_ID.dupes.tag });
  } else if (s?.ungraded_reason?.startsWith("not scored yet")) {
    flags.push({ label: "not scored yet", cls: TONE.neutral });
  }
  const rec = r.stale?.recommendation;
  if (rec) flags.push({ label: `→ ${rec.action}`, cls: rec.action === "retire" ? TONE.red : rec.action === "keep" ? TONE.neutral : TONE.purple });
  else if (r.stale) flags.push({ label: "ungraded", cls: TONE.neutral });
  if (r.queue === "unvetted" && r.shard.scoring_source) {
    const unvetted = UNVETTED_SOURCES.includes(r.shard.scoring_source);
    flags.push({ label: SOURCE_LABEL[r.shard.scoring_source] ?? r.shard.scoring_source, cls: unvetted ? TONE.purple : TONE.neutral });
  }
  const confCls = r.conf == null ? "text-faint" : r.conf >= 0.85 ? "text-st-done" : r.conf >= 0.55 ? "text-fg-2" : "text-st-review";

  return (
    <div
      role="option"
      aria-selected={active}
      data-row-id={r.id}
      onClick={onOpen}
      className={cn(
        "grid cursor-pointer grid-cols-[28px_minmax(0,1fr)_84px_52px_48px] items-start gap-2.5 border-b border-line-2/70 px-3 py-2.5 hover:bg-surface-3/60",
        active ? "bg-surface-2 shadow-[inset_2px_0_0_var(--color-accent)]" : checked && "bg-accent/[0.04]",
      )}
    >
      <Box on={checked} label={`Select ${r.id}`} onClick={(e) => { e.stopPropagation(); onToggle(); }} />
      <div className="min-w-0">
        <p className="line-clamp-2 text-body leading-normal text-fg">{r.text}</p>
        <div className="mt-1 flex flex-wrap items-center gap-x-2 gap-y-1 font-mono text-meta text-faint">
          <span className="text-muted">{r.id}</span>
          <span>{r.agent}{r.source ? ` · ${r.source}` : ""}</span>
          {flags.map((f) => (
            <span key={f.label} className={cn("rounded border px-[5px] py-px text-micro uppercase tracking-wide", f.cls)}>{f.label}</span>
          ))}
        </div>
      </div>
      <span data-testid="row-class" className="font-mono text-meta text-muted">{r.shard.shard_class || "—"}</span>
      <span className={cn("text-right font-mono text-small", confCls)} title={r.conf == null ? "Not scored" : confBand(r.conf) ?? undefined}>
        {r.conf == null ? "—" : r.conf.toFixed(2)}
      </span>
      <span className="text-right font-mono text-meta text-faint">{formatAge(r.createdMs, now)}</span>
    </div>
  );
}

function Box({ on, label, onClick }: { on: boolean; label: string; onClick: (e: React.MouseEvent) => void }) {
  return (
    <button
      aria-pressed={on}
      aria-label={label}
      onClick={onClick}
      className={cn(
        "mt-0.5 flex h-4 w-4 items-center justify-center rounded border p-0 text-[10px] leading-none text-bg",
        on ? "border-accent bg-accent" : "border-control",
      )}
    >
      {on ? "✓" : ""}
    </button>
  );
}

function Chip({ on, onClick, children }: { on: boolean; onClick: () => void; children: React.ReactNode }) {
  return (
    <button
      aria-pressed={on}
      onClick={onClick}
      className={cn(
        "rounded-md border px-2 py-[3px] font-mono text-micro uppercase tracking-wide",
        on ? "border-control bg-surface-3 text-fg" : "border-control text-faint hover:border-control-hover hover:text-fg-2",
      )}
    >
      {children}
    </button>
  );
}

function Facet<T extends string>({ label, values, on, pick }: { label: string; values: readonly T[]; on?: T; pick: (v: T) => void }) {
  if (!values.length) return null;
  return (
    <div className="flex flex-wrap items-center gap-1" role="group" aria-label={`Filter by ${label.toLowerCase()}`}>
      <span className="mr-0.5 font-mono text-micro tracking-wide text-faint">{label}</span>
      {values.map((v) => (
        <Chip key={v} on={on === v} onClick={() => pick(v)}>{v}</Chip>
      ))}
    </div>
  );
}

function ListSkeleton() {
  return (
    <div aria-busy="true" aria-label="Loading memory review" className="flex flex-col">
      {Array.from({ length: 5 }, (_, i) => (
        <div key={i} className="border-b border-line-2/70 px-3 py-3">
          <div className="mb-2 h-3 w-4/5 animate-pulse rounded bg-surface-3" />
          <div className="h-2.5 w-1/3 animate-pulse rounded bg-surface-3" />
        </div>
      ))}
    </div>
  );
}

/** The project's memory policy, read from the project and the judge's live status. */
function PolicyStrip({ judgeStatus }: { judgeStatus?: JudgeStatus }) {
  const { active } = useProjectCtx();
  const policy: [string, string][] = [
    ["Auto-publish ≥", active ? (active.memory_write_mode === "auto" ? AUTO_PUBLISH_MIN.toFixed(2) : "off") : "—"],
    ["Auto-reject dupes ≥", active ? (active.memory_auto_reject ? AUTO_REJECT_DUPE_MIN.toFixed(2) : "off") : "—"],
    ["LLM judge", active ? (active.memory_llm_judge ? "on" : "off") : "—"],
    ["Expire candidates", active ? `${active.memory_candidate_expiry_days ?? 45}d` : "—"],
    ["Grading", judgeStatus ? (judgeStatus.falling_back ? `${judgeStatus.judge} (fallback)` : judgeStatus.judge) : "—"],
  ];
  return (
    <div className="mx-6 mt-3.5 flex flex-wrap items-center gap-x-4 gap-y-1.5 rounded-[10px] border border-line-2 bg-surface px-3 py-2" data-testid="memory-policy">
      <span className="font-mono text-micro tracking-[0.07em] text-faint">PROJECT POLICY</span>
      {policy.map(([k, v]) => (
        <span key={k} className="text-small text-muted">
          {k} <span className="font-mono text-fg-2">{v}</span>
        </span>
      ))}
      <span className="flex-1" />
      <Link to={settingsPath("project")} className="text-small">Change in project preferences →</Link>
    </div>
  );
}

/** The judge this project asked for cannot answer (GRPH-995).
 *
 *  Rendered above the queue, and above an EMPTY queue too. A decider that probed `valid` and
 *  then failed every `decide()` leaves rows that look merely unasked, so the page used to
 *  show nothing at all — and the reassuring reading of a quiet queue is always that nothing
 *  is wrong with it. The copy states what IS grading these candidates, not only what is not. */
function JudgeFallbackBanner({ status }: { status: JudgeStatus }) {
  return (
    <div
      data-testid="judge-falling-back"
      className="flex items-start gap-2 rounded-[10px] border border-st-review/30 bg-st-review/[0.08] px-3 py-2 text-[12px] text-st-review"
    >
      <AlertTriangle size={13} className="mt-px flex-none" />
      <span className="min-w-0 flex-1 leading-relaxed">
        <span className="font-medium">This project is falling back to similarity.</span>{" "}
        {status.reason} Everything below is ungraded, not approved —{" "}
        <Link to={settingsPath("deployment/providers")} className="underline underline-offset-2 hover:text-fg-2">
          fix the credential in AI providers
        </Link>
        .
      </span>
    </div>
  );
}
