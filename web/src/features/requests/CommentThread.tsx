import { MessageCircle } from "lucide-react";
import * as React from "react";

import { useCreateRequestComment, useMe, useRequestComments } from "@/lib/queries";
import { relTime } from "@/lib/relTime";
import type { RequestComment } from "@/lib/types";
import { cn } from "@/lib/cn";

/**
 * A request's comment thread with a public-reply / internal-note composer (PRD-60 D9).
 * One component for Triage and the Requests list, so the two can't drift.
 *
 * The public hint names the submitter only: voters are not notified in v1, and copy that
 * says they are would be a promise the server does not keep.
 */

type Mode = "public" | "internal";

const ROLE: Record<RequestComment["role"], { label: string; cls: string }> = {
  submitter: { label: "SUBMITTER", cls: "border-st-next/30 text-st-next" },
  team: { label: "TEAM", cls: "border-accent/30 text-accent" },
  internal: { label: "INTERNAL", cls: "border-st-review/30 text-st-review" },
};

const MODES: { id: Mode; label: string }[] = [
  { id: "public", label: "Public reply" },
  { id: "internal", label: "Internal note" },
];

function initials(name: string): string {
  if (!name || name === "anonymous") return "?";
  const parts = name.replace(/@.*/, "").split(/[.\s_-]+/).filter(Boolean);
  return parts.map((p) => p[0]).join("").slice(0, 2).toUpperCase() || "?";
}

export function CommentThread({ requestId, triageKey = false }: { requestId: string; triageKey?: boolean }) {
  const thread = useRequestComments(requestId);
  const me = useMe();
  const add = useCreateRequestComment();
  const [draft, setDraft] = React.useState("");
  const [mode, setMode] = React.useState<Mode>("public");
  const [error, setError] = React.useState<string | null>(null);

  // A new request starts with an empty box: a half-written reply must not carry over.
  React.useEffect(() => {
    setDraft("");
    setError(null);
  }, [requestId]);

  const comments = thread.data ?? [];
  const internal = mode === "internal";
  const canPost = !!draft.trim() && !add.isPending;

  async function post() {
    const body = draft.trim();
    if (!body || add.isPending) return;
    setError(null);
    try {
      await add.mutateAsync({ id: requestId, body, visibility: internal ? "private" : "public" });
      setDraft("");
    } catch (e) {
      setError(`Comment failed — ${e instanceof Error ? e.message : "the request failed"}`);
    }
  }

  function onKeyDown(e: React.KeyboardEvent<HTMLTextAreaElement>) {
    if (e.key === "Enter" && (e.metaKey || e.ctrlKey)) {
      e.preventDefault();
      void post();
    } else if (e.key === "Escape") {
      e.currentTarget.blur();
    }
  }

  return (
    <section className="flex flex-col gap-2.5 border-t border-line pt-3.5" aria-label="Comments">
      <div className="flex items-center gap-2">
        <MessageCircle size={13} className="text-muted" />
        <span className="font-mono text-micro uppercase tracking-[0.07em] text-faint-2">
          Comments · {thread.isSuccess ? comments.length : "—"}
        </span>
      </div>

      {thread.isError ? (
        <p className="text-small text-st-blocked">Comments could not be loaded.</p>
      ) : thread.isSuccess && comments.length === 0 ? (
        <p className="text-small text-muted">No comments yet. Ask the submitter for detail, or leave a note for the team.</p>
      ) : (
        comments.map((c) => {
          const role = ROLE[c.role];
          const isNote = c.role === "internal";
          const ours = c.role !== "submitter";
          const who = me.data && ours && c.author === me.data.name ? "You" : c.author || (ours ? "Former member" : "anonymous");
          return (
            <div key={c.id} className="flex items-start gap-2.5" data-role={c.role}>
              <span
                className={cn(
                  "flex h-6 w-6 flex-none items-center justify-center rounded-full font-mono text-micro",
                  ours ? "bg-accent/15 text-accent" : "bg-surface-3 text-muted",
                )}
                aria-hidden
              >
                {initials(c.author)}
              </span>
              <div
                className={cn(
                  "min-w-0 flex-1 rounded-[9px] border px-3 py-2",
                  isNote ? "border-st-review/25 bg-st-review/[0.05]" : "border-line bg-surface",
                )}
              >
                <div className="flex flex-wrap items-center gap-2">
                  <span className="text-small font-semibold text-fg-2">{who}</span>
                  <span className={cn("rounded border px-1 py-px font-mono text-micro tracking-[0.04em]", role.cls)}>
                    {role.label}
                  </span>
                  <span className="flex-1" />
                  <span className="font-mono text-micro text-faint-2">{relTime(c.created_at)}</span>
                </div>
                <p className="mt-1 whitespace-pre-wrap text-small leading-relaxed text-fg-2">{c.body}</p>
              </div>
            </div>
          );
        })
      )}

      <div
        className={cn(
          "flex flex-col gap-2 rounded-[10px] border bg-surface px-3 py-2",
          internal ? "border-st-review/30" : "border-control",
        )}
      >
        <textarea
          value={draft}
          onChange={(e) => setDraft(e.target.value)}
          onKeyDown={onKeyDown}
          rows={2}
          aria-label="Comment"
          placeholder={internal ? "Note for the team — not visible to the submitter" : "Reply to the submitter…"}
          data-triage-comment={triageKey ? "" : undefined}
          className="w-full resize-y border-0 bg-transparent text-small leading-relaxed text-fg placeholder:text-faint focus:outline-none"
        />
        <div className="flex flex-wrap items-center gap-2">
          <div className="flex overflow-hidden rounded-[7px] border border-control" role="group" aria-label="Comment mode">
            {MODES.map((m) => (
              <button
                key={m.id}
                type="button"
                aria-pressed={mode === m.id}
                onClick={() => setMode(m.id)}
                className={cn(
                  "h-6 px-2.5 text-meta",
                  mode === m.id ? "bg-surface-3" : "text-muted hover:text-fg-2",
                  mode === m.id && (m.id === "internal" ? "text-st-review" : "text-fg"),
                )}
              >
                {m.label}
              </button>
            ))}
          </div>
          <span className="flex-1 font-mono text-micro text-faint-2">
            {internal ? "only your team sees this" : "notifies the submitter"}
          </span>
          <button
            type="button"
            onClick={() => void post()}
            disabled={!canPost}
            className={cn(
              "h-[26px] rounded-[7px] border px-3 text-meta font-medium",
              canPost ? "border-accent bg-accent text-bg" : "border-control text-faint",
            )}
          >
            {add.isPending ? "Posting…" : "Comment"} <span className="font-mono text-micro opacity-70">⌘↵</span>
          </button>
        </div>
        {error && <p role="alert" className="text-small text-st-blocked">{error}</p>}
      </div>
    </section>
  );
}
