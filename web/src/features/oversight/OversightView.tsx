import { Button } from "@/components/ui/button";
import { useLinkAct, useLinkOversight, useOrgs } from "@/lib/queries";
import { api } from "@/lib/api";
import type { OversightThread } from "@/lib/types";

/** `3h`, `2d`: how long a thing has waited. */
export function ageText(seconds: number): string {
  if (seconds < 3600) return `${Math.max(1, Math.round(seconds / 60))}m`;
  if (seconds < 86_400) return `${Math.round(seconds / 3600)}h`;
  return `${Math.round(seconds / 86_400)}d`;
}

function StaleTag() {
  return (
    <span data-testid="stale-tag" className="rounded border border-amber-500/50 px-1.5 py-px font-mono text-micro uppercase text-amber-500">
      stale
    </span>
  );
}

function ThreadRow({ t }: { t: OversightThread }) {
  const act = useLinkAct();
  const id = t.link.id;
  const ask = (what: string) => window.prompt(what)?.trim() || null;
  const run = (fn: () => Promise<unknown>) => act.mutate(fn);
  return (
    <li data-testid="oversight-row" data-stale={t.stale} className="rounded border border-line bg-surface p-3">
      <div className="flex flex-wrap items-center gap-2 text-sm">
        <span className="font-mono">{t.link.from.id}</span>
        <span className="text-muted">→</span>
        <span className="font-mono">{t.link.to.id}</span>
        <span className="text-muted">{t.projects.from} → {t.projects.to}</span>
        <span className="rounded border border-line px-1.5 py-px font-mono text-micro uppercase">{t.status}</span>
        {t.latest_kind && <span className="text-muted">latest: {t.latest_kind}</span>}
        <span className="text-muted">{ageText(t.age_seconds)}</span>
        {t.stale && <StaleTag />}
      </div>
      {t.status === "pending" && (
        <div className="mt-2 flex items-center gap-2">
          <span className="text-muted">{t.link.reason}</span>
          {t.can_decide && (
            <>
              <Button size="sm" onClick={() => run(() => api.acceptPrdLink(id))}>Accept</Button>
              <Button size="sm" variant="outline" onClick={() => {
                const reason = ask("Why decline this link?");
                if (reason) run(() => api.declinePrdLink(id, reason));
              }}>Decline</Button>
            </>
          )}
        </div>
      )}
      {t.questions.map((q) => (
        <div key={q.message} className="mt-2 flex items-center gap-2" data-testid="oversight-question">
          <span>question: {q.body}</span>
          {q.stale && <StaleTag />}
          <Button size="sm" variant="outline" onClick={() => {
            const body = ask("Your answer");
            if (body) run(() => api.answerLinkQuestion(id, q.message, body));
          }}>Answer</Button>
        </div>
      ))}
      {t.suggestions.map((s) => (
        <div key={s.message} className="mt-2 flex items-center gap-2" data-testid="oversight-suggestion">
          <span>suggested: {s.title}</span>
          <span className="text-muted">
            {s.outcome}{s.code && s.outcome !== "undecided" ? ` (${s.code})` : ""}{s.item ? ` → ${s.item}` : ""}
          </span>
          {s.stale && <StaleTag />}
          {s.outcome === "undecided" && (
            <>
              <Button size="sm" onClick={() => run(() => api.fileLinkSuggestion(id, s.message))}>File</Button>
              <Button size="sm" variant="outline" onClick={() => {
                const reason = ask("Why decline this suggestion?");
                if (reason) run(() => api.declineLinkSuggestion(id, s.message, reason));
              }}>Decline</Button>
            </>
          )}
        </div>
      ))}
      {act.error && <p role="alert" className="mt-2 text-red-500">{(act.error as Error).message}</p>}
    </li>
  );
}

/** Org-level oversight of PRD links and their threads (GRPH-P104 S5): every pending request and
 *  open pair thread, what is waiting on a person, and what has gone stale. */
export function OversightView() {
  const orgs = useOrgs();
  const orgId = orgs.data?.[0]?.id;
  const q = useLinkOversight(orgId);
  if (orgs.isLoading || q.isLoading) return <p className="p-4 text-muted">Loading…</p>;
  if (!orgId) return <p className="p-4 text-muted">Link oversight needs an organization.</p>;
  if (q.error) return <p role="alert" className="p-4 text-red-500">Could not load link oversight.</p>;
  const threads = q.data?.threads ?? [];
  return (
    <div className="p-4" data-testid="oversight-view">
      <h1 className="text-lg font-semibold">Link oversight</h1>
      <p className="text-muted">
        Undecided link requests, questions and suggestions are flagged after {q.data?.stale_after_hours}h.
      </p>
      {threads.length === 0 ? (
        <p className="mt-4 text-muted">No pending link requests or open threads.</p>
      ) : (
        <ul className="mt-4 space-y-2">
          {threads.map((t) => <ThreadRow key={t.link.id} t={t} />)}
        </ul>
      )}
    </div>
  );
}
