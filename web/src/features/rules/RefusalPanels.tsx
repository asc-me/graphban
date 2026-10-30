/**
 * Rules side sheets (GRPH-1164, frames 5a–5e): Unmapped refusals and Recommended rules.
 *
 * Unmapped groups are aggregated by the server (GRPH-1195) from `refusal_unmapped` events by route
 * template or MCP tool plus status, over the last 7 days — the browser only draws them. The "N of M
 * refusal sites name their rule" coverage line is the number the build of this package published,
 * served by the API — the browser never computes it, and a build that did not publish says so, never zero.
 */
import * as React from "react";

import { Button } from "@/components/ui/button";
import { absTime } from "@/features/activity/model";
import { useProjectCtx } from "@/features/ProjectContext";
import { cn } from "@/lib/cn";
import { errorDetail } from "@/lib/errors";
import { useCreateItem, useDismissRecommendedRule, useRecommendedRules, useRefusalCoverage, useRulesFlow, useUnmappedRefusals } from "@/lib/queries";
import type { RuleSuggestion, SuggestionSource, UnmappedGroup } from "@/lib/types";

const MONO = "font-mono";
const WINDOW_DAYS = 7;

function groupKey(g: UnmappedGroup) {
  return `${g.platform ? "platform|" : ""}${g.where}|${g.status ?? ""}`;
}

const TABBABLE = 'a[href],button:not([disabled]),input:not([disabled]),select:not([disabled]),textarea:not([disabled]),[tabindex]:not([tabindex="-1"])';

/**
 * What a real dialog does (GRPH-1202): focus moves in on open unless something inside already
 * has it, Escape closes it, Tab can be kept inside, and focus returns to the opener on close.
 * The opener is read in render — before an unmounting sibling dialog can move focus — and an
 * explicit `returnTo` wins because Safari does not focus a clicked button.
 */
export function useDialogBehavior<T extends HTMLElement>(
  onClose: () => void,
  { trap = false, returnTo }: { trap?: boolean; returnTo?: React.RefObject<HTMLElement | null> } = {},
) {
  const ref = React.useRef<T | null>(null);
  const opener = React.useRef<Element | null>(typeof document === "undefined" ? null : document.activeElement);
  const closeRef = React.useRef(onClose);
  closeRef.current = onClose;
  React.useEffect(() => {
    const el = ref.current;
    if (el && !el.contains(document.activeElement)) el.focus({ preventScroll: true });
    return () => {
      const target = returnTo?.current ?? opener.current;
      const active = document.activeElement;
      if (!(target instanceof HTMLElement) || !target.isConnected) return;
      if (active && active !== document.body && !(el && el.contains(active))) return;
      target.focus({ preventScroll: true });
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);
  const onKeyDown = (e: React.KeyboardEvent) => {
    if (e.key === "Escape") {
      e.preventDefault();
      e.stopPropagation();
      closeRef.current();
      return;
    }
    if (e.key !== "Tab" || !trap || !ref.current) return;
    const items = Array.from(ref.current.querySelectorAll<HTMLElement>(TABBABLE));
    if (items.length === 0) {
      e.preventDefault();
      return;
    }
    const first = items[0];
    const last = items[items.length - 1];
    const active = document.activeElement;
    if (e.shiftKey && (active === first || active === ref.current)) {
      e.preventDefault();
      last.focus();
    } else if (!e.shiftKey && active === last) {
      e.preventDefault();
      first.focus();
    }
  };
  return { ref, onKeyDown };
}

function Sheet({
  label,
  kicker,
  title,
  onClose,
  children,
  footer,
  testId,
  returnTo,
}: {
  returnTo?: React.RefObject<HTMLElement | null>;
  label: string;
  kicker: string;
  title: string;
  onClose: () => void;
  children: React.ReactNode;
  footer?: React.ReactNode;
  testId: string;
}) {
  const dialog = useDialogBehavior<HTMLElement>(onClose, { trap: true, returnTo });
  return (
    <aside
      ref={dialog.ref}
      tabIndex={-1}
      onKeyDown={dialog.onKeyDown}
      role="dialog"
      aria-modal="true"
      aria-label={label}
      data-testid={testId}
      className="absolute inset-y-4 right-4 z-20 flex w-[440px] max-w-[calc(100%-2rem)] flex-col overflow-hidden rounded-[16px] border border-line-2 bg-surface-2 shadow-2xl"
    >
      <div className="flex gap-3 border-b border-line px-4 py-3.5">
        <div className="flex min-w-0 flex-1 flex-col gap-1">
          <span className={cn(MONO, "text-meta tracking-[0.07em] text-muted-2")}>{kicker}</span>
          <span className="text-[16px] font-semibold leading-tight">{title}</span>
        </div>
        <button
          type="button"
          onClick={onClose}
          aria-label={`Close ${label}`}
          className="h-7 w-7 flex-none rounded-[8px] border border-control text-[15px] leading-none text-muted hover:text-fg"
        >
          ×
        </button>
      </div>
      <div className="flex min-h-0 flex-1 flex-col gap-2 overflow-y-auto p-3">{children}</div>
      {footer && <div className="border-t border-line px-4 py-3 text-[11.5px] leading-snug text-muted-2">{footer}</div>}
    </aside>
  );
}

function Note({ children, testId, tone = "muted" }: { children: React.ReactNode; testId?: string; tone?: "muted" | "red" }) {
  return (
    <div
      data-testid={testId}
      className="rounded-[8px] px-3 py-2.5 text-[12px] leading-snug text-muted"
      style={{ border: `1.5px dashed ${tone === "red" ? "#ff6b6b" : "#626c75"}` }}
    >
      {children}
    </div>
  );
}

function CoverageLine({ projectId }: { projectId: string }) {
  const q = useRefusalCoverage(projectId);
  const c = q.data;
  if (c?.published && c.n != null && c.m != null) {
    const pct = c.m > 0 ? Math.round((c.n / c.m) * 100) : 0;
    return (
      <Note testId="rules-unmapped-coverage">
        <div className="mb-1.5 text-fg">
          <span className={MONO} data-testid="rules-coverage-n">{c.n}</span> of <span className={MONO} data-testid="rules-coverage-m">{c.m}</span>{" "}
          refusal sites name their rule
        </div>
        <div className="h-1.5 w-full rounded-full" style={{ background: "#2a333a" }} data-testid="rules-coverage-bar">
          <div className="h-full rounded-full" style={{ width: `${pct}%`, background: "#c6f24e" }} />
        </div>
        {c.commit && <div className={cn(MONO, "mt-1.5 text-xs")}>commit {c.commit.slice(0, 7)}</div>}
      </Note>
    );
  }
  return (
    <Note testId="rules-unmapped-coverage">
      {q.isError || q.isLoading ? "Coverage unavailable" : "Not published by this build"}: the “N of M refusal sites name their rule”
      number is measured when a package is built. That is not zero.
    </Note>
  );
}

function Spark({ days }: { days: number[] }) {
  const max = Math.max(1, ...days);
  return (
    <svg width={42} height={16} role="img" aria-label={`Last ${WINDOW_DAYS} days: ${days.join(", ")}`} className="flex-none">
      {days.map((n, i) => (
        <rect key={i} x={i * 6} y={16 - Math.max(n ? 2 : 1, (n / max) * 16)} width={4} height={Math.max(n ? 2 : 1, (n / max) * 16)} fill={n ? "#c6f24e" : "#2a333a"} />
      ))}
    </svg>
  );
}

// ── Unmapped refusals (5a–5c) ──────────────────────────────────────────────────

export function UnmappedSheet({ onClose, returnTo }: { onClose: () => void; returnTo?: React.RefObject<HTMLElement | null> }) {
  const { activeId } = useProjectCtx();
  const q = useUnmappedRefusals(activeId);
  const createItem = useCreateItem(activeId);
  const [filed, setFiled] = React.useState<Record<string, string>>({});
  const [copied, setCopied] = React.useState<string | null>(null);
  const groups = q.data?.groups ?? [];
  const since = q.data?.recording_since;

  function fileAsItem(g: UnmappedGroup) {
    createItem.mutate(
      {
        title: `Name the rule for the ${g.status ?? ""} refusal at ${g.where}`.replace("  ", " "),
        description: `Refused ${g.count} time(s) with no rule_id: ${g.where}${g.status ? ` (${g.status})` : ""}.\n\n## Acceptance\n- The refusal is raised as Refused(..., rule_id=<registered rule>).`,
      },
      { onSuccess: (item) => setFiled((f) => ({ ...f, [groupKey(g)]: item.id })) },
    );
  }

  function copy(g: UnmappedGroup) {
    void navigator.clipboard?.writeText(g.where);
    setCopied(groupKey(g));
  }

  return (
    <Sheet
      returnTo={returnTo}
      label="Unmapped refusals"
      kicker={`REFUSAL_UNMAPPED · LAST ${WINDOW_DAYS} DAYS`}
      title="Unmapped refusals"
      onClose={onClose}
      testId="rules-unmapped"
      footer={<span data-testid="rules-unmapped-footer">Mapping a refusal is a code change: raise it as Refused(..., rule_id=…) with a registered rule. Nothing here maps one for you.</span>}
    >
      <CoverageLine projectId={activeId} />
      {q.isError ? (
        <Note tone="red" testId="rules-unmapped-error">
          <div>
            Could not read unmapped refusals: {errorDetail(q.error, "request failed")}. The count is unknown, not zero.
          </div>
          <div className={cn(MONO, "mt-1 break-all text-meta")} data-testid="rules-unmapped-request">
            GET /api/rules/refusals/unmapped?project_id={activeId}&days={WINDOW_DAYS}
          </div>
          <Button size="sm" variant="outline" className="mt-2" onClick={() => void q.refetch()}>
            Retry
          </Button>
        </Note>
      ) : q.isLoading ? (
        <Note>Loading unmapped refusals…</Note>
      ) : groups.length === 0 ? (
        <Note testId="rules-unmapped-empty">
          {since
            ? `No unmapped refusals recorded since ${absTime(since)}. Nothing in the last ${WINDOW_DAYS} days was refused without naming a rule.`
            : "No refusals of any kind have been recorded yet, so this is not a clean result — nothing has been checked."}
        </Note>
      ) : (
        <>
          <ul className="flex flex-col gap-2" data-testid="rules-unmapped-list">
            {groups.map((g, i) => (
              <li key={groupKey(g)} className="flex flex-col gap-2 rounded-[12px] border border-[#2a333a] p-3" data-testid="rules-unmapped-row">
                <div className="flex items-start gap-2">
                  <span className={cn(MONO, "w-5 flex-none text-[12px] text-muted-2")} data-testid="rules-unmapped-rank">#{i + 1}</span>
                  <span className={cn(MONO, "min-w-0 flex-1 break-all text-[12px] text-fg")}>{g.where}</span>
                  {g.platform && <span className={cn(MONO, "text-[10px] tracking-[0.05em] text-muted-2")}>PLATFORM</span>}
                  {g.status !== null && <span className={cn(MONO, "text-small text-muted")}>{g.status}</span>}
                </div>
                <div className="flex items-center gap-3">
                  <span className={cn(MONO, "text-[12px] font-semibold")}>{g.count}×</span>
                  <Spark days={g.daily} />
                  {g.last_at && <span className="text-small text-muted" data-testid="rules-unmapped-last">last {absTime(g.last_at)}</span>}
                  <span className="flex-1" />
                  <Button size="sm" variant="outline" onClick={() => copy(g)}>
                    {copied === groupKey(g) ? "Copied" : "Copy route"}
                  </Button>
                  {filed[groupKey(g)] ? (
                    <span className={cn(MONO, "text-small text-accent")}>Filed {filed[groupKey(g)]}</span>
                  ) : (
                    <Button size="sm" variant="outline" disabled={createItem.isPending} onClick={() => fileAsItem(g)}>
                      File as item
                    </Button>
                  )}
                </div>
              </li>
            ))}
          </ul>
          {createItem.isError && <Note tone="red">{errorDetail(createItem.error, "Could not file the item.")}</Note>}
        </>
      )}
    </Sheet>
  );
}

// ── Recommended rules (5d–5e) ──────────────────────────────────────────────────

const SOURCE_NAMES: Record<string, string> = { stack: "Stack", code_graph: "Code graph", github: "GitHub" };

function SourceLine({ s }: { s: SuggestionSource }) {
  const name = SOURCE_NAMES[s.source] ?? s.source;
  const read = s.state === "read";
  return (
    <li className="flex flex-wrap items-center gap-2 text-[12px]" data-testid={`rules-source-${s.source}`}>
      <span className={cn(MONO, "w-[88px] flex-none text-fg-2")}>{name}</span>
      <span className={cn(MONO, "text-[10px] font-semibold tracking-[0.05em]")} style={{ color: read ? "#c6f24e" : "#ff6b6b" }}>
        {read ? "READ" : s.source === "github" ? "NOT CONNECTED" : "NOT AVAILABLE"}
      </span>
      <span className="text-muted">{read ? (s.read_at ? absTime(s.read_at) : "") : s.reason ?? ""}</span>
      {!read && s.source === "github" && (
        <a href="/settings" className="text-accent hover:underline">Connect GitHub</a>
      )}
    </li>
  );
}

export function RecommendedSheet({
  onClose,
  onOpenValue,
  returnTo,
}: {
  onClose: () => void;
  returnTo?: React.RefObject<HTMLElement | null>;
  /** Opens the stage with the editor prefilled — for a value suggestion. */
  onOpenValue: (s: RuleSuggestion) => void;
}) {
  const { activeId } = useProjectCtx();
  const q = useRecommendedRules(activeId, true);
  const dismiss = useDismissRecommendedRule(activeId);
  const createItem = useCreateItem(activeId);
  const { data: flow } = useRulesFlow(activeId);
  // The id is a key, never copy: the sheet says the stage's title.
  const stageTitle = (id: string) => flow?.stages.find((st) => st.id === id)?.title ?? "the flow";
  const [filed, setFiled] = React.useState<Record<string, string>>({});
  const github = q.data?.sources.find((s) => s.source === "github");

  function fileAsItem(s: RuleSuggestion) {
    createItem.mutate(
      { title: `Rule: ${s.title}`, description: `${s.why}\n\n## Acceptance\n- ${s.title} is enforced by the server.` },
      { onSuccess: (item) => setFiled((f) => ({ ...f, [s.id]: item.id })) },
    );
  }

  return (
    <Sheet returnTo={returnTo} label="Recommended rules" kicker="SUGGESTED · FROM THE SOURCES BELOW" title="Recommended rules" onClose={onClose} testId="rules-recommended">
      {q.isError ? (
        <Note tone="red" testId="rules-recommended-error">
          Could not read suggestions: {errorDetail(q.error, "request failed")}. That is not “nothing to suggest”.
        </Note>
      ) : !q.data ? (
        <Note>Reading the sources…</Note>
      ) : (
        <>
          <ul className="flex flex-col gap-1.5 rounded-[8px] border border-[#2a333a] p-3" data-testid="rules-sources">
            {q.data.sources.map((s) => (
              <SourceLine key={s.source} s={s} />
            ))}
          </ul>
          {github && github.state !== "read" && (
            <Note testId="rules-github-absent">
              GitHub suggestions are absent: {github.reason || "GitHub is not connected"}. Connect it to see rules its history implies.
            </Note>
          )}
          {q.data.suggestions.length === 0 ? (
            <Note testId="rules-recommended-empty">
              No suggestions from the sources that were read. Sources that could not be read are listed above.
            </Note>
          ) : (
            q.data.suggestions.map((s) => (
              <div key={s.id} className="flex flex-col gap-2 rounded-[12px] border border-[#2a333a] p-3" data-testid={`rules-suggestion-${s.id}`}>
                <div className="flex items-center gap-2">
                  <span
                    className={cn(MONO, "rounded-[4px] px-1.5 text-[10px] font-semibold tracking-[0.05em] text-[#ffbf47]")}
                    style={{ border: "1px dashed #ffbf47" }}
                    data-testid="rules-suggestion-chip"
                  >
                    {s.kind === "code" ? `${s.level.toUpperCase()} ONCE BUILT` : `WOULD BE ${s.level.toUpperCase()}`}
                  </span>
                  <span className={cn(MONO, "text-[10px] text-muted-2")}>{stageTitle(s.stage).toUpperCase()}</span>
                </div>
                <span className="text-[13px] font-semibold leading-snug">{s.title}</span>
                <span className="text-[12px] leading-snug text-muted">{s.why}</span>
                {s.kind === "code" && (
                  <span className="text-[11.5px] leading-snug text-fg-2">Needs a code change. Until it ships, nothing holds it.</span>
                )}
                <ul className="flex flex-col gap-0.5">
                  {s.evidence.map((e, i) => (
                    <li key={i} className="text-[11.5px] text-muted-2">
                      {e.link ? (
                        <a href={e.link} target="_blank" rel="noopener noreferrer" className="text-accent hover:underline">{e.text}</a>
                      ) : (
                        e.text
                      )}
                      <span className={cn(MONO, "ml-1.5 text-[10px]")}>{e.source}</span>
                    </li>
                  ))}
                </ul>
                <div className="flex items-center gap-2">
                  {s.kind === "code" ? (
                    filed[s.id] ? (
                      <span className={cn(MONO, "text-small text-accent")}>Filed {filed[s.id]}</span>
                    ) : (
                      <Button size="sm" variant="outline" disabled={createItem.isPending} onClick={() => fileAsItem(s)}>
                        File as item
                      </Button>
                    )
                  ) : (
                    <Button size="sm" variant="outline" onClick={() => onOpenValue(s)}>
                      Open in {stageTitle(s.stage)}
                    </Button>
                  )}
                  <Button size="sm" variant="ghost" disabled={dismiss.isPending} onClick={() => dismiss.mutate(s.id)}>
                    Dismiss
                  </Button>
                </div>
              </div>
            ))
          )}
          {(dismiss.isError || createItem.isError) && (
            <Note tone="red">{errorDetail(dismiss.error ?? createItem.error, "That did not save.")}</Note>
          )}
        </>
      )}
    </Sheet>
  );
}
