import { ArrowUp, Sparkles } from "lucide-react";
import * as React from "react";

import { api } from "@/lib/api";
import { cn } from "@/lib/cn";
import type { CodeHit } from "@/lib/types";

/**
 * Code-graph chat: the connected-LLM consumer. Streams an answer grounded in the code
 * structure the coding agent described (via /agent/code/stream), and surfaces the nodes it
 * was grounded in as chips — click one to select it in the graph.
 */
export function CodeChat({
  projectId,
  onSelectPath,
}: {
  projectId?: string;
  onSelectPath?: (path: string) => void;
}) {
  const [messages, setMessages] = React.useState<{ role: "agent" | "user"; text: string }[]>([
    {
      role: "agent",
      text: "Ask me about the codebase — what a module does, what depends on it, what work touches it. I answer only from the code graph agents have described.",
    },
  ]);
  const [grounding, setGrounding] = React.useState<CodeHit[]>([]);
  const [input, setInput] = React.useState("");
  const [busy, setBusy] = React.useState(false);
  const scrollRef = React.useRef<HTMLDivElement>(null);

  React.useEffect(() => {
    scrollRef.current?.scrollTo({ top: scrollRef.current.scrollHeight });
  }, [messages]);

  async function send(e: React.FormEvent) {
    e.preventDefault();
    const text = input.trim();
    if (!text || busy) return;
    setMessages((m) => [...m, { role: "user", text }, { role: "agent", text: "" }]);
    setInput("");
    setBusy(true);
    setGrounding([]);
    try {
      await api.codeChatStream(
        text,
        {
          onNodes: (nodes) => setGrounding(nodes),
          onDelta: (delta) =>
            setMessages((m) => {
              const copy = [...m];
              const last = copy[copy.length - 1];
              copy[copy.length - 1] = { ...last, text: last.text + delta };
              return copy;
            }),
        },
        projectId,
      );
    } catch {
      setMessages((m) => {
        const copy = [...m];
        copy[copy.length - 1] = { role: "agent", text: "Something went wrong reaching the backend." };
        return copy;
      });
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="flex h-full min-h-0 flex-col">
      <div className="flex h-12 flex-none items-center gap-2 border-b border-line px-4 text-[13px] font-semibold">
        <Sparkles size={15} className="text-purple" />
        Ask the codebase
      </div>

      <div ref={scrollRef} className="min-h-0 flex-1 space-y-3 overflow-y-auto p-4">
        {messages.map((m, i) => (
          <div key={i} className={cn("flex gap-2.5", m.role === "user" && "flex-row-reverse")}>
            {m.role === "agent" && (
              <span className="mt-0.5 flex h-6 w-6 flex-none items-center justify-center rounded-full bg-[rgba(167,139,250,0.12)]">
                <Sparkles size={12} className="text-purple" />
              </span>
            )}
            <div
              className={cn(
                "max-w-[85%] whitespace-pre-wrap rounded-[12px] px-3 py-2 text-[12.5px] leading-relaxed",
                m.role === "agent" ? "border border-line-2 bg-surface-2 text-fg-2" : "bg-accent/90 text-bg",
              )}
            >
              {m.text || (busy && i === messages.length - 1 ? "…" : "")}
            </div>
          </div>
        ))}
        {busy && <div className="pl-9 font-mono text-[11px] text-faint">thinking…</div>}
      </div>

      {grounding.length > 0 && (
        <div className="flex-none border-t border-line px-3 py-2.5">
          <div className="mb-1.5 font-mono text-[10px] uppercase tracking-wide text-faint">
            Grounded in {grounding.length} node{grounding.length === 1 ? "" : "s"}
          </div>
          <div className="flex flex-wrap gap-1.5">
            {grounding.map(({ node, score }) => (
              <button
                key={node.id}
                onClick={() => onSelectPath?.(node.path)}
                title={node.summary}
                className="inline-flex items-center gap-1.5 rounded-md border border-control bg-surface-2 px-2 py-1 text-[11px] transition-colors hover:border-control-hover"
              >
                <span className="max-w-[180px] truncate font-mono text-fg-2">{node.path}</span>
                <span className="font-mono text-[10px] text-accent">{score.toFixed(2)}</span>
              </button>
            ))}
          </div>
        </div>
      )}

      <form onSubmit={send} className="flex-none border-t border-line p-3">
        <div className="flex items-center gap-2 rounded-[10px] border border-line-2 bg-surface-2 px-2.5 focus-within:border-line-hover">
          <input
            value={input}
            onChange={(e) => setInput(e.target.value)}
            placeholder="What depends on the embedder?"
            className="h-9 flex-1 bg-transparent text-[12.5px] outline-none placeholder:text-faint"
          />
          <button type="submit" disabled={busy} className="rounded-md p-1 text-accent disabled:opacity-40">
            <ArrowUp size={15} />
          </button>
        </div>
      </form>
    </div>
  );
}
