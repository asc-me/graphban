import { useQueryClient } from "@tanstack/react-query";
import { ArrowUp, Check, Inbox, MessageCircleQuestion, User as UserIcon } from "lucide-react";
import * as React from "react";

import { Markdown } from "@/lib/markdown";
import { api } from "@/lib/api";
import { keys } from "@/lib/queries";
import type { GrillMessage } from "@/lib/types";

/** AL-67: interactive grill. The agent interrogates the PRD; answers accumulate;
 *  "Apply to PRD" folds the decisions into the editor body for review + save. */
export function GrillPanel({ prdId, onApply }: { prdId: string; onApply: (body: string) => void }) {
  const [messages, setMessages] = React.useState<GrillMessage[]>([]);
  const [draft, setDraft] = React.useState("");
  const [streaming, setStreaming] = React.useState(false);
  const [applying, setApplying] = React.useState(false);
  const [captured, setCaptured] = React.useState<number | null>(null);
  const started = React.useRef(false);
  const scrollRef = React.useRef<HTMLDivElement>(null);
  const qc = useQueryClient();

  const runTurn = React.useCallback(
    async (userText: string) => {
      const history = userText
        ? [...messages, { role: "user" as const, text: userText }]
        : messages;
      if (userText) setMessages(history);
      setStreaming(true);
      setMessages((m) => [...m, { role: "agent", text: "" }]);
      try {
        await api.grillStream(prdId, userText, history, (delta) => {
          setMessages((m) => {
            const next = [...m];
            next[next.length - 1] = { role: "agent", text: next[next.length - 1].text + delta };
            return next;
          });
        });
      } finally {
        setStreaming(false);
        // The round was graded server-side while that stream ran, so the progress panel
        // beside this one is now stale — including whether the round could be graded at
        // all. Leaving it stale is what made an ungraded round look like no progress.
        qc.invalidateQueries({ queryKey: ["grill", prdId] });
        qc.invalidateQueries({ queryKey: keys.prd(prdId) });
      }
    },
    [messages, prdId, qc],
  );

  // Kick off the opening questions once.
  React.useEffect(() => {
    if (!started.current) {
      started.current = true;
      void runTurn("");
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  React.useEffect(() => {
    scrollRef.current?.scrollTo?.({ top: scrollRef.current.scrollHeight });
  }, [messages]);

  function send() {
    const t = draft.trim();
    if (!t || streaming) return;
    setDraft("");
    void runTurn(t);
  }

  async function apply() {
    setApplying(true);
    try {
      const { body, decisions_captured } = await api.grillApply(prdId, messages);
      setCaptured(decisions_captured);
      // Decisions became candidate shards — refresh the Memory Review queue/badge.
      qc.invalidateQueries({ queryKey: ["shard-candidates"] });
      qc.invalidateQueries({ queryKey: ["shard-clusters"] });
      onApply(body);
    } finally {
      setApplying(false);
    }
  }

  const hasAnswers = messages.some((m) => m.role === "user");

  return (
    <div className="flex h-full min-h-0 flex-col">
      <div ref={scrollRef} className="min-h-0 flex-1 space-y-3 overflow-y-auto pr-1">
        {messages.map((m, i) => (
          <Bubble key={i} msg={m} streaming={streaming && i === messages.length - 1} />
        ))}
      </div>

      <div className="flex-none pt-2">
        {captured !== null && (
          <div className="mb-2 flex items-center gap-1.5 rounded-lg border border-line-2 bg-surface-2 px-2.5 py-1.5 text-[11.5px] text-muted">
            <Inbox size={12} className="text-accent" />
            {captured > 0
              ? `${captured} decision${captured > 1 ? "s" : ""} saved to Memory review for approval.`
              : "Draft updated. No new decisions to preserve."}
          </div>
        )}
        {hasAnswers && (
          <button
            onClick={apply}
            disabled={applying || streaming}
            className="mb-2 inline-flex w-full items-center justify-center gap-1.5 rounded-lg border border-[#1c2620] bg-[rgba(95,208,122,0.08)] px-3 py-1.5 text-[12px] font-medium text-st-done transition-colors hover:bg-[rgba(95,208,122,0.14)] disabled:opacity-50"
          >
            <Check size={13} />
            {applying ? "Folding in…" : "Apply to PRD — fold decisions into the draft"}
          </button>
        )}
        <div className="flex items-end gap-2">
          <textarea
            value={draft}
            onChange={(e) => setDraft(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === "Enter" && !e.shiftKey) {
                e.preventDefault();
                send();
              }
            }}
            rows={2}
            placeholder="Answer the questions, or steer the grill…"
            className="min-h-0 flex-1 resize-none rounded-lg border border-line-2 bg-surface-2 px-3 py-2 text-[12.5px] outline-none placeholder:text-faint focus:border-line-hover"
          />
          <button
            onClick={send}
            disabled={!draft.trim() || streaming}
            className="flex-none rounded-lg border border-line-2 bg-surface-2 p-2 text-muted transition-colors hover:text-fg disabled:opacity-40"
          >
            <ArrowUp size={15} />
          </button>
        </div>
      </div>
    </div>
  );
}

function Bubble({ msg, streaming }: { msg: GrillMessage; streaming: boolean }) {
  const isAgent = msg.role === "agent";
  return (
    <div className="flex gap-2.5">
      <span
        className={`mt-0.5 flex h-6 w-6 flex-none items-center justify-center rounded-full ${
          isAgent ? "bg-[rgba(198,242,78,0.1)] text-accent" : "bg-[rgba(167,139,250,0.12)] text-[#a78bfa]"
        }`}
      >
        {isAgent ? <MessageCircleQuestion size={12} /> : <UserIcon size={12} />}
      </span>
      <div className="min-w-0 flex-1 text-[12.5px] leading-relaxed text-fg-2">
        {msg.text ? <Markdown source={msg.text} /> : streaming ? <span className="text-faint">…</span> : null}
      </div>
    </div>
  );
}
