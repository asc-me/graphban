import { ArrowUp, Check, Copy, Inbox, MessageSquare, Radar } from "lucide-react";
import { useProjectCtx } from "@/features/ProjectContext";
import { TYPE_META } from "@/lib/meta";
import { useAcceptRequest, useFleet, useTriageQueue, useVoteRequest } from "@/lib/queries";
import type { RequestType, TriageRow } from "@/lib/types";

/**
 * Screen 14 — Triage. Two questions on one page, because they are asked together:
 * *what came in*, and *what is already in flight that a new claim would collide with*.
 *
 * **Per-project, and the header says so.** Collision clustering reasons over one
 * project's code graph; `collision.py` and `clustering.py` scope every query by
 * `project_id`. A package name shared across repos is a galaxy edge, not a collision, and
 * a screen that implied otherwise would be promising an analysis nothing computes.
 */
export function TriageView() {
  const { active } = useProjectCtx();
  const projectId = active?.id ?? "";
  const { data: queue = [], isLoading } = useTriageQueue(projectId);
  const { data: fleet } = useFleet(projectId);
  const clusters = fleet?.clusters ?? [];

  return (
    <div className="max-w-[1300px] px-6 pb-16 pt-6">
      <div className="flex flex-wrap items-center gap-2.5">
        <h1 className="text-[19px] font-semibold tracking-[-0.3px]">Triage</h1>
        <span className="rounded-full border border-st-next/30 bg-st-next/[0.07] px-2 py-0.5 font-mono text-[9.5px] uppercase tracking-[0.06em] text-st-next">
          per-project · {active?.tag ?? "—"}
        </span>
      </div>
      <p className="mb-5 mt-1.5 max-w-[78ch] text-[12.5px] leading-relaxed text-muted">
        Clustering reasons over this project's code graph only. Overlaps across repos are
        not computed — a shared package name is a galaxy edge, not a collision.
      </p>

      <div className="grid gap-4 lg:grid-cols-[320px_1fr] lg:items-start">
        <IncomingQueue rows={queue} loading={isLoading} projectId={projectId} />
        <Clusters clusters={clusters} />
      </div>
    </div>
  );
}

function IncomingQueue({
  rows,
  loading,
  projectId,
}: {
  rows: TriageRow[];
  loading: boolean;
  projectId: string;
}) {
  return (
    <section className="overflow-hidden rounded-[13px] border border-line bg-surface-2">
      <div className="flex items-center gap-2.5 border-b border-line px-3.5 py-3">
        <MessageSquare size={15} className="shrink-0 text-accent" />
        <h2 className="flex-1 text-[14px] font-semibold">Incoming</h2>
        <span className="font-mono text-[9.5px] text-faint-2">{rows.length}</span>
      </div>

      {loading ? (
        <div className="px-3.5 py-6 text-center font-mono text-[11px] text-faint-2">loading…</div>
      ) : rows.length === 0 ? (
        <div className="px-3.5 py-7 text-center">
          <div className="text-[13px] font-semibold text-fg-2">Queue is empty</div>
          <p className="mx-auto mt-1.5 max-w-[34ch] text-[11.5px] leading-relaxed text-muted">
            Everything reported has been triaged. Not the same as nothing having been
            reported — history lives in the tracker.
          </p>
        </div>
      ) : (
        rows.map((row) => <QueueRow key={row.request.id} row={row} projectId={projectId} />)
      )}
    </section>
  );
}

function QueueRow({ row, projectId }: { row: TriageRow; projectId: string }) {
  const accept = useAcceptRequest(projectId);
  const vote = useVoteRequest();
  const { request: req, duplicate } = row;
  const meta = TYPE_META[req.type as RequestType];

  return (
    <div className="border-b border-line px-3.5 py-3 hover:bg-surface-3">
      <div className="flex items-start gap-2.5">
        <span
          className="mt-0.5 shrink-0 rounded border px-1.5 py-px font-mono text-[8.5px] uppercase tracking-[0.04em]"
          style={{ color: meta?.color, borderColor: `${meta?.color}44` }}
        >
          {req.type}
        </span>
        <span className="min-w-0 flex-1 text-[12.5px] leading-snug text-fg-2">{req.title}</span>
      </div>

      <div className="mt-2 flex items-center gap-2.5 font-mono text-[9.5px] text-faint-2">
        <span>{req.by || "anonymous"}</span>
        <span className="text-line-3">·</span>
        <button
          onClick={() => vote.mutate({ id: req.id, delta: 1 })}
          className="inline-flex items-center gap-1 text-muted hover:text-accent"
        >
          <ArrowUp size={9} />
          {req.votes}
        </button>
        <div className="flex-1" />
        <button
          onClick={() => accept.mutate(req.id)}
          disabled={accept.isPending}
          className="h-5 rounded-[5px] border border-accent/30 px-2 font-mono text-[8.5px] uppercase tracking-[0.04em] text-accent hover:bg-accent/10 disabled:opacity-50"
        >
          {accept.isPending ? "…" : "Accept"}
        </button>
      </div>

      {duplicate && (
        <div className="mt-2 flex items-center gap-2 rounded-md border border-line bg-surface px-2 py-1.5">
          <Copy size={11} className="shrink-0 text-st-next" />
          <span className="min-w-0 flex-1 text-[11px] text-muted">
            looks like <span className="font-mono text-st-next">{duplicate.id}</span> —{" "}
            {Math.round(duplicate.score * 100)}% similar
          </span>
        </div>
      )}
    </div>
  );
}

type Cluster = NonNullable<ReturnType<typeof useFleet>["data"]>["clusters"][number];

/**
 * In-flight work grouped by the code it touches.
 *
 * A cluster of one is not a collision, so only real overlaps are drawn — but "no
 * overlaps" has two very different causes, and the empty state names which one it is
 * rather than showing a calm green tick over an idle project.
 */
function Clusters({ clusters }: { clusters: Cluster[] }) {
  const overlapping = clusters.filter((c) => c.items.length > 1);
  const anyInFlight = clusters.length > 0;

  return (
    <div className="flex flex-col gap-3">
      <div className="flex items-center gap-2.5">
        <Radar size={15} className={overlapping.length ? "text-st-review" : "text-st-done"} />
        <h2 className="text-[14px] font-semibold">Collision clusters</h2>
        <span className="font-mono text-[9.5px] uppercase tracking-[0.05em] text-faint-2">
          {overlapping.length
            ? `${overlapping.length} overlapping`
            : `${clusters.length} in flight`}
        </span>
      </div>

      {overlapping.length === 0 ? (
        <div className="flex gap-3 rounded-[13px] border border-st-done/25 bg-st-done/[0.05] px-4 py-5">
          <span className="flex h-8 w-8 shrink-0 items-center justify-center rounded-[10px] border border-st-done/30 bg-st-done/[0.12]">
            {anyInFlight ? (
              <Check size={16} className="text-st-done" />
            ) : (
              <Inbox size={16} className="text-st-done" />
            )}
          </span>
          <div className="min-w-0 flex-1">
            <div className="text-[14px] font-semibold text-st-done">
              {anyInFlight ? "No overlaps in flight" : "Nothing in flight"}
            </div>
            <div className="mt-1 text-[12px] leading-relaxed text-st-done/70">
              {anyInFlight
                ? `${clusters.length} claim${clusters.length === 1 ? "" : "s"} open and no two touch the same code, so they can run in parallel.`
                : "Nothing is claimed, so there is nothing to collide. This is an idle project, not a cleared one — the check found no work rather than no conflict."}
            </div>
          </div>
        </div>
      ) : (
        overlapping.map((c, i) => <ClusterCard key={i} cluster={c} />)
      )}
    </div>
  );
}

function ClusterCard({ cluster }: { cluster: Cluster }) {
  const risk = cluster.blocked_on ? "blocked" : "serialize";
  return (
    <section
      className={`overflow-hidden rounded-[13px] border bg-surface-2 ${
        risk === "blocked" ? "border-st-blocked/30" : "border-st-review/30"
      }`}
    >
      <div
        className={`flex flex-wrap items-center gap-2.5 px-4 py-3 ${
          risk === "blocked" ? "bg-st-blocked/[0.05]" : "bg-st-review/[0.05]"
        }`}
      >
        <span
          className={`inline-flex items-center gap-1.5 rounded-full border px-2 py-0.5 font-mono text-[9px] uppercase tracking-[0.05em] ${
            risk === "blocked"
              ? "border-st-blocked/30 text-st-blocked"
              : "border-st-review/30 text-st-review"
          }`}
        >
          <span className="h-[5px] w-[5px] rounded-full bg-current" />
          {risk}
        </span>
        <span className="font-mono text-[11.5px] text-fg-2">
          {cluster.items.length} items overlap
        </span>
        {cluster.predicted && (
          <span className="rounded border border-purple/30 px-1.5 py-px font-mono text-[8.5px] uppercase tracking-[0.05em] text-purple">
            predicted
          </span>
        )}
      </div>

      <div className="px-4 py-3">
        <div className="flex flex-wrap gap-1.5">
          {cluster.items.map((id) => (
            <span
              key={id}
              className="rounded-[5px] border border-line px-1.5 py-px font-mono text-[10.5px] text-fg-2"
            >
              {id}
            </span>
          ))}
        </div>

        <div className="mt-2.5 font-mono text-[9px] uppercase tracking-[0.07em] text-faint-2">
          shared paths
        </div>
        <div className="mt-1.5 flex flex-wrap gap-1.5">
          {cluster.areas.map((a) => (
            <span
              key={a}
              className="rounded-[5px] border border-line bg-surface px-1.5 py-px font-mono text-[10px] text-muted"
            >
              {a}
            </span>
          ))}
        </div>

        <p className="mt-3 text-[11.5px] leading-relaxed text-muted">
          {cluster.blocked_on ? (
            <>
              Blocked on <span className="font-mono text-st-blocked">{cluster.blocked_on}</span> —
              the overlap is already held, so a second claim would be refused rather than
              queued.
            </>
          ) : (
            <>
              Serialize — these touch{" "}
              <span className="font-mono text-fg-2">{cluster.areas[0] ?? "the same code"}</span>
              {cluster.areas.length > 1 && ` and ${cluster.areas.length - 1} more`}. Running
              them together is what produces the conflict this check exists to predict.
            </>
          )}
        </p>
      </div>
    </section>
  );
}
