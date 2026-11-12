import { QUESTION_LABEL } from "@/features/tracker/QuestionStateBadge";
import { useEffect, useRef, useState, type ReactNode } from "react";
import { Link, useLocation } from "react-router-dom";


import { CommentThread } from "@/features/requests/CommentThread";
import { PALETTE_ITEM_PARAM } from "@/lib/palette-nav";
import { projectPath, tagFromPath } from "@/lib/routes";
import type { TriageAnsweredRow, TriageFeedRow } from "@/lib/types";

import { DrainNote } from "./DrainNote";

import { duplicateHintCopy, formatAge, type TriageAction } from "./actions";

/** `c` in the queue asks the open question's answer box to show itself. */
export const OPEN_ANSWER_EVENT = "triage-open-answer";
const EMPTY_ANSWER = "Type an answer first. An empty answer isn't sent.";
const OPEN_QUESTION =
  "The agent asked an open question, so there are no options. Type the answer it should work from.";

function AnswerOptions({
  options, onAction, onText,
}: {
  options: TriageAction[];
  onAction: (a: TriageAction) => void;
  onText: (text: string) => void;
}) {
  const noOptions = options.length === 0;
  const [open, setOpen] = useState(noOptions);
  const [text, setText] = useState("");
  const [err, setErr] = useState<string | null>(null);
  const ref = useRef<HTMLTextAreaElement>(null);
  const focusNext = useRef(false);
  const show = open || noOptions;
  useEffect(() => {
    const on = () => { focusNext.current = true; setOpen(true); ref.current?.focus(); };
    window.addEventListener(OPEN_ANSWER_EVENT, on);
    return () => window.removeEventListener(OPEN_ANSWER_EVENT, on);
  }, []);
  useEffect(() => {
    if (show && focusNext.current) { focusNext.current = false; ref.current?.focus(); }
  }, [show]);
  const send = () => {
    if (!text.trim()) { setErr(EMPTY_ANSWER); return; }
    setErr(null);
    onText(text.trim());
  };
  const keyed = options.filter((o) => o.key);
  const more = options.filter((o) => !o.key);
  const btn = (a: TriageAction) => (
    <button
      key={a.label}
      onClick={() => onAction(a)}
      className="flex h-8 w-full items-center gap-2 rounded-md border border-control px-3 text-small text-fg-2 hover:bg-surface-3"
    >
      {a.key && <kbd className="rounded bg-surface-3 px-1 font-mono text-micro text-muted">{a.key.toUpperCase()}</kbd>}
      {a.label}
    </button>
  );
  return (
    <div className="mt-4" data-testid="answer-options">
      {keyed.length > 0 && <div className="flex flex-col gap-1.5">{keyed.map(btn)}</div>}
      {more.length > 0 && (
        <>
          <div className="my-3 flex items-center gap-2 font-mono text-micro uppercase tracking-[0.07em] text-faint-2">
            <span className="h-px flex-1 bg-line" />More options<span className="h-px flex-1 bg-line" />
          </div>
          <div className="flex flex-col gap-1.5">{more.map(btn)}</div>
        </>
      )}
      {!noOptions && !show && (
        <button onClick={() => { focusNext.current = true; setOpen(true); }}
          className="mt-3 text-small text-muted underline hover:text-fg">
          Answer in your own words
        </button>
      )}
      {show && (
        <div className="mt-3">
          <label className="font-mono text-micro uppercase tracking-[0.07em] text-faint-2" htmlFor="triage-answer">
            Your answer
          </label>
          <textarea
            id="triage-answer"
            ref={ref}
            data-triage-answer
            value={text}
            rows={3}
            placeholder={noOptions ? OPEN_QUESTION : undefined}
            onChange={(e) => { setText(e.target.value); setErr(null); }}
            onKeyDown={(e) => {
              if (e.key === "Escape") { e.preventDefault(); e.currentTarget.blur(); }
              else if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); send(); }
            }}
            className="mt-1 w-full rounded-md border border-control bg-surface px-2 py-1.5 text-small text-fg"
          />
          {err && <div role="alert" className="mt-1 text-small text-st-blocked">{err}</div>}
          <div className="mt-1.5 flex items-center gap-3">
            <button onClick={send} className="h-7 rounded-md bg-accent px-3 text-small font-semibold text-bg">Send</button>
            {!noOptions && (
              <button onClick={() => { setOpen(false); setErr(null); }}
                className="text-small text-muted underline hover:text-fg">
                Back to options
              </button>
            )}
          </div>
        </div>
      )}
    </div>
  );
}

const STATUS_TEXT: Record<string, string> = {
  in_progress: "text-st-next", review: "text-st-review", blocked: "text-st-blocked", next: "text-purple",
  backlog: "text-muted", done: "text-st-done",
};
const statusText = (s: string) => STATUS_TEXT[s] ?? "text-muted";

const KIND_PILL: Record<string, string> = {
  blocker: "border-st-blocked/40 text-st-blocked",
  collision: "border-st-review/40 text-st-review",
  answered: "border-st-done/40 text-st-done",
  feature: "border-accent/40 text-accent",
};
const kindPill = (k: string) => KIND_PILL[k] ?? "border-control text-fg-2";

function Label({ children }: { children: ReactNode }) {
  return (
    <div className="mt-5 font-mono text-micro uppercase tracking-[0.07em] text-faint-2">{children}</div>
  );
}

function Paths({ label, paths }: { label: string; paths: string[] }) {
  if (!paths.length) return null;
  return (
    <>
      <Label>{label}</Label>
      <div className="mt-1.5 flex flex-wrap gap-1.5">
        {paths.map((p) => (
          <span key={p} className="rounded-[5px] border border-line bg-surface px-1.5 py-px font-mono text-meta text-muted">
            {p}
          </span>
        ))}
      </div>
    </>
  );
}

export function TriageDetail({
  row,
  actions,
  onAction,
  onAnswerText,
}: {
  row: TriageFeedRow;
  actions: TriageAction[];
  onAction: (a: TriageAction) => void;
  onAnswerText?: (text: string) => void;
}) {
  const isDecision = row.kind === "blocker" && !!row.decision;
  const optionActions = actions.filter((a) => a.option);
  const toolbarActions = actions.filter((a) => !a.option);
  const metaLine =
    row.kind === "blocker"
      ? `claimed by ${row.owner ?? "nobody"} · blocked ${formatAge(row.blocked_seconds)}`
      : row.kind === "collision"
        ? `detected ${formatAge(row.age_seconds)} ago`
        : row.kind === "answered"
          ? `answered ${formatAge(row.age_seconds)} ago`
          : `${row.by || "anonymous"} · ${row.source} · ↑ ${row.votes} · ${formatAge(row.age_seconds)}`;

  return (
    <div className="px-6 py-5">
      <div className="flex flex-wrap items-center gap-2 font-mono text-meta text-faint-2">
        <span className={`rounded-full border px-2 py-px uppercase ${kindPill(row.kind)}`}>{row.kind}</span>
        <span>{row.id}</span>
        {row.kind === "collision" && row.predicted && (
          <span className="rounded border border-purple/30 px-1.5 py-px text-micro uppercase text-purple">predicted</span>
        )}
        <span className="flex-1" />
        <span className="text-muted">{metaLine}</span>
      </div>
      <h2 className="mt-2 text-[18px] font-semibold leading-snug">{row.title}</h2>

      <div className="mt-4 flex flex-wrap gap-2" role="toolbar" aria-label="Actions">
        {toolbarActions.map((a) => (
          <button
            key={a.key}
            onClick={() => onAction(a)}
            className={`flex h-8 items-center gap-2 rounded-md border px-3 text-small ${
              a.primary ? "border-accent bg-accent text-bg" : "border-control text-fg-2 hover:bg-surface-3"
            }`}
          >
            <kbd className={`rounded px-1 font-mono text-micro ${a.primary ? "bg-black/15" : "bg-surface-3 text-muted"}`}>
              {a.key.toUpperCase()}
            </kbd>
            {a.label}
          </button>
        ))}
      </div>
      {isDecision && onAnswerText && (
        <AnswerOptions key={row.id} options={optionActions} onAction={onAction} onText={onAnswerText} />
      )}
      <div data-testid="triage-rule" className="mt-4 border-t border-line" />

      {row.kind === "blocker" && (
        <>
          <div className="mt-5 rounded-lg border border-st-blocked/30 bg-st-blocked/[0.07] px-3 py-2.5">
            <div className="font-mono text-micro uppercase tracking-[0.07em] text-st-blocked">
              {row.decision ? "AGENT ASKED" : "WHY IT'S BLOCKED"}
            </div>
            {row.question_state && (
              <div data-testid="question-state" className="mt-1 font-mono text-micro text-st-blocked">
                {QUESTION_LABEL[row.question_state]}
              </div>
            )}
            {row.nobody_alerted && (
              <div data-testid="nobody-alerted" className="mt-1 font-mono text-micro text-st-blocked">
                Nobody could be alerted: no one with access to this project was found.
              </div>
            )}
            <p className="mt-1.5 text-body leading-relaxed text-fg-2">
              {row.decision?.question ?? (row.reason || "No reason was recorded.")}
            </p>
            {row.decision && (
              <div className="mt-2 flex flex-wrap gap-1.5" aria-label="Agent's options">
                {row.decision.options.map((o) => (
                  <span key={o} className="rounded-full border border-st-blocked/30 px-2.5 py-0.5 text-small text-fg-2">{o}</span>
                ))}
              </div>
            )}
          </div>
          {row.waiting_on && (
            <>
              <Label>WAITING ON</Label>
              <div className="mt-1.5 flex items-center gap-2 rounded-md border border-line bg-surface px-3 py-2">
                <span className="font-mono text-meta text-fg-2">{row.waiting_on.id}</span>
                <span className="min-w-0 flex-1 truncate text-small text-muted">{row.waiting_on.title}</span>
                <span className={`font-mono text-micro uppercase ${statusText(row.waiting_on.status)}`}>
                  {row.waiting_on.status.replace("_", " ")}
                </span>
              </div>
            </>
          )}
          <Paths label="CODE TOUCH-AREA" paths={row.paths} />
        </>
      )}

      {row.kind === "collision" && (
        <>
          <Label>ITEMS IN THIS CLUSTER</Label>
          <ol className="mt-1.5 divide-y divide-line overflow-hidden rounded-md border border-line bg-surface">
            {row.members.map((m, i) => (
              <li key={m.id} className="flex items-center gap-2 px-3 py-2">
                <span className="font-mono text-meta text-faint-2">{i + 1}</span>
                <span className="font-mono text-meta text-fg-2">{m.id}</span>
                <span className="min-w-0 flex-1 truncate text-small text-muted">{m.title}</span>
                <span className="font-mono text-micro text-faint-2">{m.owner ?? "unclaimed"}</span>
                <span className={`font-mono text-micro uppercase ${statusText(m.status)}`}>
                  {m.status.replace("_", " ")}
                </span>
              </li>
            ))}
          </ol>
          <div className="mt-4 rounded-lg border border-st-review/30 bg-st-review/[0.07] px-3 py-2.5">
            <div className="font-mono text-micro uppercase tracking-[0.07em] text-st-review">
              RECOMMENDATION · {row.risk.toUpperCase()}
            </div>
            <p className="mt-1.5 text-body leading-relaxed text-fg-2">{row.recommendation}</p>
          </div>
          <Paths label="SHARED PATHS" paths={row.paths} />
        </>
      )}

      {row.kind === "answered" && <AnsweredCard row={row} />}

      {row.kind !== "blocker" && row.kind !== "collision" && row.kind !== "answered" && (
        <>
          {row.body && <p className="mt-5 text-body leading-relaxed text-fg-2">{row.body}</p>}
          {row.duplicate && (
            <>
              <div className="mt-4 rounded-lg border border-st-next/30 bg-st-next/[0.07] px-3 py-2.5">
                <div className="flex items-center gap-1.5 font-mono text-micro uppercase tracking-[0.07em] text-st-next">
                  <span aria-hidden>⧉</span>
                  {duplicateHintCopy(row.duplicate.id, row.duplicate.score)}
                </div>
                <div className="mt-1.5 flex items-center gap-2">
                  <span className="font-mono text-meta text-st-next">{row.duplicate.id}</span>
                  <span className="min-w-0 flex-1 truncate text-small text-fg-2">{row.duplicate.title}</span>
                  <span className="font-mono text-micro text-faint-2">{row.duplicate.status}</span>
                </div>
              </div>
            </>
          )}
          {row.suggested_prd && (
            <p className="mt-4 font-mono text-meta text-muted">
              Fits <span className="text-fg-2">{row.suggested_prd.id}</span>{" "}
              {Math.round(row.suggested_prd.score * 100)}% — will link on accept
            </p>
          )}
          <Paths label="CODE TOUCH-AREA" paths={row.paths} />
          {row.kind !== "feedback" && (
            <div className="mt-5">
              <CommentThread requestId={row.id} triageKey />
            </div>
          )}
        </>
      )}
    </div>
  );
}

/**
 * Where a source ref opens in the app, honouring the hosted `/p/:tag/...` scope.
 * PRD → its editor; decision → the Tracker with that item selected; memory → the
 * published lesson for that shard (an agent answers from published memory, and the
 * Memory review queue holds only unpublished candidates, so it cannot open one).
 */
export function sourceHref(kind: string | undefined, ref: string, tag: string | null): string | null {
  const id = encodeURIComponent(ref);
  const sub =
    kind === "prd" ? `prds/${id}`
      : kind === "memory" ? `lessons/${id}`
        : kind === "decision" ? `tracker?${PALETTE_ITEM_PARAM}=${id}`
          : null;
  if (!sub) return null;
  return tag ? projectPath(tag, sub) : `/${sub}`;
}

function SourceLine({ row }: { row: TriageAnsweredRow }) {
  const { pathname } = useLocation();
  const agent = row.by.agent;
  const src = row.source;
  const ref = src?.ref ?? null;
  const href = ref ? sourceHref(src?.kind, ref, tagFromPath(pathname)) : null;
  const link = href ? <Link to={href} className="text-purple underline">{ref}</Link> : ref;
  switch (src?.kind) {
    case "prd":
      return <>answered from PRD {link} by {agent}</>;
    case "memory":
      return <>answered from memory {link} by {agent}</>;
    case "decision":
      return <>answered from {link}'s decision by {agent}</>;
    case "session":
      return <>answered from this session by {agent}</>;
    default:
      return <>answered by {agent}</>;
  }
}

function AnsweredCard({ row }: { row: TriageAnsweredRow }) {
  const isAgent = !!row.by?.agent;
  const ref = row.source?.ref;
  const isSession = row.source?.kind === "session";
  return (
    <>
      <div className="mt-5 rounded-lg border border-st-done/30 bg-st-done/[0.07] px-3 py-2.5">
        <div className="font-mono text-micro uppercase tracking-[0.07em] text-st-done">ANSWERED</div>
        <p className="mt-1.5 text-small text-muted">{row.question}</p>
        <p className="mt-1.5 text-[18px] font-semibold leading-snug text-fg">{row.answer}</p>
        <div data-testid="answered-by" className="mt-2 flex flex-wrap items-center gap-2 text-small text-muted">
          {isAgent ? (
            <>
              <span aria-hidden>🤖</span>
              <span className="rounded border border-purple/40 bg-purple/10 px-1.5 py-px font-mono text-micro uppercase text-purple">
                BY AN AGENT
              </span>
              <span data-testid="answered-source"><SourceLine row={row} /></span>
            </>
          ) : (
            <>
              <span aria-hidden>👤</span>
              <span>{row.answered_by}</span>
            </>
          )}
        </div>
      </div>
      {isAgent && (
        <div data-testid="agent-quote" className="mt-3 rounded-lg border border-purple/30 bg-purple/[0.07] px-3 py-2.5">
          {row.quote ? (
            <>
              <div className="font-mono text-micro uppercase tracking-[0.07em] text-purple">THE QUOTE IT USED</div>
              <p className="mt-1.5 text-body italic text-fg-2">{row.quote}</p>
              <p className="mt-1.5 text-small text-muted">
                Does the quote support this answer? If not, tell the PRD owner.
              </p>
            </>
          ) : (
            <p className="text-small text-fg-2">
              {isSession
                ? "No quote given. Check the source before relying on this. With a session source, that means confirming with whoever ran the session."
                : `No quote given. Check the source${ref ? ` (${ref})` : ""} before relying on this. If it's wrong, tell the PRD owner.`}
            </p>
          )}
        </div>
      )}
      <DrainNote
        status={row.drain?.status} prd={row.drain?.prd}
        message={row.drain?.message} command={row.drain?.command}
      />
      <p className="mt-4 text-small text-faint-2">Leaves Triage 24h after the answer.</p>
    </>
  );
}
