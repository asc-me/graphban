import { Check, Layers, RotateCcw, Sparkles, X } from "lucide-react";
import * as React from "react";

import { cn } from "@/lib/cn";
import { useProjectCtx } from "@/features/ProjectContext";
import {
  useAutoActions,
  useCandidateClusters,
  useCandidateShards,
  useJudgeShard,
  usePromoteCluster,
  useReviewShard,
  useScoredCandidates,
  useUndoAutoShard,
} from "@/lib/queries";
import type { CandidateJudge, ReviewSuggestion, ScoredCandidate, Shard, ShardCluster } from "@/lib/types";

/** AL-49: the review queue. Agent-written memory enters as a candidate and only
 *  reaches the trusted retrieval path once a human publishes it here.
 *  AL-50: recurring near-duplicate candidates are grouped so a lesson that keeps
 *  recurring can be promoted once as a principle. */
export function MemoryReviewView() {
  const { activeId, active } = useProjectCtx();
  const judgeOn = Boolean(active?.memory_llm_judge);
  const { data: candidates, isLoading } = useCandidateShards(activeId);
  const { data: clusters } = useCandidateClusters(activeId);
  const { data: scored } = useScoredCandidates(activeId);
  const { data: autoActions } = useAutoActions(activeId);
  const { publish, reject } = useReviewShard();
  const promoteCluster = usePromoteCluster();
  const undoAuto = useUndoAutoShard();
  const [unvettedOnly, setUnvettedOnly] = React.useState(false);

  if (isLoading || !candidates) {
    return <div className="flex h-full items-center justify-center text-[13px] text-muted">Loading…</div>;
  }

  const clustered = new Set((clusters ?? []).flatMap((c) => [c.representative.id, ...c.members.map((m) => m.id)]));
  const scoreById = new Map((scored ?? []).map((s) => [s.shard.id, s]));
  // Published with nobody looking (AL-280 trusted / AL-282 agent). With no candidates
  // queued this is the ONLY work left, so it drives the empty state too.
  const unvettedTotal = (autoActions ?? []).filter((s) =>
    UNVETTED_SOURCES.includes(s.scoring_source),
  ).length;
  // Loose candidates ordered most-actionable first (highest suggestion confidence).
  const loose = candidates
    .filter((s) => !clustered.has(s.id))
    .sort((a, b) => (scoreById.get(b.id)?.confidence ?? 0) - (scoreById.get(a.id)?.confidence ?? 0));

  return (
    <div className="flex h-full min-h-0 flex-col">
      <div className="flex flex-none items-center gap-4 border-b border-line px-5 py-4">
        <div>
          <h1 className="text-[18px] font-semibold tracking-tight">Memory review</h1>
          <p className="mt-0.5 text-[12.5px] text-muted">
            Agent-written memory is a candidate until you publish it. Only published shards surface in
            search — so an unverified note never becomes ground truth for the next agent.
          </p>
        </div>
        <div className="ml-auto flex items-center gap-3 font-mono text-[10.5px] text-faint">
          <span>{candidates.length} PENDING</span>
          {unvettedTotal > 0 && (
            <span className="text-[#a78bfa]">{unvettedTotal} UNREVIEWED</span>
          )}
        </div>
      </div>

      <div className="min-h-0 flex-1 overflow-y-auto p-5">
        <div className="mx-auto flex max-w-3xl flex-col gap-2.5">
          {candidates.length === 0 ? (
            <div className="py-16 text-center text-[13px] text-muted">
              {unvettedTotal > 0 ? (
                <>
                  No candidates waiting — but {unvettedTotal} shard{unvettedTotal === 1 ? " was" : "s were"}{" "}
                  published without a human. Reviewing those is the job below.
                </>
              ) : (
                <>Nothing to review. Agent-proposed lessons and notes will queue here for your approval.</>
              )}
            </div>
          ) : (
            <>
              {(clusters ?? []).map((c) => (
                <ClusterCard
                  key={c.representative.id}
                  cluster={c}
                  onPromote={() =>
                    promoteCluster.mutate({
                      publishId: c.representative.id,
                      rejectIds: c.members.map((m) => m.id),
                    })
                  }
                  busy={promoteCluster.isPending}
                />
              ))}
              {loose.map((s) => (
                <CandidateCard
                  key={s.id}
                  shard={s}
                  score={scoreById.get(s.id)}
                  judgeOn={judgeOn}
                  onPublish={() => publish.mutate(s.id)}
                  onReject={() => reject.mutate(s.id)}
                  busy={publish.isPending || reject.isPending}
                />
              ))}
            </>
          )}
          {autoActions && autoActions.length > 0 && (
            <AutoActionsLane
              shards={autoActions}
              onUndo={(id) => undoAuto.mutate(id)}
              busy={undoAuto.isPending}
              unvettedOnly={unvettedOnly}
              onToggleUnvetted={() => setUnvettedOnly((v) => !v)}
            />
          )}
        </div>
      </div>
    </div>
  );
}

/** Which of these did a person actually look at? (AL-287)
 *
 *  `similarity` / `llm` are the AL-227 scorer acting on its own thresholds. `trusted`
 *  (AL-280) published on write with nothing assessed at all, and `agent` (AL-282) means
 *  an agent submitted it and the judge ruled — no human either way.
 *
 *  Once agents run the loop, reviewing what they decided IS the human's job, so the two
 *  unvetted sources get their own label and a one-click filter. These are also the
 *  shards excluded from the corroboration pool, so this list is exactly the set worth
 *  sweeping. */
export const UNVETTED_SOURCES = ["trusted", "agent"];

const SOURCE_LABEL: Record<string, string> = {
  trusted: "no review",
  agent: "agent + judge",
  llm: "llm scorer",
  similarity: "similarity",
};

function AutoActionsLane({
  shards,
  onUndo,
  busy,
  unvettedOnly,
  onToggleUnvetted,
}: {
  shards: Shard[];
  onUndo: (id: string) => void;
  busy: boolean;
  unvettedOnly: boolean;
  onToggleUnvetted: () => void;
}) {
  const unvettedCount = shards.filter((s) => UNVETTED_SOURCES.includes(s.scoring_source)).length;
  const shown = unvettedOnly
    ? shards.filter((s) => UNVETTED_SOURCES.includes(s.scoring_source))
    : shards;
  return (
    <div className="mt-4">
      <div className="mb-2 flex items-center gap-2 px-0.5">
        <Sparkles size={12} className="text-[#a78bfa]" />
        <span className="font-mono text-[10.5px] uppercase tracking-wide text-faint">
          Recent auto-actions · {shown.length}
        </span>
        {unvettedCount > 0 && (
          <button
            onClick={onToggleUnvetted}
            aria-pressed={unvettedOnly}
            className={cn(
              "rounded-md border px-1.5 py-0.5 font-mono text-[9.5px] uppercase tracking-wide transition-colors",
              unvettedOnly
                ? "border-[#a78bfa] bg-[rgba(167,139,250,0.12)] text-[#a78bfa]"
                : "border-line-2 text-faint hover:border-line-hover hover:text-muted",
            )}
          >
            {unvettedOnly ? "showing" : "show"} {unvettedCount} nobody reviewed
          </button>
        )}
      </div>
      <div className="flex flex-col gap-1.5">
        {shown.map((s) => {
          const rejected = s.status === "rejected";
          const unvetted = UNVETTED_SOURCES.includes(s.scoring_source);
          return (
            <div
              key={s.id}
              className="flex items-start gap-2.5 rounded-[10px] border border-line-2 bg-surface-2/60 px-3 py-2"
            >
              <span
                className={cn(
                  "mt-0.5 shrink-0 rounded border px-1.5 py-0.5 font-mono text-[9px] uppercase tracking-wide",
                  rejected
                    ? "border-[rgba(255,107,107,0.3)] bg-[rgba(255,107,107,0.08)] text-st-blocked"
                    : "border-[#1c2620] bg-[rgba(95,208,122,0.1)] text-st-done",
                )}
              >
                auto-{rejected ? "rejected" : "published"}
              </span>
              {s.scoring_source && (
                <span
                  title={
                    unvetted
                      ? "Published without a human looking at it. Excluded from the corroboration pool until you review it."
                      : "Decided by the offline scorer on its confidence thresholds."
                  }
                  className={cn(
                    "mt-0.5 shrink-0 rounded border px-1.5 py-0.5 font-mono text-[9px] uppercase tracking-wide",
                    unvetted
                      ? "border-[rgba(167,139,250,0.35)] bg-[rgba(167,139,250,0.1)] text-[#a78bfa]"
                      : "border-line-2 text-faint",
                  )}
                >
                  {SOURCE_LABEL[s.scoring_source] ?? s.scoring_source}
                </span>
              )}
              <p className="min-w-0 flex-1 truncate text-[12.5px] text-fg-2" title={s.text}>
                {s.text}
              </p>
              {s.auto_confidence != null && (
                <span className="mt-0.5 shrink-0 font-mono text-[10px] text-faint">
                  {Math.round(s.auto_confidence * 100)}%
                </span>
              )}
              <button
                onClick={() => onUndo(s.id)}
                disabled={busy}
                title="Return to the review queue"
                className="inline-flex shrink-0 items-center gap-1 rounded-md border border-line px-2 py-1 text-[11px] text-muted transition-colors hover:border-line-hover hover:text-ink disabled:opacity-50"
              >
                <RotateCcw size={11} /> Undo
              </button>
            </div>
          );
        })}
      </div>
    </div>
  );
}

// AL-151: advisory review suggestion — colour + label per suggestion category.
const SUGGESTION_META: Record<ReviewSuggestion, { label: string; className: string }> = {
  accept: { label: "suggest publish", className: "border-[#1c2620] bg-[rgba(95,208,122,0.1)] text-st-done" },
  reject: { label: "suggest reject", className: "border-[rgba(255,107,107,0.3)] bg-[rgba(255,107,107,0.08)] text-st-blocked" },
  review: { label: "needs a look", className: "border-line-2 bg-surface-3 text-muted" },
};

function CandidateCard({
  shard,
  score,
  judgeOn,
  onPublish,
  onReject,
  busy,
}: {
  shard: Shard;
  score?: ScoredCandidate;
  judgeOn: boolean;
  onPublish: () => void;
  onReject: () => void;
  busy: boolean;
}) {
  const meta = score ? SUGGESTION_META[score.suggestion] : null;
  const judge = useJudgeShard();
  const [asked, setAsked] = React.useState<CandidateJudge | null>(null);
  return (
    <div className="rounded-[12px] border border-line-2 bg-surface-2 p-3.5">
      <div className="mb-2 flex items-center gap-2">
        <Sparkles size={13} className="text-[#a78bfa]" />
        <span className="font-mono text-[10.5px] text-faint">{shard.origin || "agent"}</span>
        {shard.source && <span className="font-mono text-[10.5px] text-faint">· {shard.source}</span>}
        {meta && (
          <span
            title={score ? `${Math.round(score.confidence * 100)}% confidence` : undefined}
            className={cn(
              "ml-auto rounded border px-1.5 py-0.5 font-mono text-[9px] uppercase tracking-wide",
              meta.className,
            )}
          >
            {meta.label}
          </span>
        )}
        <span
          className={cn(
            "rounded border border-[#3a2f1a] bg-[rgba(224,179,74,0.08)] px-1.5 py-0.5 font-mono text-[9px] uppercase tracking-wide text-[#e0b34a]",
            !meta && "ml-auto",
          )}
        >
          candidate
        </span>
      </div>
      <p className="mb-2 whitespace-pre-wrap text-[13px] leading-relaxed text-ink">{shard.text}</p>
      {score && score.reasons.length > 0 && (
        <p className="mb-3 text-[11.5px] text-faint">Why: {score.reasons.join(" · ")}</p>
      )}
      {asked?.verdict && (
        <p className="mb-3 text-[11.5px] text-fg-2">
          Judge: {Math.round(asked.verdict.quality * 100)}% — {asked.verdict.reason || (asked.verdict.keep ? "publish-worthy" : "not publish-worthy")}
        </p>
      )}
      {asked && asked.verdict == null && (
        <p className="mb-3 text-[11.5px] text-faint">Judge unavailable: {asked.cause_detail}</p>
      )}
      <div className="flex items-center gap-2">
        <button
          onClick={onPublish}
          disabled={busy}
          className="inline-flex items-center gap-1.5 rounded-lg border border-[#1c2620] bg-[rgba(95,208,122,0.08)] px-2.5 py-1.5 text-[12px] font-medium text-st-done transition-colors hover:bg-[rgba(95,208,122,0.14)] disabled:opacity-50"
        >
          <Check size={13} /> Publish
        </button>
        <button
          onClick={onReject}
          disabled={busy}
          className="inline-flex items-center gap-1.5 rounded-lg border border-line px-2.5 py-1.5 text-[12px] text-muted transition-colors hover:border-line-hover hover:text-ink disabled:opacity-50"
        >
          <X size={13} /> Reject
        </button>
        {judgeOn && (
          <button
            onClick={() => judge.mutate(shard.id, { onSuccess: setAsked })}
            disabled={busy || judge.isPending}
            className="inline-flex items-center gap-1.5 rounded-lg border border-line px-2.5 py-1.5 text-[12px] text-muted transition-colors hover:border-line-hover hover:text-ink disabled:opacity-50"
          >
            {judge.isPending ? "Asking…" : "Ask the judge"}
          </button>
        )}
      </div>
    </div>
  );
}

/** AL-50: a recurring lesson — several near-duplicate candidates. Promote the
 *  representative as the principle and drop the duplicates in one action. */
function ClusterCard({
  cluster,
  onPromote,
  busy,
}: {
  cluster: ShardCluster;
  onPromote: () => void;
  busy: boolean;
}) {
  const { representative: rep, members, size } = cluster;
  return (
    <div className="rounded-[12px] border border-[rgba(224,179,74,0.35)] bg-[rgba(224,179,74,0.04)] p-3.5">
      <div className="mb-2 flex items-center gap-2">
        <Layers size={13} className="text-[#e0b34a]" />
        <span className="font-mono text-[10.5px] font-medium text-[#e0b34a]">
          RECURRING · appeared {size}×
        </span>
        <span className="font-mono text-[10.5px] text-faint">{rep.origin || "agent"}</span>
      </div>
      <p className="mb-2 whitespace-pre-wrap text-[13px] leading-relaxed text-ink">{rep.text}</p>
      {members.length > 0 && (
        <details className="mb-3 text-[12px] text-muted">
          <summary className="cursor-pointer select-none text-faint">
            {members.length} similar duplicate{members.length > 1 ? "s" : ""}
          </summary>
          <ul className="mt-1.5 flex flex-col gap-1 border-l border-line pl-3">
            {members.map((m) => (
              <li key={m.id} className="whitespace-pre-wrap leading-relaxed">{m.text}</li>
            ))}
          </ul>
        </details>
      )}
      <div className="flex items-center gap-2">
        <button
          onClick={onPromote}
          disabled={busy}
          className="inline-flex items-center gap-1.5 rounded-lg border border-[#1c2620] bg-[rgba(95,208,122,0.08)] px-2.5 py-1.5 text-[12px] font-medium text-st-done transition-colors hover:bg-[rgba(95,208,122,0.14)] disabled:opacity-50"
        >
          <Check size={13} /> Publish as principle{members.length > 0 ? ` · drop ${members.length}` : ""}
        </button>
      </div>
    </div>
  );
}
